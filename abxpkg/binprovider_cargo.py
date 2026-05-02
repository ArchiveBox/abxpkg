#!/usr/bin/env python3
__package__ = "abxpkg"

import hashlib
import os
import platform
import subprocess
import urllib.request

from pathlib import Path

from pydantic import Field, model_validator, computed_field
from typing import Self, cast, ClassVar

from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    abxpkg_install_root_default,
)
from .semver import SemVer
from .binprovider import BinProvider, EnvProvider, log_method_call, remap_kwargs
from .logging import format_subprocess_output, get_logger


DEFAULT_CARGO_HOME = Path(os.environ.get("CARGO_HOME", "~/.cargo")).expanduser()
MIN_CARGO_INSTALLER_VERSION = cast(SemVer, SemVer.parse("1.85.0"))
# Canonical rust-lang.org distribution. Each ``rustup-init`` binary has a
# sibling ``rustup-init.sha256`` file we use to verify the download — we
# never run a ``curl sh.rustup.rs | sh``-style unverified installer.
RUSTUP_DIST_BASE = "https://static.rust-lang.org/rustup/dist"
logger = get_logger(__name__)


class CargoProvider(BinProvider):
    name: BinProviderName = "cargo"
    _log_emoji = "🦀"
    INSTALLER_BIN: BinName = "cargo"
    INSTALLER_BINPROVIDERS: ClassVar[tuple[BinProviderName, ...] | None] = (
        "env",
        "brew",
        "apt",
        "nix",
    )

    PATH: PATHStr = ""  # Starts empty; setup_PATH() fills it with cargo_home/bin plus any install_root/bin override.

    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("cargo"),
        validation_alias="cargo_root",
    )
    # detect_euid_to_use() resolves this to the active cargo install bin dir and setup()
    # creates it when using a managed non-default cargo root.
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        cargo_home = DEFAULT_CARGO_HOME
        env: dict[str, str] = {
            "CARGO_HOME": str(cargo_home),
            "CARGO_TARGET_DIR": str(self.install_root / "target"),
        }
        if self.install_root != cargo_home:
            env["CARGO_INSTALL_ROOT"] = str(self.install_root)
        return env

    @computed_field
    @property
    def is_valid(self) -> bool:
        return super().is_valid

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Resolve Cargo's install_root/bin_dir defaults from the active cargo home."""
        if self.install_root is None:
            self.install_root = DEFAULT_CARGO_HOME
        if self.bin_dir is None:
            self.bin_dir = self.install_root / "bin"

        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from cargo_home/bin and any install_root/bin override."""
        cargo_bin_dirs = [DEFAULT_CARGO_HOME / "bin"]
        install_root = self.install_root
        assert install_root is not None
        if install_root != DEFAULT_CARGO_HOME:
            cargo_bin_dirs.insert(0, install_root / "bin")
        self.PATH = self._merge_PATH(*cargo_bin_dirs, PATH=self.PATH, prepend=True)
        super().setup_PATH(no_cache=no_cache)

    def INSTALLER_BINARY(self, no_cache: bool = False):
        from . import Binary, DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

        cached_installer = self._INSTALLER_BINARY
        if not no_cache and cached_installer and cached_installer.is_valid:
            cached_version = cached_installer.loaded_version
            if (
                cached_version is not None
                and cached_version >= MIN_CARGO_INSTALLER_VERSION
                and self._cargo_executes(cached_installer.loaded_abspath)
            ):
                return cached_installer

        loaded = None
        try:
            loaded = super().INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            loaded = None

        if loaded and loaded.loaded_abspath:
            loaded_version = loaded.loaded_version
            if (
                loaded_version is not None
                and loaded_version >= MIN_CARGO_INSTALLER_VERSION
                and self._cargo_executes(loaded.loaded_abspath)
            ):
                self._INSTALLER_BINARY = loaded
                return loaded
            # Discovered binary doesn't actually run (e.g. linuxbrew's cargo
            # missing libllhttp.so on a stale CI image). Drop it so the
            # install path below kicks in instead of returning a broken bin.
            loaded = None

        raw_provider_names = os.environ.get("ABXPKG_BINPROVIDERS")
        selected_provider_names = (
            [provider_name.strip() for provider_name in raw_provider_names.split(",")]
            if raw_provider_names
            else list(DEFAULT_PROVIDER_NAMES)
        )
        if raw_provider_names:
            preferred_provider_names = selected_provider_names
        else:
            installer_binproviders = self.INSTALLER_BINPROVIDERS
            assert installer_binproviders is not None
            preferred_provider_names = list(installer_binproviders)
        env_provider = EnvProvider(install_root=None, bin_dir=None)
        installer_providers: list[BinProvider] = [
            env_provider
            if provider_name == "env"
            else PROVIDER_CLASS_BY_NAME[provider_name]()
            for provider_name in preferred_provider_names
            if provider_name
            and provider_name in selected_provider_names
            and provider_name in PROVIDER_CLASS_BY_NAME
            and provider_name != self.name
        ]
        if not installer_providers:
            installer_providers = [env_provider]

        try:
            upgraded = Binary(
                name=self.INSTALLER_BIN,
                min_version=MIN_CARGO_INSTALLER_VERSION,
                binproviders=installer_providers,
            ).install(no_cache=no_cache)
        except Exception:
            upgraded = None
        if (
            upgraded
            and upgraded.loaded_abspath
            and self._cargo_executes(upgraded.loaded_abspath)
        ):
            self._INSTALLER_BINARY = upgraded
            return upgraded

        # Last-resort fallback: bootstrap a hermetic rust toolchain via the
        # canonical rustup-init installer. This bypasses any broken/partial
        # system rust installs (e.g. linuxbrew's cargo missing libllhttp.so
        # on a stale CI image) and never depends on root/sudo.
        logger.warning(
            "%s: no working cargo from env/brew/apt/nix; falling back to rustup-init",
            self.__class__.__name__,
        )
        try:
            rustup_cargo = self._install_via_rustup(no_cache=no_cache)
        except Exception as err:
            logger.warning(
                "%s: rustup-init fallback raised %r",
                self.__class__.__name__,
                err,
            )
            rustup_cargo = None
        if rustup_cargo is not None:
            rustup_loaded = EnvProvider(
                install_root=None,
                bin_dir=None,
                PATH=str(rustup_cargo.parent),
            ).load(self.INSTALLER_BIN, no_cache=no_cache)
            if rustup_loaded and rustup_loaded.loaded_abspath:
                self._INSTALLER_BINARY = rustup_loaded
                return rustup_loaded

        from .exceptions import BinProviderUnavailableError

        raise BinProviderUnavailableError(
            self.__class__.__name__,
            self.INSTALLER_BIN,
        )

    def _cargo_executes(self, abspath) -> bool:
        """Return True iff ``<abspath> --version`` exits cleanly with parseable output.

        Guards against partially broken cargo installs where the binary exists
        on PATH but won't actually run (e.g. brew's cargo dynamically linked
        to a libllhttp that's been removed). The base BinProvider load() does
        a version probe, but providers can persist cache entries that bypass
        it; this is a final, no-cache executable check.
        """
        if abspath is None:
            return False
        try:
            proc = subprocess.run(
                [str(abspath), "--version"],
                capture_output=True,
                text=True,
                timeout=self.version_timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if proc.returncode != 0:
            return False
        return bool(SemVer.parse(proc.stdout.strip() or proc.stderr.strip()))

    @staticmethod
    def _rustup_target_triple() -> str | None:
        """Return the rust-lang.org target triple for the current host, or None.

        Only the four targets we publish CI/dev support for are returned —
        unknown hosts get None so the rustup fallback is skipped instead of
        downloading a binary that doesn't match the host ABI.
        """
        machine = platform.machine().lower()
        system = platform.system().lower()
        arch = {
            "x86_64": "x86_64",
            "amd64": "x86_64",
            "aarch64": "aarch64",
            "arm64": "aarch64",
        }.get(machine)
        if arch is None:
            return None
        if system == "linux":
            return f"{arch}-unknown-linux-gnu"
        if system == "darwin":
            return f"{arch}-apple-darwin"
        return None

    def _install_via_rustup(self, no_cache: bool = False) -> Path | None:
        """Bootstrap rust via the rustup-init binary into CARGO_HOME and return cargo's abspath.

        Used as a last-resort installer when no other BinProvider can produce
        a working ``cargo`` binary. Honors ``$CARGO_HOME`` when set and
        installs a minimal stable profile so this runs in 60-90s on CI.

        The downloaded ``rustup-init`` binary is verified against the
        ``rustup-init.sha256`` sidecar file published alongside it on
        ``static.rust-lang.org`` BEFORE we exec it, so a tampered download
        won't run.
        """
        triple = self._rustup_target_triple()
        if triple is None:
            logger.warning(
                "Unsupported host for rustup fallback: %s %s",
                platform.system(),
                platform.machine(),
            )
            return None

        cargo_home = DEFAULT_CARGO_HOME
        try:
            cargo_home.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            logger.warning("rustup fallback: cannot create %s: %s", cargo_home, err)
            return None

        binary_url = f"{RUSTUP_DIST_BASE}/{triple}/rustup-init"
        sha_url = f"{binary_url}.sha256"
        logger.warning(
            "rustup fallback: downloading verified rustup-init from %s",
            binary_url,
        )
        try:
            with urllib.request.urlopen(binary_url, timeout=60) as response:
                binary_bytes = response.read()
            with urllib.request.urlopen(sha_url, timeout=30) as response:
                sha_text = response.read().decode("utf-8", errors="replace")
        except Exception as err:
            logger.warning(
                "Failed to download rustup-init from %s: %s",
                binary_url,
                err,
            )
            return None

        # The sidecar is a single line: "<hex>  rustup-init". Take the first
        # 64 hex chars and reject anything that isn't a clean SHA-256.
        expected_sha = (sha_text.strip().split() or [""])[0].lower()
        if len(expected_sha) != 64 or any(
            ch not in "0123456789abcdef" for ch in expected_sha
        ):
            logger.warning(
                "rustup-init.sha256 sidecar at %s is malformed: %r",
                sha_url,
                sha_text[:120],
            )
            return None

        actual_sha = hashlib.sha256(binary_bytes).hexdigest()
        if actual_sha != expected_sha:
            logger.warning(
                "rustup-init SHA-256 mismatch (got %s, expected %s) — refusing to run",
                actual_sha,
                expected_sha,
            )
            return None

        rustup_init = cargo_home / "rustup-init"
        rustup_init.write_bytes(binary_bytes)
        rustup_init.chmod(0o755)

        env = {
            **os.environ,
            "CARGO_HOME": str(cargo_home),
            "RUSTUP_HOME": str(cargo_home / ".rustup"),
        }
        try:
            proc = subprocess.run(
                [
                    str(rustup_init),
                    "-y",
                    "--no-modify-path",
                    "--default-toolchain",
                    "stable",
                    "--profile",
                    "minimal",
                ],
                capture_output=True,
                text=True,
                timeout=max(self.install_timeout, 600),
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as err:
            logger.warning("rustup-init failed to run: %s", err)
            return None
        finally:
            rustup_init.unlink(missing_ok=True)

        if proc.returncode != 0:
            logger.warning(
                "rustup-init exited with %s: %s",
                proc.returncode,
                format_subprocess_output(proc.stdout, proc.stderr),
            )
            return None

        cargo_path = cargo_home / "bin" / "cargo"
        if not cargo_path.is_file():
            logger.warning(
                "rustup-init exited 0 but %s was not produced; output=%s",
                cargo_path,
                format_subprocess_output(proc.stdout, proc.stderr),
            )
            return None
        if not self._cargo_executes(cargo_path):
            logger.warning(
                "rustup-init produced %s but it does not run cleanly",
                cargo_path,
            )
            return None
        logger.warning("rustup-init successfully bootstrapped %s", cargo_path)
        return cargo_path

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        install_root = self.install_root
        assert install_root is not None
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(install_root, DEFAULT_CARGO_HOME),
                preserve_root=True,
            )
        DEFAULT_CARGO_HOME.mkdir(parents=True, exist_ok=True)
        (install_root / "target").mkdir(parents=True, exist_ok=True)
        if install_root != DEFAULT_CARGO_HOME:
            bin_dir = self.bin_dir
            assert bin_dir is not None
            bin_dir.mkdir(parents=True, exist_ok=True)

    def _cargo_package_specs(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
    ) -> list[str]:
        """Extract bare cargo package names from install_args for uninstall operations."""
        install_args = list(install_args or self.get_install_args(bin_name))
        options_with_values = {
            "--version",
            "--git",
            "--branch",
            "--tag",
            "--rev",
            "--path",
            "--root",
            "--index",
            "--registry",
            "--bin",
            "--example",
            "--profile",
            "--target",
            "--target-dir",
            "--config",
            "-j",
            "--jobs",
            "-Z",
        }
        package_specs: list[str] = []
        skip_next = False
        for arg in install_args:
            if skip_next:
                skip_next = False
                continue
            if arg in options_with_values:
                skip_next = True
                continue
            if arg.startswith("-"):
                continue
            package_specs.append(arg)
        return package_specs or [bin_name]

    def default_docs_url_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> str | None:
        package = self._docs_url_package_name(bin_name)
        if not package:
            return None
        return f"https://crates.io/crates/{package}"

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)
        if min_version and not any(arg.startswith("--version") for arg in install_args):
            install_args = ["--version", f">={min_version}", *install_args]
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        cargo_install_args = ["--locked"]
        if self.install_root != DEFAULT_CARGO_HOME:
            cargo_install_args.extend(["--root", str(self.install_root)])

        proc = self.exec(
            bin_name=installer_bin,
            cmd=["install", *cargo_install_args, *install_args],
            timeout=timeout,
        )
        proc_output = format_subprocess_output(proc.stdout, proc.stderr)
        if (
            proc.returncode != 0
            and "--locked" in cargo_install_args
            and "lock file version 4 requires `-Znext-lockfile-bump`" in proc_output
        ):
            proc = self.exec(
                bin_name=installer_bin,
                cmd=["install", *cargo_install_args[1:], *install_args],
                timeout=timeout,
            )
        if proc.returncode != 0:
            self._raise_proc_error("install", install_args, proc)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)
        if min_version and not any(arg.startswith("--version") for arg in install_args):
            install_args = ["--version", f">={min_version}", *install_args]
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        cargo_install_args = ["--locked"]
        if self.install_root != DEFAULT_CARGO_HOME:
            cargo_install_args.extend(["--root", str(self.install_root)])

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "install",
                "--force",
                *cargo_install_args,
                *install_args,
            ],
            timeout=timeout,
        )
        proc_output = format_subprocess_output(proc.stdout, proc.stderr)
        if (
            proc.returncode != 0
            and "--locked" in cargo_install_args
            and "lock file version 4 requires `-Znext-lockfile-bump`" in proc_output
        ):
            proc = self.exec(
                bin_name=installer_bin,
                cmd=[
                    "install",
                    "--force",
                    *cargo_install_args[1:],
                    *install_args,
                ],
                timeout=timeout,
            )
        if proc.returncode != 0:
            self._raise_proc_error("update", install_args, proc)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> bool:
        install_args = install_args or self.get_install_args(bin_name)
        package_specs = self._cargo_package_specs(
            bin_name,
            install_args=install_args,
        )
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "uninstall",
                *(
                    ["--root", str(self.install_root)]
                    if self.install_root is not None
                    and self.install_root != DEFAULT_CARGO_HOME
                    else []
                ),
                *package_specs,
            ],
            timeout=timeout,
        )
        if proc.returncode != 0 and "did not match any packages" not in proc.stderr:
            self._raise_proc_error("uninstall", package_specs, proc)

        return True

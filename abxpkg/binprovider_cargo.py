#!/usr/bin/env python3
__package__ = "abxpkg"

import os
import subprocess

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
from .logging import format_subprocess_output


DEFAULT_CARGO_HOME = Path(os.environ.get("CARGO_HOME", "~/.cargo")).expanduser()
MIN_CARGO_INSTALLER_VERSION = cast(SemVer, SemVer.parse("1.85.0"))


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

        from .exceptions import BinProviderUnavailableError

        raise BinProviderUnavailableError(
            self.__class__.__name__,
            self.INSTALLER_BIN,
        )

    def _cargo_executes(self, abspath) -> bool:
        """Return True iff Cargo and its Rust compiler are fully initialized.

        Guards against partially broken cargo installs where the binary exists
        on PATH but won't actually run (e.g. brew's cargo dynamically linked
        to a libllhttp that's been removed). The base BinProvider load() does
        a version probe, but providers can persist cache entries that bypass
        it; this is a final, no-cache executable check. Probe rustc too: rustup
        can defer a stable-toolchain update until the first compiler process,
        and letting a parallel ``cargo install`` trigger that update races
        sibling rustc processes against a temporarily incomplete sysroot.
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
        if not SemVer.parse(proc.stdout.strip() or proc.stderr.strip()):
            return False

        rustc_abspath = self._rustc_for_cargo(abspath)
        if rustc_abspath is None:
            return False
        try:
            rustc_proc = subprocess.run(
                [str(rustc_abspath), "--version"],
                capture_output=True,
                text=True,
                timeout=max(self.version_timeout, 120),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return rustc_proc.returncode == 0 and bool(
            SemVer.parse(rustc_proc.stdout.strip() or rustc_proc.stderr.strip()),
        )

    @staticmethod
    def _rustc_for_cargo(cargo_abspath: str | Path) -> Path | None:
        """Return the compiler shipped beside the selected Cargo executable."""
        try:
            rustc_abspath = Path(cargo_abspath).resolve(strict=True).with_name("rustc")
        except OSError:
            return None
        return rustc_abspath if rustc_abspath.is_file() else None

    def _cargo_build_env(self, cargo_abspath: str | Path) -> dict[str, str]:
        """Keep Cargo on its matching compiler instead of another PATH toolchain."""
        env = os.environ.copy()
        if "RUSTC" not in env:
            rustc_abspath = self._rustc_for_cargo(cargo_abspath)
            if rustc_abspath is not None:
                env["RUSTC"] = str(rustc_abspath)
        return env

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

    def default_search_handler(
        self,
        bin_name: str,
        min_version: SemVer | None = None,
        min_release_age: float | None = None,
        timeout: int | None = None,
        **context,
    ) -> list:
        """Search crates.io for crates whose name matches bin_name."""
        from .binary import Binary

        installer = self.INSTALLER_BINARY(no_cache=bool(context.get("no_cache", False)))
        assert installer and installer.loaded_abspath
        # ``cargo search`` returns lines like:
        #   <crate> = "<version>"      # <description>
        proc = self.exec(
            bin_name=installer.loaded_abspath,
            cmd=["search", "--limit", "25", str(bin_name)],
            quiet=True,
            timeout=timeout,
        )
        results: list = []
        for line in proc.stdout.splitlines():
            if "=" not in line or '"' not in line:
                continue
            crate_name = line.split("=", 1)[0].strip()
            version_str = line.split('"', 2)[1] if '"' in line else ""
            description = line.split("# ", 1)[1].strip() if "# " in line else ""
            if not crate_name or str(bin_name) not in crate_name:
                continue
            results.append(
                Binary(
                    name=crate_name,
                    description=f"{version_str} - {description}".strip(" -"),
                    binproviders=[self],
                    overrides={self.name: {"install_args": [crate_name]}},
                ),
            )
        return results

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
            env=self._cargo_build_env(installer_bin),
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
                env=self._cargo_build_env(installer_bin),
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
            env=self._cargo_build_env(installer_bin),
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
                env=self._cargo_build_env(installer_bin),
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

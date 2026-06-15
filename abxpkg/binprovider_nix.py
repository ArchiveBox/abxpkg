#!/usr/bin/env python3
__package__ = "abxpkg"

import json
import os

from pathlib import Path

from pydantic import Field, model_validator, computed_field
from typing import Self

from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    abxpkg_install_root_default,
    bin_abspath,
)
from .semver import SemVer
from .binprovider import (
    BinProvider,
    EnvProvider,
    DEFAULT_ENV_PATH,
    log_method_call,
    remap_kwargs,
)
from .logging import format_subprocess_output, get_logger

logger = get_logger(__name__)


# Ultimate fallback when neither the constructor arg nor
# ``ABXPKG_NIX_ROOT`` nor ``ABXPKG_LIB_DIR`` is set.
DEFAULT_NIX_PROFILE = Path("~/.nix-profile").expanduser()
DEFAULT_NIX_BIN_DIR = Path("/nix/var/nix/profiles/default/bin")


class NixProvider(BinProvider):
    name: BinProviderName = "nix"
    _log_emoji = "❄️"
    INSTALLER_BIN: BinName = "nix"

    PATH: PATHStr = (
        ""  # Starts empty; setup_PATH() lazily replaces it with install_root/bin only.
    )

    install_root: Path | None = Field(
        default_factory=lambda: (
            abxpkg_install_root_default("nix") or DEFAULT_NIX_PROFILE
        ),
        validation_alias="nix_profile",
    )
    # detect_euid_to_use() fills this from the active Nix profile path and setup_PATH()
    # reads it to prepend the profile's runtime bin dir on every resolution pass.
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        env: dict[str, str] = {
            "LD_LIBRARY_PATH": ":" + str(self.install_root / "lib"),
        }
        return env

    @computed_field
    @property
    def is_valid(self) -> bool:
        install_root = self.install_root
        assert install_root is not None
        profile_bin_dir = install_root / "bin"
        if profile_bin_dir.exists() and not os.access(profile_bin_dir, os.R_OK):
            return False

        return bool(
            bin_abspath(
                self.INSTALLER_BIN,
                PATH=f"{DEFAULT_NIX_BIN_DIR}:{DEFAULT_ENV_PATH}",
            )
            or bin_abspath(self.INSTALLER_BIN),
        )

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Fill in the active Nix profile bin dir from the resolved install_root."""
        install_root = self.install_root
        assert install_root is not None
        if self.bin_dir is None:
            self.bin_dir = install_root / "bin"

        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from install_root/bin only."""
        install_root = self.install_root
        assert install_root is not None
        self.PATH = self._merge_PATH(
            install_root / "bin",
            PATH=self.PATH,
            prepend=True,
        )
        super().setup_PATH(no_cache=no_cache)

    @property
    def derived_env_path(self) -> Path | None:
        install_root = self.install_root
        if install_root is None:
            return None
        return install_root.parent / f".{install_root.name}.derived.env"

    def INSTALLER_BINARY(self, no_cache: bool = False):
        if not no_cache and self._INSTALLER_BINARY and self._INSTALLER_BINARY.is_valid:
            return self._INSTALLER_BINARY

        derived_env_path = self.derived_env_path
        if not no_cache and derived_env_path and derived_env_path.is_file():
            from .config import load_derived_cache

            cache = load_derived_cache(derived_env_path)
            for cached_record in cache.values():
                if not isinstance(cached_record, dict):
                    continue
                if cached_record.get("provider_name") != self.name or cached_record.get(
                    "bin_name",
                ) != str(self.INSTALLER_BIN):
                    continue
                cached_abspath = cached_record.get("abspath")
                if not isinstance(cached_abspath, str):
                    continue
                loaded = self.load_cached_binary(
                    self.INSTALLER_BIN,
                    Path(cached_abspath),
                )
                if loaded and loaded.loaded_abspath:
                    self._INSTALLER_BINARY = loaded
                    return loaded

        env_provider = EnvProvider(install_root=None, bin_dir=None)
        env_var = f"{self.INSTALLER_BIN.upper()}_BINARY"
        manual = os.environ.get(env_var)
        if manual and os.path.isabs(manual) and Path(manual).is_file():
            env_provider.PATH = env_provider._merge_PATH(
                str(Path(manual).parent),
                PATH=env_provider.PATH,
                prepend=True,
            )

        loaded = env_provider.load(bin_name=self.INSTALLER_BIN, no_cache=no_cache)
        if loaded and loaded.loaded_abspath:
            if loaded.loaded_version and loaded.loaded_sha256:
                self.write_cached_binary(
                    self.INSTALLER_BIN,
                    loaded.loaded_abspath,
                    loaded.loaded_version,
                    loaded.loaded_sha256,
                    resolved_provider_name=(
                        loaded.loaded_binprovider.name
                        if loaded.loaded_binprovider is not None
                        else self.name
                    ),
                    resolved_provider=loaded.loaded_binprovider,
                    cache_kind="dependency",
                )
            self._INSTALLER_BINARY = loaded
            return self._INSTALLER_BINARY

        return super().INSTALLER_BINARY(no_cache=no_cache)

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
                owner_paths=(install_root.parent,),
                preserve_root=True,
            )
        install_root.parent.mkdir(parents=True, exist_ok=True)

    def _profile_element_name(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
    ) -> str:
        """Map install_args to the Nix profile element name used by upgrade/remove."""
        install_args = install_args or self.get_install_args(bin_name)
        install_target = str(install_args[0]) if install_args else bin_name
        element = install_target.split("#", 1)[-1].split("^", 1)[0]
        return element or bin_name

    def default_install_args_handler(self, bin_name: BinName, **context) -> InstallArgs:
        return [bin_name]

    def default_docs_url_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> str | None:
        package = self._docs_url_package_name(bin_name)
        if not package:
            return None
        # Nix flake refs like "nixpkgs#foo" -> use the attr after #
        if "#" in package:
            package = package.split("#", 1)[-1].split("^", 1)[0]
        return f"https://search.nixos.org/packages?show={package}&query={package}"

    def default_search_handler(
        self,
        bin_name: str,
        min_version: SemVer | None = None,
        min_release_age: float | None = None,
        timeout: int | None = None,
        **context,
    ) -> list:
        """Search nixpkgs for attributes whose name matches bin_name (substring)."""
        from .binary import Binary

        # Use ``self.INSTALLER_BINARY`` so nix's auto-install logic
        # kicks in if env's nix is missing/broken.
        installer = self.INSTALLER_BINARY(no_cache=bool(context.get("no_cache", False)))
        assert installer and installer.loaded_abspath
        # ``nix search`` against a flake (e.g. ``nixpkgs#``) evaluates
        # the entire flake via the daemon and runs OOM on small CI
        # runners (rc=-9 / SIGKILL on the GitHub-hosted x86_64 hosts).
        # Direct ``nix eval nixpkgs#<bin_name>`` only evaluates the
        # single requested attribute path, so it's bounded in memory
        # and finishes in ~seconds even on the cold flake-fetch run.
        # Filter env exactly like ``default_install_handler`` does
        # (drop GH/GITHUB tokens + NIX_REMOTE).
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"GH_TOKEN", "GITHUB_TOKEN", "NIX_REMOTE"}
        }
        proc = self.exec(
            bin_name=installer.loaded_abspath,
            cmd=[
                "eval",
                "--extra-experimental-features",
                "nix-command flakes",
                "--json",
                "--apply",
                "p: { pname = p.pname or p.name or null; "
                "version = p.version or null; "
                "description = (p.meta or {}).description or null; }",
                f"nixpkgs#{bin_name}",
            ],
            env=env,
            quiet=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            # ``nix eval`` exits non-zero when the requested attribute
            # path doesn't exist in the flake — exact-match miss is a
            # legitimate "no result" outcome, not a failure to log. Only
            # warn when the failure mode looks genuinely broken
            # (daemon socket, network, registry) so CI logs surface the
            # real cause instead of every legit miss.
            stderr_text = (proc.stderr or "").lower()
            looks_like_attribute_miss = (
                "does not provide attribute" in stderr_text
                or "missing attribute" in stderr_text
                or "evaluation aborted" in stderr_text
            )
            if not looks_like_attribute_miss:
                logger.warning(
                    "nix search failed for %r (rc=%s):\n%s",
                    str(bin_name),
                    proc.returncode,
                    format_subprocess_output(proc.stdout, proc.stderr),
                )
            return []
        try:
            info = json.loads(proc.stdout) or {}
        except json.JSONDecodeError:
            return []
        if not isinstance(info, dict):
            return []
        pname = info.get("pname") or str(bin_name)
        version_str = info.get("version") or ""
        description = info.get("description") or pname
        return [
            Binary(
                name=pname,
                description=f"{version_str} - {description}".strip(" -"),
                binproviders=[self],
                overrides={self.name: {"install_args": [f"nixpkgs#{bin_name}"]}},
            ),
        ]

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
        cmd_install_args = list(install_args)
        if not any(
            arg in {"-f", "--file", "--expr", "--override-flake", "--inputs-from"}
            or "#" in arg
            or arg.startswith(("channel:", "http://", "https://", "github:", "path:"))
            for arg in cmd_install_args
        ):
            cmd_install_args = [
                "-f",
                "https://channels.nixos.org/nixpkgs-unstable/nixexprs.tar.xz",
                *cmd_install_args,
            ]
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"GH_TOKEN", "GITHUB_TOKEN", "NIX_REMOTE"}
        }
        nix_config = env.get("NIX_CONFIG", "").rstrip()
        env["NIX_CONFIG"] = "\n".join(
            line
            for line in (
                nix_config,
                "access-tokens = github.com=",
            )
            if line
        )
        profile_element = self._profile_element_name(
            bin_name,
            install_args=install_args,
        )
        list_proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "--access-tokens",
                "",
                "profile",
                "list",
                "--json",
                "--extra-experimental-features",
                "nix-command",
                "--extra-experimental-features",
                "flakes",
                "--profile",
                str(self.install_root),
            ],
            env=env,
            timeout=timeout,
            quiet=True,
        )
        if (
            list_proc.returncode == 0
            and list_proc.stdout.strip()
            and profile_element in json.loads(list_proc.stdout).get("elements", {})
        ):
            return self.default_update_handler(
                bin_name=bin_name,
                install_args=install_args,
                postinstall_scripts=postinstall_scripts,
                min_release_age=min_release_age,
                min_version=min_version,
                no_cache=no_cache,
                timeout=timeout,
            )

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "--access-tokens",
                "",
                "profile",
                "add",
                "--extra-experimental-features",
                "nix-command",
                "--extra-experimental-features",
                "flakes",
                "--profile",
                str(self.install_root),
                *cmd_install_args,
            ],
            env=env,
            timeout=timeout,
        )
        proc_output = format_subprocess_output(proc.stdout, proc.stderr)
        systemctl_bin = next(
            (
                str(path)
                for path in (Path("/usr/bin/systemctl"), Path("/bin/systemctl"))
                if path.is_file()
            ),
            None,
        )
        if (
            proc.returncode != 0
            and os.uname().sysname == "Linux"
            and systemctl_bin is not None
            and Path("/run/systemd/system").is_dir()
            and (
                "cannot connect to socket at '/nix/var/nix/daemon-socket/socket'"
                in proc_output
                or "opening a connection to remote store 'daemon' previously failed"
                in proc_output
            )
        ):
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "daemon-reload"],
                timeout=timeout,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "reset-failed", "nix-daemon.socket"],
                timeout=timeout,
                quiet=True,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "reset-failed", "nix-daemon.service"],
                timeout=timeout,
                quiet=True,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "start", "nix-daemon.socket"],
                timeout=timeout,
                quiet=True,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "start", "nix-daemon.service"],
                timeout=timeout,
                quiet=True,
            )
            proc = self.exec(
                bin_name=installer_bin,
                cmd=[
                    "--access-tokens",
                    "",
                    "profile",
                    "add",
                    "--extra-experimental-features",
                    "nix-command",
                    "--extra-experimental-features",
                    "flakes",
                    "--profile",
                    str(self.install_root),
                    *cmd_install_args,
                ],
                env=env,
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
        profile_element = self._profile_element_name(
            bin_name,
            install_args=install_args,
        )
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"GH_TOKEN", "GITHUB_TOKEN", "NIX_REMOTE"}
        }
        nix_config = env.get("NIX_CONFIG", "").rstrip()
        env["NIX_CONFIG"] = "\n".join(
            line
            for line in (
                nix_config,
                "access-tokens = github.com=",
            )
            if line
        )
        list_proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "--access-tokens",
                "",
                "profile",
                "list",
                "--json",
                "--extra-experimental-features",
                "nix-command",
                "--extra-experimental-features",
                "flakes",
                "--profile",
                str(self.install_root),
            ],
            env=env,
            timeout=timeout,
            quiet=True,
        )
        profile_elements = [profile_element]
        if list_proc.returncode == 0 and list_proc.stdout.strip():
            profile_elements = [
                element_name
                for element_name in json.loads(list_proc.stdout).get("elements", {})
                if element_name == profile_element
                or (
                    element_name.startswith(f"{profile_element}-")
                    and element_name.removeprefix(f"{profile_element}-").isdigit()
                )
            ] or [profile_element]

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "--access-tokens",
                "",
                "profile",
                "upgrade",
                "--extra-experimental-features",
                "nix-command",
                "--extra-experimental-features",
                "flakes",
                "--profile",
                str(self.install_root),
                *profile_elements,
            ],
            env=env,
            timeout=timeout,
        )
        proc_output = format_subprocess_output(proc.stdout, proc.stderr)
        systemctl_bin = next(
            (
                str(path)
                for path in (Path("/usr/bin/systemctl"), Path("/bin/systemctl"))
                if path.is_file()
            ),
            None,
        )
        if (
            proc.returncode != 0
            and os.uname().sysname == "Linux"
            and systemctl_bin is not None
            and Path("/run/systemd/system").is_dir()
            and (
                "cannot connect to socket at '/nix/var/nix/daemon-socket/socket'"
                in proc_output
                or "opening a connection to remote store 'daemon' previously failed"
                in proc_output
            )
        ):
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "daemon-reload"],
                timeout=timeout,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "reset-failed", "nix-daemon.socket"],
                timeout=timeout,
                quiet=True,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "reset-failed", "nix-daemon.service"],
                timeout=timeout,
                quiet=True,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "start", "nix-daemon.socket"],
                timeout=timeout,
                quiet=True,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "start", "nix-daemon.service"],
                timeout=timeout,
                quiet=True,
            )
            proc = self.exec(
                bin_name=installer_bin,
                cmd=[
                    "--access-tokens",
                    "",
                    "profile",
                    "upgrade",
                    "--extra-experimental-features",
                    "nix-command",
                    "--extra-experimental-features",
                    "flakes",
                    "--profile",
                    str(self.install_root),
                    profile_element,
                ],
                env=env,
                timeout=timeout,
            )
        if proc.returncode != 0:
            self._raise_proc_error("update", profile_element, proc)

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
        profile_element = self._profile_element_name(
            bin_name,
            install_args=install_args,
        )
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"GH_TOKEN", "GITHUB_TOKEN", "NIX_REMOTE"}
        }
        nix_config = env.get("NIX_CONFIG", "").rstrip()
        env["NIX_CONFIG"] = "\n".join(
            line
            for line in (
                nix_config,
                "access-tokens = github.com=",
            )
            if line
        )
        list_proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "--access-tokens",
                "",
                "profile",
                "list",
                "--json",
                "--extra-experimental-features",
                "nix-command",
                "--extra-experimental-features",
                "flakes",
                "--profile",
                str(self.install_root),
            ],
            env=env,
            timeout=timeout,
            quiet=True,
        )
        profile_elements = [profile_element]
        if list_proc.returncode == 0 and list_proc.stdout.strip():
            profile_elements = [
                element_name
                for element_name in json.loads(list_proc.stdout).get("elements", {})
                if element_name == profile_element
                or (
                    element_name.startswith(f"{profile_element}-")
                    and element_name.removeprefix(f"{profile_element}-").isdigit()
                )
            ] or [profile_element]

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "--access-tokens",
                "",
                "profile",
                "remove",
                "--extra-experimental-features",
                "nix-command",
                "--extra-experimental-features",
                "flakes",
                "--profile",
                str(self.install_root),
                *profile_elements,
            ],
            env=env,
            timeout=timeout,
        )
        proc_output = format_subprocess_output(proc.stdout, proc.stderr)
        systemctl_bin = next(
            (
                str(path)
                for path in (Path("/usr/bin/systemctl"), Path("/bin/systemctl"))
                if path.is_file()
            ),
            None,
        )
        if (
            proc.returncode not in (0, 1)
            and os.uname().sysname == "Linux"
            and systemctl_bin is not None
            and Path("/run/systemd/system").is_dir()
            and (
                "cannot connect to socket at '/nix/var/nix/daemon-socket/socket'"
                in proc_output
                or "opening a connection to remote store 'daemon' previously failed"
                in proc_output
            )
        ):
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "daemon-reload"],
                timeout=timeout,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "reset-failed", "nix-daemon.socket"],
                timeout=timeout,
                quiet=True,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "reset-failed", "nix-daemon.service"],
                timeout=timeout,
                quiet=True,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "start", "nix-daemon.socket"],
                timeout=timeout,
                quiet=True,
            )
            self.exec(
                bin_name="sudo",
                cmd=[systemctl_bin, "start", "nix-daemon.service"],
                timeout=timeout,
                quiet=True,
            )
            proc = self.exec(
                bin_name=installer_bin,
                cmd=[
                    "--access-tokens",
                    "",
                    "profile",
                    "remove",
                    "--extra-experimental-features",
                    "nix-command",
                    "--extra-experimental-features",
                    "flakes",
                    "--profile",
                    str(self.install_root),
                    *profile_elements,
                ],
                env=env,
                timeout=timeout,
            )
        if proc.returncode not in (0, 1):
            self._raise_proc_error("uninstall", profile_elements, proc)

        return True

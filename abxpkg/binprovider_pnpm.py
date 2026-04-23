#!/usr/bin/env python3

__package__ = "abxpkg"

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Self

from platformdirs import user_cache_path
from pydantic import Field, TypeAdapter, computed_field, model_validator

from .binary import Binary
from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abxpkg_install_root_default,
    bin_abspath,
)
from .binprovider import (
    BinProvider,
    EnvProvider,
    env_flag_is_true,
    log_method_call,
    remap_kwargs,
)
from .logging import format_subprocess_output
from .semver import SemVer


USER_CACHE_PATH = user_cache_path("pnpm", "abxpkg")


class PnpmProvider(BinProvider):
    """Standalone pnpm package manager provider.

    Shells out to ``pnpm`` directly. ``minimumReleaseAge`` is enforced via
    ``--config.minimumReleaseAge=<minutes>`` (pnpm 10.16+).
    """

    name: BinProviderName = "pnpm"
    _log_emoji = "📦"
    INSTALLER_BIN: BinName = "pnpm"

    PATH: PATHStr = ""  # Starts empty; setup_PATH() lazily uses install_root/bin_dir only, or PNPM_HOME in global mode.
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABXPKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABXPKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    # None = -g global, otherwise it's a path.
    # Default: ABXPKG_PNPM_ROOT > ABXPKG_LIB_DIR/pnpm > None.
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("pnpm"),
        validation_alias="pnpm_prefix",
    )
    # detect_euid_to_use() fills this with ``<install_root>/node_modules/.bin`` in managed
    # mode; global mode leaves it unset and exec/setup_PATH() fall back to PNPM_HOME.
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        env: dict[str, str] = {
            "PNPM_HOME": str(
                self.bin_dir
                if self.bin_dir
                else (
                    Path(os.environ["PNPM_HOME"])
                    if os.environ.get("PNPM_HOME")
                    else self.cache_dir / "pnpm-home"
                ),
            ),
        }
        if self.install_root:
            node_modules_dir = str(self.install_root / "node_modules")
            env["NODE_MODULES_DIR"] = node_modules_dir
            env["NODE_MODULE_DIR"] = node_modules_dir
            env["NODE_PATH"] = ":" + node_modules_dir
        return env

    def get_cache_info(
        self,
        bin_name: BinName,
        abspath: HostBinPath,
    ) -> dict[str, list[Path]] | None:
        cache_info = super().get_cache_info(bin_name, abspath)
        if cache_info is None or self.install_root is None:
            return cache_info

        install_args = self.get_install_args(str(bin_name), quiet=True) or [
            str(bin_name),
        ]
        main_package = install_args[0]
        package = (
            "@" + main_package[1:].split("@", 1)[0]
            if main_package.startswith("@")
            else main_package.split("@", 1)[0]
        )
        package_json = self.install_root / "node_modules" / package / "package.json"
        if package_json.exists():
            cache_info["fingerprint_paths"].append(package_json)
        return cache_info

    def supports_min_release_age(self, action, no_cache: bool = False) -> bool:
        if action not in ("install", "update"):
            return False
        threshold = SemVer.parse("10.16.0")
        try:
            installer = self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return False
        version = installer.loaded_version if installer else None
        return bool(version and threshold and version >= threshold)

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        return action in ("install", "update")

    def default_install_args_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> InstallArgs:
        if str(bin_name) == "puppeteer":
            return ("puppeteer", "@puppeteer/browsers")
        if str(bin_name) == "puppeteer-browsers":
            return ("@puppeteer/browsers",)
        return TypeAdapter(InstallArgs).validate_python(
            super().default_install_args_handler(bin_name, **context)
            or [str(bin_name)],
        )

    @computed_field
    @property
    def is_valid(self) -> bool:
        return super().is_valid

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Derive pnpm's managed node_modules/.bin dir from install_root."""
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "node_modules" / ".bin"
        return self

    @property
    def cache_dir(self) -> Path:
        """Return the writable pnpm store dir, falling back to a temp dir if needed."""
        default_cache_dir = Path(USER_CACHE_PATH)
        if self._ensure_writable_cache_dir(default_cache_dir):
            return default_cache_dir
        return Path(tempfile.gettempdir()) / f"abxpkg-pnpm-store-{os.getuid()}"

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from install_root/bin_dir, or PNPM_HOME in global mode."""
        if self.bin_dir:
            self.PATH = self._merge_PATH(self.bin_dir)
        else:
            # In global mode, pnpm puts shims under PNPM_HOME (from env, or
            # ``<cache_dir>/pnpm-home`` — the same fallback exec() uses).
            pnpm_home = os.environ.get("PNPM_HOME") or str(
                self.cache_dir / "pnpm-home",
            )
            self.PATH = self._merge_PATH(pnpm_home, PATH=self.PATH)
        super().setup_PATH(no_cache=no_cache)

    def INSTALLER_BINARY(self, no_cache: bool = False):
        from . import DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

        loaded = super().INSTALLER_BINARY(no_cache=no_cache)
        raw_provider_names = os.environ.get("ABXPKG_BINPROVIDERS")
        selected_provider_names = (
            [provider_name.strip() for provider_name in raw_provider_names.split(",")]
            if raw_provider_names
            else list(DEFAULT_PROVIDER_NAMES)
        )
        dependency_providers = [
            EnvProvider(install_root=None, bin_dir=None)
            if provider_name == "env"
            else PROVIDER_CLASS_BY_NAME[provider_name]()
            for provider_name in selected_provider_names
            if provider_name
            and provider_name in PROVIDER_CLASS_BY_NAME
            and provider_name != self.name
        ]
        node_loaded = (
            Binary(
                name="node",
                binproviders=dependency_providers,
            ).load(no_cache=no_cache)
            if dependency_providers
            else None
        )
        if (
            node_loaded
            and node_loaded.loaded_abspath
            and node_loaded.loaded_version
            and node_loaded.loaded_sha256
        ):
            self.write_cached_binary(
                "node",
                node_loaded.loaded_abspath,
                node_loaded.loaded_version,
                node_loaded.loaded_sha256,
                resolved_provider_name=(
                    node_loaded.loaded_binprovider.name
                    if node_loaded.loaded_binprovider is not None
                    else self.name
                ),
                cache_kind="dependency",
            )
        return loaded

    @log_method_call(include_result=True)
    def exec(
        self,
        bin_name,
        cmd=(),
        cwd: Path | str = ".",
        quiet=False,
        should_log_command: bool = True,
        **kwargs,
    ):
        # pnpm REQUIRES PNPM_HOME to exist for global installs to work.
        pnpm_home = Path(self.ENV["PNPM_HOME"])
        pnpm_home.mkdir(parents=True, exist_ok=True)
        return super().exec(
            bin_name=bin_name,
            cmd=cmd,
            cwd=cwd,
            quiet=quiet,
            should_log_command=should_log_command,
            **kwargs,
        )

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.install_root,),
                preserve_root=True,
            )
        self._ensure_writable_cache_dir(self.cache_dir)
        if self.bin_dir:
            self.bin_dir.mkdir(parents=True, exist_ok=True)

    def _linked_bin_path(self, bin_name: BinName | HostBinPath) -> Path | None:
        """Return the managed shim path for a pnpm-installed executable, if any."""
        if self.bin_dir is None:
            return None
        return self.bin_dir / str(bin_name)

    def _refresh_bin_link(
        self,
        bin_name: BinName | HostBinPath,
        target: HostBinPath,
    ) -> HostBinPath:
        """Recreate the managed shim symlink pointing at the resolved pnpm executable."""
        link_path = self._linked_bin_path(bin_name)
        assert link_path is not None, "_refresh_bin_link requires bin_dir to be set"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        # Idempotent refresh: skip when shim already points at target.
        # Rewriting on every load() bumps mtime and churns the inode,
        # which invalidates fingerprint caches unnecessarily.
        if link_path.is_symlink():
            try:
                if link_path.readlink() == Path(target):
                    return TypeAdapter(HostBinPath).validate_python(link_path)
            except OSError:
                pass
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink(missing_ok=True)
        link_path.symlink_to(target)
        return TypeAdapter(HostBinPath).validate_python(link_path)

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
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
        min_release_age = 7.0 if min_release_age is None else min_release_age
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}@>={min_version}"
                if arg
                and not arg.startswith(("-", ".", "/"))
                and ":" not in arg.split("/")[0]
                and "@" not in arg.split("/")[-1]
                else arg
                for arg in install_args
            ]
        if any(
            arg == "--ignore-scripts" for arg in ("--loglevel=error", *install_args)
        ):
            postinstall_scripts = False

        cmd: list[str] = [
            "add",
            "--loglevel=error",
            f"--store-dir={self.cache_dir}",
        ]
        if not postinstall_scripts:
            cmd.append("--ignore-scripts")
        else:
            # pnpm 10+ blocks ALL postinstall scripts unless explicitly allowed.
            cmd.append("--config.dangerouslyAllowAllBuilds=true")
        if (
            min_release_age is not None
            and min_release_age > 0
            and not any(
                arg == "--config.minimumReleaseAge"
                or arg.startswith("--config.minimumReleaseAge=")
                for arg in ("--loglevel=error", *install_args)
            )
        ):
            cmd.append(
                f"--config.minimumReleaseAge={max(int(min_release_age * 24 * 60), 1)}",
            )
        cmd.append(f"--dir={self.install_root}" if self.install_root else "--global")
        cmd.extend(install_args)

        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
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
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
        min_release_age = 7.0 if min_release_age is None else min_release_age
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}@>={min_version}"
                if arg
                and not arg.startswith(("-", ".", "/"))
                and ":" not in arg.split("/")[0]
                and "@" not in arg.split("/")[-1]
                else arg
                for arg in install_args
            ]
        if any(
            arg == "--ignore-scripts" for arg in ("--loglevel=error", *install_args)
        ):
            postinstall_scripts = False

        cmd: list[str] = [
            "add" if min_version is not None else "update",
            "--loglevel=error",
            f"--store-dir={self.cache_dir}",
        ]
        if not postinstall_scripts:
            cmd.append("--ignore-scripts")
        else:
            cmd.append("--config.dangerouslyAllowAllBuilds=true")
        if (
            min_release_age is not None
            and min_release_age > 0
            and not any(
                arg == "--config.minimumReleaseAge"
                or arg.startswith("--config.minimumReleaseAge=")
                for arg in ("--loglevel=error", *install_args)
            )
        ):
            cmd.append(
                f"--config.minimumReleaseAge={max(int(min_release_age * 24 * 60), 1)}",
            )
        cmd.append(f"--dir={self.install_root}" if self.install_root else "--global")
        cmd.extend(install_args)

        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
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
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        install_args = install_args or self.get_install_args(bin_name)
        if str(bin_name) == "puppeteer" and tuple(install_args) == (
            "puppeteer",
            "@puppeteer/browsers",
        ):
            install_args = ["puppeteer"]

        # pnpm remove rejects --ignore-scripts and --config.minimumReleaseAge,
        # so don't pass either even if they were set as provider defaults.
        cmd: list[str] = [
            "remove",
            "--loglevel=error",
            f"--store-dir={self.cache_dir}",
        ]
        cmd.append(f"--dir={self.install_root}" if self.install_root else "--global")
        cmd.extend(install_args)

        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", install_args, proc)
        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except Exception:
            pass

        try:
            pnpm_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert pnpm_abspath
        except Exception:
            return None

        # Fallback: ask `pnpm view` for the package's bin entries and look
        # them up by name in our PATH.
        try:
            install_args = self.get_install_args(str(bin_name)) or [str(bin_name)]
            package_info = json.loads(
                self.exec(
                    bin_name=pnpm_abspath,
                    cmd=["view", "--json", install_args[0], "bin"],
                    timeout=self.version_timeout,
                    quiet=True,
                ).stdout.strip(),
            )
            alt_bin_names = (
                package_info.get("bin", package_info)
                if isinstance(package_info, dict)
                else {}
            ).keys()
            for alt_bin_name in alt_bin_names:
                abspath = bin_abspath(
                    alt_bin_name,
                    PATH=str(self.bin_dir) if self.bin_dir else self.PATH,
                )
                if abspath:
                    direct_abspath = TypeAdapter(HostBinPath).validate_python(abspath)
                    if str(alt_bin_name) == str(bin_name) or self.bin_dir is None:
                        return direct_abspath
                    return self._refresh_bin_link(bin_name, direct_abspath)
        except Exception:
            pass
        return None

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> SemVer | None:
        try:
            version = self._version_from_exec(
                bin_name,
                abspath=abspath,
                timeout=timeout,
            )
            if version:
                return version
        except ValueError:
            pass

        try:
            pnpm_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert pnpm_abspath
        except Exception:
            return None

        # Fallback: ask `pnpm ls --json` for the installed version of the
        # main package, and finally fall back to reading its package.json.
        install_args = self.get_install_args(str(bin_name), **context) or [
            str(bin_name),
        ]
        main_package = install_args[0]
        package = (
            "@" + main_package[1:].split("@", 1)[0]
            if main_package.startswith("@")
            else main_package.split("@", 1)[0]
        )
        try:
            json_output = self.exec(
                bin_name=pnpm_abspath,
                cmd=[
                    "ls",
                    f"--dir={self.install_root}" if self.install_root else "--global",
                    "--depth=0",
                    "--json",
                    package,
                ],
                timeout=timeout,
                quiet=True,
            ).stdout.strip()
            listing = json.loads(json_output)
            if isinstance(listing, list):
                listing = listing[0] if listing else {}
            return listing["dependencies"][package]["version"]
        except Exception:
            pass

        try:
            modules_dir = Path(
                self.exec(
                    bin_name=pnpm_abspath,
                    cmd=(
                        ["root", f"--dir={self.install_root}"]
                        if self.install_root
                        else ["root", "--global"]
                    ),
                    timeout=timeout,
                    quiet=True,
                ).stdout.strip(),
            )
            return json.loads((modules_dir / package / "package.json").read_text())[
                "version"
            ]
        except Exception:
            return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_pnpm.py load zx
    # ./binprovider_pnpm.py install zx
    result = pnpm = PnpmProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(pnpm, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)

#!/usr/bin/env python3

__package__ = "abxpkg"

import json
import os
import sys
from pathlib import Path
from typing import Self

from platformdirs import user_cache_path
from pydantic import Field, TypeAdapter, computed_field, model_validator

from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abxpkg_install_root_default,
)
from .binprovider import BinProvider, env_flag_is_true, log_method_call, remap_kwargs
from .logging import format_subprocess_output
from .semver import SemVer


USER_CACHE_PATH = user_cache_path("bun", "abxpkg")


class BunProvider(BinProvider):
    """Bun package manager + runtime provider.

    ``bun_prefix`` mirrors the ``BUN_INSTALL`` environment variable: when
    set, ``bun add -g`` lays out binaries under ``<bun_prefix>/bin`` and
    stores its global ``node_modules`` under ``<bun_prefix>/install/global``.

    Security:
    - ``--ignore-scripts`` for ``postinstall_scripts=False``
    - ``--minimum-release-age=<seconds>`` for ``min_release_age`` (Bun 1.3+)
    """

    name: BinProviderName = "bun"
    _log_emoji = "🥖"
    INSTALLER_BIN: BinName = "bun"

    PATH: PATHStr = ""  # Starts empty; setup_PATH() lazily uses install_root/bin_dir only, or Bun's global bin dir from BUN_INSTALL/~/.bun in ambient mode.
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABXPKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABXPKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    # None = inherit BUN_INSTALL / ~/.bun, otherwise use install_root as the prefix.
    # Default: ABXPKG_BUN_ROOT > ABXPKG_LIB_DIR/bun > None.
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("bun"),
        validation_alias="bun_prefix",
    )
    # detect_euid_to_use() fills this from install_root/bin in managed mode; ambient mode
    # leaves it unset so setup_PATH() falls back to Bun's own global bin location.
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        node_modules_dir = str(
            self.install_root / "install" / "global" / "node_modules",
        )
        return {
            "NODE_MODULES_DIR": node_modules_dir,
            "NODE_MODULE_DIR": node_modules_dir,
            "NODE_PATH": os.pathsep + node_modules_dir,
            "BUN_INSTALL": str(self.install_root),
        }

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
        package_json = (
            self.install_root
            / "install"
            / "global"
            / "node_modules"
            / package
            / "package.json"
        )
        if package_json.exists():
            cache_info["fingerprint_paths"].append(package_json)
        return cache_info

    def supports_min_release_age(self, action, no_cache: bool = False) -> bool:
        if action not in ("install", "update"):
            return False
        threshold = SemVer.parse("1.3.0")
        try:
            installer = self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return False
        version = installer.loaded_version if installer else None
        return bool(version and threshold and version >= threshold)

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        return action in ("install", "update")

    @staticmethod
    def _has_cli_flag(args: InstallArgs, *flags: str) -> bool:
        """Return True when any explicit bun CLI flag is already present in install_args."""
        return any(
            arg == flag or arg.startswith(f"{flag}=") for arg in args for flag in flags
        )

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
        """Derive bun's managed bin_dir from install_root when running in managed mode."""
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "bin"
        return self

    @property
    def cache_dir(self) -> Path:
        """Return Bun's shared cache dir used for downloads and package metadata."""
        return Path(USER_CACHE_PATH)

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from install_root/bin_dir, or Bun's ambient global bin dir."""
        if self.bin_dir:
            self.PATH = self._merge_PATH(self.bin_dir)
        else:
            default_bun = (
                Path(os.environ.get("BUN_INSTALL") or (Path("~").expanduser() / ".bun"))
                / "bin"
            )
            self.PATH = self._merge_PATH(default_bun, PATH=self.PATH)
        super().setup_PATH(no_cache=no_cache)

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
        # Ensure install_root exists before bun tries to populate it.
        if self.install_root:
            self.install_root.mkdir(parents=True, exist_ok=True)
            assert self.bin_dir is not None
            self.bin_dir.mkdir(parents=True, exist_ok=True)
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
        if self.install_root:
            assert self.bin_dir is not None
            self.bin_dir.mkdir(parents=True, exist_ok=True)
            (self.install_root / "install").mkdir(parents=True, exist_ok=True)

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
        if any(arg == "--ignore-scripts" for arg in install_args):
            postinstall_scripts = False

        cache_arg = (
            "--no-cache"
            if no_cache or not self._ensure_writable_cache_dir(self.cache_dir)
            else f"--cache-dir={self.cache_dir}"
        )
        cmd: list[str] = ["add", cache_arg, "-g"]
        if not postinstall_scripts:
            cmd.append("--ignore-scripts")
        elif not self._has_cli_flag(
            install_args,
            "--trust",
        ):
            # Bun does not run dependency lifecycle scripts by default.
            # ``--trust`` is required for packages like ``optipng-bin`` whose
            # executable is materialized by an install script.
            cmd.append("--trust")
        if (
            min_release_age is not None
            and min_release_age > 0
            and not self._has_cli_flag(
                install_args,
                "--minimum-release-age",
            )
        ):
            cmd.append(
                f"--minimum-release-age={max(int(min_release_age * 24 * 60 * 60), 1)}",
            )
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
        if any(arg == "--ignore-scripts" for arg in install_args):
            postinstall_scripts = False

        cache_arg = (
            "--no-cache"
            if no_cache or not self._ensure_writable_cache_dir(self.cache_dir)
            else f"--cache-dir={self.cache_dir}"
        )
        cmd: list[str] = ["update", cache_arg, "-g"]
        if not postinstall_scripts:
            cmd.append("--ignore-scripts")
        elif not self._has_cli_flag(
            install_args,
            "--trust",
        ):
            cmd.append("--trust")
        if (
            min_release_age is not None
            and min_release_age > 0
            and not self._has_cli_flag(
                install_args,
                "--minimum-release-age",
            )
        ):
            cmd.append(
                f"--minimum-release-age={max(int(min_release_age * 24 * 60 * 60), 1)}",
            )
        cmd.extend(install_args)

        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
        if proc.returncode != 0:
            # `bun update -g <pkg>` is rejected by some bun versions; fall
            # back to `bun add -g --force <pkg>` to refresh the global store.
            cmd = ["add", cache_arg, "-g", "--force"]
            if not postinstall_scripts:
                cmd.append("--ignore-scripts")
            elif not self._has_cli_flag(
                install_args,
                "--trust",
            ):
                cmd.append("--trust")
            if (
                min_release_age is not None
                and min_release_age > 0
                and not self._has_cli_flag(
                    install_args,
                    "--minimum-release-age",
                )
            ):
                cmd.append(
                    f"--minimum-release-age={max(int(min_release_age * 24 * 60 * 60), 1)}",
                )
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

        proc = self.exec(
            bin_name=installer_bin,
            cmd=["remove", "-g", *install_args],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", install_args, proc)
        return True

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath=None,
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
            self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return None

        # Fallback: read the package.json from bun's global node_modules.
        install_args = self.get_install_args(str(bin_name), **context) or [
            str(bin_name),
        ]
        main_package = install_args[0]
        package = (
            "@" + main_package[1:].split("@", 1)[0]
            if main_package.startswith("@")
            else main_package.split("@", 1)[0]
        )
        global_root = (
            (self.install_root / "install" / "global")
            if self.install_root
            else Path(
                os.environ.get("BUN_INSTALL") or (Path("~").expanduser() / ".bun"),
            )
            / "install"
            / "global"
        )
        package_json = global_root / "node_modules" / package / "package.json"
        if package_json.exists():
            try:
                return json.loads(package_json.read_text())["version"]
            except Exception:
                return None
        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_bun.py load zx
    # ./binprovider_bun.py install zx
    result = bun = BunProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(bun, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)

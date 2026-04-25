#!/usr/bin/env python
__package__ = "abxpkg"

import sys
import time

from pydantic import TypeAdapter, model_validator
from typing import Self

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .semver import SemVer
from .binprovider import BinProvider, EnvProvider, remap_kwargs
from .logging import format_subprocess_output

_LAST_UPDATE_CHECK = None
UPDATE_CHECK_INTERVAL = 60 * 60 * 24  # 1 day


class AptProvider(BinProvider):
    name: BinProviderName = "apt"
    _log_emoji = "🐧"
    INSTALLER_BIN: BinName = "apt-get"

    PATH: PATHStr = ""  # Starts empty; setup_PATH() discovers package runtime bin dirs via dpkg and replaces PATH with those dirs.
    euid: int | None = (
        0  # Import-time default that forces every apt subprocess through the root/sudo execution path.
    )

    @model_validator(mode="after")
    def add_package_aliases(self) -> Self:
        self.overrides["gem"] = {
            **self.overrides.get("gem", {}),
            "install_args": ["ruby"],
        }
        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from dpkg-discovered package runtime bin dirs, not from apt-get itself."""
        # Rebuild PATH on first use, when the caller forces no_cache, or when
        # PATH is still empty — the last case covers the "INSTALLER_BINARY was
        # resolved out-of-band (hook preflight etc.), so _INSTALLER_BINARY is
        # non-None but self.PATH was never populated" race.
        if (
            no_cache
            or not self.PATH
            or self._INSTALLER_BINARY is None
            or self._INSTALLER_BINARY.loaded_abspath is None
        ):
            dpkg_binary = EnvProvider().load("dpkg")
            apt_binary = None
            try:
                apt_binary = self.INSTALLER_BINARY(no_cache=no_cache)
            except Exception:
                apt_binary = None
            dpkg_abspath = (
                dpkg_binary.loaded_abspath
                if dpkg_binary and dpkg_binary.loaded_abspath
                else None
            )
            apt_abspath = (
                apt_binary.loaded_abspath
                if apt_binary and apt_binary.loaded_abspath
                else None
            )
            if not dpkg_abspath or not apt_abspath:
                self.PATH = ""
            else:
                PATH = self.PATH
                dpkg_install_dirs = (
                    self.exec(
                        bin_name=dpkg_abspath,
                        cmd=["-L", "bash"],
                        quiet=True,
                        should_log_command=False,
                    )
                    .stdout.strip()
                    .split("\n")
                )
                dpkg_bin_dirs = [
                    path for path in dpkg_install_dirs if path.endswith("/bin")
                ]
                for bin_dir in dpkg_bin_dirs:
                    if str(bin_dir) not in PATH:
                        PATH = ":".join([str(bin_dir), *PATH.split(":")])
                self.PATH = TypeAdapter(PATHStr).validate_python(PATH)
        super().setup_PATH(no_cache=no_cache)

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        global _LAST_UPDATE_CHECK

        install_args = install_args or self.get_install_args(bin_name)

        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        dpkg_binary = EnvProvider().load("dpkg")
        dpkg_abspath = (
            dpkg_binary.loaded_abspath
            if dpkg_binary and dpkg_binary.loaded_abspath
            else None
        )
        assert installer_bin
        if not dpkg_abspath:
            raise Exception(
                f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}",
            )

        # print(f'[*] {self.__class__.__name__}: Installing {bin_name}: {self.INSTALLER_BIN} install {install_args}')

        if (
            not _LAST_UPDATE_CHECK
            or (time.time() - _LAST_UPDATE_CHECK) > UPDATE_CHECK_INTERVAL
        ):
            # only update if we haven't checked in the last day
            self.exec(
                bin_name=installer_bin,
                cmd=["update", "-qq"],
                timeout=timeout,
            )
            _LAST_UPDATE_CHECK = time.time()

        proc = self.exec(
            bin_name=installer_bin,
            cmd=["install", "-y", "-qq", "--no-install-recommends", *install_args],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", install_args, proc)
        return (
            format_subprocess_output(proc.stdout, proc.stderr)
            or f"Installed {install_args} successfully."
        )

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        global _LAST_UPDATE_CHECK

        install_args = install_args or self.get_install_args(bin_name)

        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        dpkg_binary = EnvProvider().load("dpkg")
        dpkg_abspath = (
            dpkg_binary.loaded_abspath
            if dpkg_binary and dpkg_binary.loaded_abspath
            else None
        )
        assert installer_bin
        if not dpkg_abspath:
            raise Exception(
                f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}",
            )

        if (
            not _LAST_UPDATE_CHECK
            or (time.time() - _LAST_UPDATE_CHECK) > UPDATE_CHECK_INTERVAL
        ):
            self.exec(
                bin_name=installer_bin,
                cmd=["update", "-qq"],
                timeout=timeout,
            )
            _LAST_UPDATE_CHECK = time.time()

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "install",
                "--only-upgrade",
                "-y",
                "-qq",
                "--no-install-recommends",
                *install_args,
            ],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("update", install_args, proc)
        return (
            format_subprocess_output(proc.stdout, proc.stderr)
            or f"Updated {install_args} successfully."
        )

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> bool:
        install_args = install_args or self.get_install_args(bin_name)

        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        dpkg_binary = EnvProvider().load("dpkg")
        dpkg_abspath = (
            dpkg_binary.loaded_abspath
            if dpkg_binary and dpkg_binary.loaded_abspath
            else None
        )
        assert installer_bin
        if not dpkg_abspath:
            raise Exception(
                f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}",
            )

        proc = self.exec(
            bin_name=installer_bin,
            cmd=["remove", "-y", "-qq", *install_args],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", install_args, proc)

        return True


if __name__ == "__main__":
    result = apt = AptProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(apt, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)

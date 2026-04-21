#!/usr/bin/env python3
__package__ = "abxpkg"

import os
import sys
import time
import platform
from pathlib import Path

from pydantic import Field, TypeAdapter, computed_field

from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    HostBinPath,
    abxpkg_install_root_default,
    bin_abspath,
)
from .semver import SemVer
from .binprovider import BinProvider, env_flag_is_true, remap_kwargs
from .logging import format_subprocess_output

OS = platform.system().lower()

NEW_MACOS_DIR = Path("/opt/homebrew/bin")
OLD_MACOS_DIR = Path("/usr/local/bin")
DEFAULT_MACOS_DIR = NEW_MACOS_DIR if platform.machine() == "arm64" else OLD_MACOS_DIR
DEFAULT_LINUX_DIR = Path("/home/linuxbrew/.linuxbrew/bin")
GUESSED_BREW_PREFIX = (
    DEFAULT_MACOS_DIR.parent if OS == "darwin" else DEFAULT_LINUX_DIR.parent
)

_LAST_UPDATE_CHECK = None
UPDATE_CHECK_INTERVAL = 60 * 60 * 24  # 1 day


class BrewProvider(BinProvider):
    name: BinProviderName = "brew"
    _log_emoji = "🍺"
    INSTALLER_BIN: BinName = "brew"

    PATH: PATHStr = f"{DEFAULT_LINUX_DIR}:{NEW_MACOS_DIR}:{OLD_MACOS_DIR}"  # Seeded with common brew bin roots; setup_PATH() lazily normalizes it to the resolved brew/runtime bin dirs.
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABXPKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )

    install_root: Path | None = Field(
        default_factory=lambda: (
            abxpkg_install_root_default("brew") or GUESSED_BREW_PREFIX
        ),
        validation_alias="brew_prefix",
    )
    # Starts unset so setup_PATH() can infer the real brew prefix first, then normalizes
    # to ``<install_root>/bin`` for the shim/link refresh paths used by load().
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        return {
            "HOMEBREW_PREFIX": str(self.install_root),
            "HOMEBREW_CELLAR": str(self.install_root / "Cellar"),
        }

    def supports_min_release_age(self, action, no_cache: bool = False) -> bool:
        return False

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        return action == "install"

    def _brew_prefixes(self, no_cache: bool = False) -> list[Path]:
        """Collect candidate Homebrew prefixes from the installer binary and current PATH."""
        prefixes: list[Path] = []
        seen: set[str] = set()

        def add_prefix(bin_dir_or_prefix: Path) -> None:
            prefix = (
                bin_dir_or_prefix.parent
                if bin_dir_or_prefix.name == "bin"
                else bin_dir_or_prefix
            )
            prefix_str = str(prefix)
            if prefix_str in seen:
                return
            seen.add(prefix_str)
            prefixes.append(prefix)

        installer_binary = None if no_cache else self._INSTALLER_BINARY
        if installer_binary is None or installer_binary.loaded_abspath is None:
            try:
                installer_binary = self.INSTALLER_BINARY(no_cache=no_cache)
            except Exception:
                installer_binary = None
        installer_abspath = (
            installer_binary.loaded_abspath
            if installer_binary and installer_binary.loaded_abspath
            else None
        )
        if installer_abspath:
            add_prefix(Path(installer_abspath).parent)

        for bin_dir in self.PATH.split(":"):
            if not bin_dir:
                continue
            add_prefix(Path(bin_dir))

        return prefixes

    def _brew_search_paths(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
    ) -> PATHStr:
        """Build the brew-specific bin search PATH for the requested formula names."""
        package_names = [
            package
            for package in self.get_install_args(str(bin_name), quiet=True)
            if isinstance(package, str) and package and not package.startswith("-")
        ] or [str(bin_name)]

        search_paths: list[str] = []
        seen: set[str] = set()

        def add_path(path: Path) -> None:
            path_str = str(path)
            if path_str in seen:
                return
            seen.add(path_str)
            search_paths.append(path_str)

        for prefix in self._brew_prefixes(no_cache=no_cache):
            for package in package_names:
                add_path(prefix / "opt" / package / "bin")
                add_path(prefix / "opt" / package / "libexec" / "bin")
                for cellar_bin in (prefix / "Cellar" / package).glob("*/bin"):
                    add_path(cellar_bin)
                for cellar_bin in (prefix / "Cellar" / package).glob("*/libexec/bin"):
                    add_path(cellar_bin)

        for bin_dir in self.PATH.split(":"):
            if bin_dir:
                add_path(Path(bin_dir))

        return TypeAdapter(PATHStr).validate_python(search_paths)

    def _linked_bin_path(self, bin_name: BinName | HostBinPath) -> Path | None:
        """Return the managed shim path for a loaded brew binary, if shimming is enabled."""
        if self.bin_dir is None:
            return None
        return self.bin_dir / str(bin_name)

    def _refresh_bin_link(
        self,
        bin_name: BinName | HostBinPath,
        target: HostBinPath,
    ) -> HostBinPath:
        """Repoint the managed shim path at the latest resolved brew binary target."""
        link_path = self._linked_bin_path(bin_name)
        assert link_path is not None, "_refresh_bin_link requires bin_dir to be set"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        # When running as root but brew itself drops to its owner uid via
        # ``self.exec``, we also need the managed shim dir (and its parents)
        # traversable by that target uid so version probes / load() calls from
        # the dropped-privilege subprocess can reach the symlink.
        current_euid = os.geteuid()
        target_uid = self.EUID
        if current_euid == 0 and target_uid not in (0, current_euid):
            try:
                pw_record = self.get_pw_record(target_uid)
                os.chown(link_path.parent, target_uid, pw_record.pw_gid)
            except Exception:
                pass
            walk_path = link_path.parent
            while walk_path != walk_path.parent:
                try:
                    mode = walk_path.stat().st_mode
                    walk_path.chmod(mode | 0o055)
                except Exception:
                    break
                walk_path = walk_path.parent
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink(missing_ok=True)
        link_path.symlink_to(target)
        return TypeAdapter(HostBinPath).validate_python(link_path)

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from the resolved brew prefix and known runtime brew bin dirs."""
        # Rebuild PATH on first use, when the caller forces no_cache, or when
        # PATH is still empty — the last case covers provider copies that
        # inherited a resolved ``_INSTALLER_BINARY`` but an unset ``PATH``.
        if (
            no_cache
            or not self.PATH
            or self._INSTALLER_BINARY is None
            or self._INSTALLER_BINARY.loaded_abspath is None
        ):
            install_root = self.install_root
            assert install_root is not None
            if self.bin_dir is None:
                self.bin_dir = install_root / "bin"

            brew_binary = None
            try:
                brew_binary = self.INSTALLER_BINARY(no_cache=no_cache)
            except Exception:
                brew_binary = None
            brew_abspath = (
                brew_binary.loaded_abspath
                if brew_binary and brew_binary.loaded_abspath
                else None
            )
            if not brew_abspath:
                self.PATH = ""
            else:
                bin_dirs: list[str] = []
                seen: set[str] = set()

                def add_bin_dir(path: Path) -> None:
                    path_str = str(path)
                    if path_str in seen:
                        return
                    seen.add(path_str)
                    bin_dirs.append(path_str)

                add_bin_dir(Path(brew_abspath).parent)

                if OS == "darwin":
                    for path in (DEFAULT_MACOS_DIR, NEW_MACOS_DIR, OLD_MACOS_DIR):
                        if os.path.isdir(path) and os.access(path, os.R_OK):
                            add_bin_dir(path)
                else:
                    if os.path.isdir(DEFAULT_LINUX_DIR) and os.access(
                        DEFAULT_LINUX_DIR,
                        os.R_OK,
                    ):
                        add_bin_dir(DEFAULT_LINUX_DIR)

                if self.install_root == GUESSED_BREW_PREFIX:
                    self.install_root = self._brew_prefixes(no_cache=no_cache)[0]
                    self.bin_dir = self.install_root / "bin"
                self.PATH = TypeAdapter(PATHStr).validate_python(bin_dirs)
        super().setup_PATH(no_cache=no_cache)

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
        global _LAST_UPDATE_CHECK

        install_args = install_args or self.get_install_args(bin_name)

        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        # print(f'[*] {self.__class__.__name__}: Installing {bin_name}: {self.INSTALLER_BIN} install {install_args}')

        assert postinstall_scripts is not None
        if (
            not _LAST_UPDATE_CHECK
            or (time.time() - _LAST_UPDATE_CHECK) > UPDATE_CHECK_INTERVAL
        ):
            # only update if we haven't checked in the last day
            self.exec(
                bin_name=installer_bin,
                cmd=["update"],
                timeout=timeout,
            )
            _LAST_UPDATE_CHECK = time.time()

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "install",
                *(
                    ["--skip-post-install"]
                    if (
                        not postinstall_scripts
                        and not any(
                            arg.startswith("--skip-post-install")
                            for arg in install_args
                        )
                    )
                    else []
                ),
                *install_args,
            ],
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
        global _LAST_UPDATE_CHECK

        install_args = install_args or self.get_install_args(bin_name)

        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        if (
            not _LAST_UPDATE_CHECK
            or (time.time() - _LAST_UPDATE_CHECK) > UPDATE_CHECK_INTERVAL
        ):
            self.exec(
                bin_name=installer_bin,
                cmd=["update"],
                timeout=timeout,
            )
            _LAST_UPDATE_CHECK = time.time()

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "upgrade",
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

        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        proc = self.exec(
            bin_name=installer_bin,
            cmd=["uninstall", *install_args],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", install_args, proc)

        linked_bin = self._linked_bin_path(bin_name)
        if linked_bin is not None:
            linked_bin.unlink(missing_ok=True)

        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        # Installer binary: delegate to base class (avoids recursion via _brew_search_paths)
        if str(bin_name) == self.INSTALLER_BIN:
            try:
                abspath = super().default_abspath_handler(
                    bin_name,
                    no_cache=no_cache,
                    **context,
                )
                if abspath:
                    return TypeAdapter(HostBinPath).validate_python(abspath)
            except Exception:
                return None
            return None

        if not self.PATH:
            return None

        linked_bin = self._linked_bin_path(bin_name)
        if linked_bin is not None:
            linked_abspath = bin_abspath(bin_name, PATH=str(self.bin_dir))
            if linked_abspath:
                return linked_abspath

        search_paths = self._brew_search_paths(bin_name, no_cache=no_cache)
        abspath = bin_abspath(bin_name, PATH=search_paths)
        if abspath:
            if linked_bin is None or Path(abspath).parent == self.bin_dir:
                return abspath
            return self._refresh_bin_link(bin_name, abspath)

        try:
            brew_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert brew_abspath
        except Exception:
            return None

        for package in self.get_install_args(str(bin_name)) or [str(bin_name)]:
            try:
                paths = (
                    self.exec(
                        bin_name=brew_abspath,
                        cmd=["list", "--formula", package],
                        timeout=self.version_timeout,
                        quiet=True,
                    )
                    .stdout.strip()
                    .split("\n")
                )
                for path_str in paths:
                    path = Path(path_str.strip())
                    if path.name != str(bin_name):
                        continue
                    if path.is_file() and os.access(path, os.X_OK):
                        direct_abspath = bin_abspath(path)
                        if not direct_abspath:
                            continue
                        if linked_bin is None or direct_abspath.parent == self.bin_dir:
                            return direct_abspath
                        return self._refresh_bin_link(bin_name, direct_abspath)
            except Exception:
                pass

        # This code works but there's no need, the method above is much faster:

        # # try checking filesystem or using brew list to get the Cellar bin path (faster than brew info)
        # for package in (self.get_install_args(str(bin_name)) or [str(bin_name)]):
        #     try:
        #         paths = self.exec(bin_name=self._require_installer_bin(), cmd=[
        #             'list',
        #             '--formulae',
        #             package,
        #         ], timeout=self.version_timeout, quiet=True).stdout.strip().split('\n')
        #         # /opt/homebrew/Cellar/curl/8.10.1/bin/curl
        #         # /opt/homebrew/Cellar/curl/8.10.1/bin/curl-config
        #         # /opt/homebrew/Cellar/curl/8.10.1/include/curl/ (12 files)
        #         return [line for line in paths if '/Cellar/' in line and line.endswith(f'/bin/{bin_name}')][0].strip()
        #     except Exception:
        #         pass

        # # fallback to using brew info to get the Cellar bin path
        # for package in (self.get_install_args(str(bin_name)) or [str(bin_name)]):
        #     try:
        #         info_lines = self.exec(bin_name=self._require_installer_bin(), cmd=[
        #             'info',
        #             '--quiet',
        #             package,
        #         ], timeout=self.version_timeout, quiet=True).stdout.strip().split('\n')
        #         # /opt/homebrew/Cellar/curl/8.10.0 (530 files, 4MB)
        #         cellar_path = [line for line in info_lines if '/Cellar/' in line][0].rsplit(' (', 1)[0]
        #         abspath = bin_abspath(bin_name, PATH=f'{cellar_path}/bin')
        #         if abspath:
        #             return abspath
        #     except Exception:
        #         pass
        # return None

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> SemVer | None:
        # print(f'[*] {self.__class__.__name__}: Getting version for {bin_name}...')

        # shortcut: if we already have the Cellar abspath, extract the version from it
        if abspath and "/Cellar/" in str(abspath):
            # /opt/homebrew/Cellar/curl/8.10.1/bin/curl -> 8.10.1
            version = str(abspath).rsplit(f"/bin/{bin_name}", 1)[0].rsplit("/", 1)[-1]
            if version:
                parsed_version = SemVer.parse(version)
                if parsed_version:
                    return parsed_version

        # fallback to running $ <bin_name> --version
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
            brew_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert brew_abspath
        except Exception:
            return None

        # fallback to using brew list to get the package version (faster than brew info)
        for package in self.get_install_args(str(bin_name)) or [str(bin_name)]:
            try:
                paths = (
                    self.exec(
                        bin_name=brew_abspath,
                        cmd=[
                            "list",
                            "--formulae",
                            package,
                        ],
                        timeout=timeout,
                        quiet=True,
                    )
                    .stdout.strip()
                    .split("\n")
                )
                # /opt/homebrew/Cellar/curl/8.10.1/bin/curl
                cellar_abspath = [
                    line
                    for line in paths
                    if "/Cellar/" in line and line.rstrip("/").endswith(f"/{bin_name}")
                ][0].strip()
                path = Path(cellar_abspath)
                if "Cellar" in path.parts:
                    cellar_idx = path.parts.index("Cellar")
                    if len(path.parts) > cellar_idx + 2:
                        version = path.parts[cellar_idx + 2]
                        if version:
                            parsed_version = SemVer.parse(version)
                            if parsed_version:
                                return parsed_version
            except Exception:
                pass

        # fallback to using brew info to get the version (slowest method of all)
        install_args = self.get_install_args(str(bin_name)) or [str(bin_name)]
        main_package = install_args[0]  # assume first package in list is the main one
        try:
            version_str = (
                self.exec(
                    bin_name=brew_abspath,
                    cmd=[
                        "info",
                        "--quiet",
                        main_package,
                    ],
                    quiet=True,
                    timeout=timeout,
                )
                .stdout.strip()
                .split("\n")[0]
            )
            # ==> curl: stable 8.10.1 (bottled), HEAD [keg-only]
            return version_str
        except Exception:
            return None

        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_brew.py load yt-dlp
    # ./binprovider_brew.py install pip
    # ./binprovider_brew.py get_version pip
    # ./binprovider_brew.py get_abspath pip
    result = brew = BrewProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(brew, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)

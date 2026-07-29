#!/usr/bin/env python
__package__ = "abxpkg"

import fcntl
import os
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from pydantic import Field, TypeAdapter, model_validator
from typing import ClassVar, Self

from .base_types import BinProviderName, PATHStr, BinName, HostBinPath, InstallArgs
from .semver import SemVer
from .binprovider import BinProvider, EnvProvider, ShallowBinary, remap_kwargs
from .logging import format_subprocess_output

_LAST_UPDATE_CHECK = None
UPDATE_CHECK_INTERVAL = 60 * 60 * 24  # 1 day
APT_LOCK_PATH = Path("/tmp/abxpkg-apt.lock")
DEFAULT_APT_KEYRING_DIR = Path("/etc/apt/keyrings")
DEFAULT_APT_SOURCES_DIR = Path("/etc/apt/sources.list.d")
DEFAULT_APT_LISTS_DIR = Path("/var/lib/apt/lists")


class AptProvider(BinProvider):
    name: BinProviderName = "apt"
    _log_emoji = "🐧"
    INSTALLER_BIN: BinName = "apt-get"
    INSTALLER_BINPROVIDERS: ClassVar[tuple[BinProviderName, ...] | None] = ("env",)
    DEFAULT_SUPPORTED_PLATFORMS: ClassVar[tuple[str, ...] | None] = ("linux",)

    PATH: PATHStr = ""  # Starts empty; setup_PATH() discovers package runtime bin dirs via dpkg and replaces PATH with those dirs.
    euid: int | None = (
        0  # Import-time default that forces every apt subprocess through the root/sudo execution path.
    )
    apt_gpg_keys: dict[str, str] = Field(default_factory=dict)
    apt_sources: dict[str, str] = Field(default_factory=dict)
    apt_system_groups: dict[str, dict[str, object]] = Field(default_factory=dict)
    apt_system_users: dict[str, dict[str, object]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def add_package_aliases(self) -> Self:
        self.overrides["gem"] = {
            **self.overrides.get("gem", {}),
            "install_args": ["ruby"],
        }
        return self

    @contextmanager
    def apt_lock(self):
        APT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with APT_LOCK_PATH.open("w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def apt_lists_recent(self, lists_dir: Path = DEFAULT_APT_LISTS_DIR) -> bool:
        """Return True when apt package lists already exist and were updated recently."""
        try:
            newest_list_mtime = max(
                path.stat().st_mtime
                for path in lists_dir.iterdir()
                if path.is_file() and path.name != "lock"
            )
        except (OSError, ValueError):
            return False
        return (time.time() - newest_list_mtime) <= UPDATE_CHECK_INTERVAL

    def should_update_apt_cache(
        self,
        *,
        custom_repositories_configured: bool,
        lists_dir: Path = DEFAULT_APT_LISTS_DIR,
    ) -> bool:
        if custom_repositories_configured:
            return True
        if (
            _LAST_UPDATE_CHECK
            and (time.time() - _LAST_UPDATE_CHECK) <= UPDATE_CHECK_INTERVAL
        ):
            return False
        return not self.apt_lists_recent(lists_dir)

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from dpkg-discovered package runtime bin dirs, not from apt-get itself."""
        if sys.platform != "linux":
            # Apt has no runtime PATH contribution on non-Linux hosts. Returning
            # here keeps fallback provider lists cheap: merely considering apt
            # must not ask other providers to locate or install apt-get.
            self.PATH = ""
            return
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
                # Seed self.PATH with apt-get's bin_dir before calling
                # self.exec(dpkg -L bash). self.exec's build_exec_env
                # re-enters self.setup_PATH; without a non-empty PATH,
                # the ``not self.PATH`` guard at the top of this method
                # would fire on every recursive entry and infinitely
                # loop. The bin_dir is correct as a baseline value —
                # the dpkg-discovered runtime bin dirs get prepended
                # onto it just below.
                self.PATH = TypeAdapter(PATHStr).validate_python(
                    str(apt_abspath.parent),
                )
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
                    Path(path) for path in dpkg_install_dirs if path.endswith("/bin")
                ]
                dpkg_runtime_dirs = list(
                    dict.fromkeys(
                        runtime_dir
                        for bin_dir in dpkg_bin_dirs
                        for runtime_dir in (bin_dir, bin_dir.with_name("sbin"))
                        if runtime_dir.is_dir()
                    ),
                )
                for runtime_dir in dpkg_runtime_dirs:
                    if str(runtime_dir) not in PATH:
                        PATH = ":".join([str(runtime_dir), *PATH.split(":")])
                self.PATH = TypeAdapter(PATHStr).validate_python(PATH)
        super().setup_PATH(no_cache=no_cache)

    @staticmethod
    def _detect_distro_codename() -> tuple[str, str]:
        """Return (distro_id, codename) parsed from /etc/os-release with apt fallbacks."""
        os_release: dict[str, str] = {}
        try:
            with open("/etc/os-release", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or "=" not in line or line.startswith("#"):
                        continue
                    key, _, value = line.partition("=")
                    os_release[key] = value.strip().strip('"').strip("'")
        except OSError:
            pass
        distro_id = (os_release.get("ID") or "").lower()
        codename = (
            os_release.get("VERSION_CODENAME")
            or os_release.get("UBUNTU_CODENAME")
            or ""
        ).lower()
        if distro_id in ("ubuntu", "debian"):
            return distro_id, codename or (
                "noble" if distro_id == "ubuntu" else "stable"
            )
        # apt is debian-derived; fall back to ubuntu LTS for derivatives.
        return "ubuntu", codename or "noble"

    @staticmethod
    def _apt_key_path(path: str) -> Path:
        candidate = Path(path)
        return (
            candidate
            if candidate.is_absolute()
            else DEFAULT_APT_KEYRING_DIR / candidate
        )

    @staticmethod
    def _apt_source_path(path: str) -> Path:
        candidate = Path(path)
        return (
            candidate
            if candidate.is_absolute()
            else DEFAULT_APT_SOURCES_DIR / candidate
        )

    def _write_apt_file(self, path: Path, content: str | bytes) -> None:
        self.exec(
            bin_name="install",
            cmd=["-d", "-m", "0755", path.parent],
            quiet=True,
            should_log_command=False,
        )
        proc = self.exec(
            bin_name="tee",
            cmd=[path],
            input=content,
            text=isinstance(content, str),
            quiet=True,
            should_log_command=False,
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", [str(path)], proc)
        chmod = self.exec(
            bin_name="chmod",
            cmd=["0644", path],
            quiet=True,
            should_log_command=False,
        )
        if chmod.returncode != 0:
            self._raise_proc_error("install", [str(path)], chmod)

    def setup_apt_system_accounts(self) -> None:
        """Create system users/groups declared by apt provider overrides."""
        for group_name, group_config in self.apt_system_groups.items():
            exists = self.exec(
                bin_name="getent",
                cmd=["group", group_name],
                quiet=True,
                should_log_command=False,
            )
            if exists.returncode == 0:
                continue

            groupadd_args = ["--system"]
            gid = group_config.get("gid")
            if gid is not None:
                groupadd_args.extend(["--gid", str(gid)])
            groupadd_args.append(group_name)
            proc = self.exec(bin_name="groupadd", cmd=groupadd_args, quiet=True)
            if proc.returncode != 0:
                self._raise_proc_error("install", groupadd_args, proc)

        for user_name, user_config in self.apt_system_users.items():
            exists = self.exec(
                bin_name="id",
                cmd=["-u", user_name],
                quiet=True,
                should_log_command=False,
            )
            if exists.returncode == 0:
                continue

            useradd_args = ["--system"]
            uid = user_config.get("uid")
            if uid is not None:
                useradd_args.extend(["--uid", str(uid)])
            gid = user_config.get("gid")
            if gid is not None:
                useradd_args.extend(["--gid", str(gid)])
            home = user_config.get("home")
            if home is not None:
                useradd_args.extend(["--home-dir", str(home)])
            shell = user_config.get("shell")
            if shell is not None:
                useradd_args.extend(["--shell", str(shell)])
            groups = user_config.get("groups")
            if isinstance(groups, (list, tuple)) and groups:
                useradd_args.extend(
                    ["--groups", ",".join(str(group) for group in groups)],
                )
            if bool(user_config.get("create_home", False)):
                useradd_args.append("--create-home")
            else:
                useradd_args.append("--no-create-home")
            useradd_args.append(user_name)

            proc = self.exec(bin_name="useradd", cmd=useradd_args, quiet=True)
            if proc.returncode != 0:
                self._raise_proc_error("install", useradd_args, proc)

    def setup_apt_repositories(self, no_cache: bool = False) -> bool:
        """Install custom apt repository keys and source files declared by overrides."""
        if not self.apt_gpg_keys and not self.apt_sources:
            return False

        for key_url, key_path in self.apt_gpg_keys.items():
            with urllib.request.urlopen(
                key_url,
                timeout=self.install_timeout or 60,
            ) as response:
                key_bytes = response.read()
            self._write_apt_file(self._apt_key_path(key_path), key_bytes)

        for source_path, source in self.apt_sources.items():
            self._write_apt_file(
                self._apt_source_path(source_path),
                source.rstrip() + "\n",
            )

        return True

    def default_docs_url_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> str | None:
        package = self._docs_url_package_name(bin_name)
        if not package:
            return None
        distro, codename = self._detect_distro_codename()
        host = "packages.debian.org" if distro == "debian" else "packages.ubuntu.com"
        return f"https://{host}/{codename}/{package}"

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> SemVer | None:
        try:
            version = super().default_version_handler(
                bin_name,
                abspath=abspath,
                timeout=timeout,
                no_cache=no_cache,
                **context,
            )
            if isinstance(version, SemVer):
                return version
        except ValueError:
            pass

        resolved_abspath = abspath or self.get_abspath(
            bin_name,
            quiet=True,
            no_cache=no_cache,
        )
        dpkg_query = EnvProvider().load("dpkg-query", no_cache=no_cache)
        if not resolved_abspath or not dpkg_query or not dpkg_query.loaded_abspath:
            return None

        resolved_path = Path(resolved_abspath).resolve()
        path_candidates = [Path(resolved_abspath), resolved_path]
        if resolved_path.parts[:2] == ("/", "usr"):
            path_candidates.append(Path("/", *resolved_path.parts[2:]))
        elif resolved_path.parts and resolved_path.parts[0] == "/":
            path_candidates.append(Path("/usr", *resolved_path.parts[1:]))

        owning_package = None
        for path_candidate in dict.fromkeys(path_candidates):
            proc = self.exec(
                bin_name=dpkg_query.loaded_abspath,
                cmd=["--search", str(path_candidate)],
                quiet=True,
                timeout=timeout,
            )
            if proc.returncode == 0 and proc.stdout:
                owning_package = proc
                break
        if owning_package is None:
            return None
        package_name, separator, _ = owning_package.stdout.partition(": ")
        if not separator or not package_name:
            return None

        package_version = self.exec(
            bin_name=dpkg_query.loaded_abspath,
            cmd=["--show", "--showformat=${Version}", package_name],
            quiet=True,
            timeout=timeout,
        )
        if package_version.returncode != 0:
            return None
        return SemVer.parse(package_version.stdout.strip())

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        abspath = super().default_abspath_handler(
            bin_name,
            no_cache=no_cache,
            **context,
        )
        if abspath or sys.platform != "linux" or "/" in str(bin_name):
            return (
                TypeAdapter(HostBinPath).validate_python(abspath) if abspath else None
            )

        dpkg_query = EnvProvider().load("dpkg-query", no_cache=no_cache)
        if not dpkg_query or not dpkg_query.loaded_abspath:
            return None

        proc = self.exec(
            bin_name=dpkg_query.loaded_abspath,
            cmd=["--search", f"*/{bin_name}"],
            quiet=True,
            timeout=self.version_timeout,
        )
        if proc.returncode != 0:
            return None

        for line in proc.stdout.splitlines():
            _, separator, package_path = line.partition(": ")
            if not separator:
                continue
            candidate = Path(package_path.strip())
            if candidate.name != str(bin_name):
                continue
            if candidate.parent.name not in {"bin", "sbin"}:
                continue
            if candidate.is_file() and os.access(candidate, os.X_OK):
                self.PATH = self._merge_PATH(
                    candidate.parent,
                    PATH=self.PATH,
                    prepend=True,
                )
                return TypeAdapter(HostBinPath).validate_python(candidate)
        return None

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

        with self.apt_lock():
            self.setup_apt_system_accounts()
            custom_repositories_configured = self.setup_apt_repositories(
                no_cache=no_cache,
            )
            if self.should_update_apt_cache(
                custom_repositories_configured=custom_repositories_configured,
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

        with self.apt_lock():
            self.setup_apt_system_accounts()
            custom_repositories_configured = self.setup_apt_repositories(
                no_cache=no_cache,
            )
            if self.should_update_apt_cache(
                custom_repositories_configured=custom_repositories_configured,
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

    def default_search_handler(
        self,
        bin_name: BinName,
        min_version: SemVer | None = None,
        min_release_age: float | None = None,
        timeout: int | None = None,
        **context,
    ) -> list[ShallowBinary]:
        """Search apt's package index for packages whose name matches bin_name (substring)."""
        from .binary import Binary

        with self.apt_lock():
            custom_repositories_configured = self.setup_apt_repositories(
                no_cache=bool(context.get("no_cache", False)),
            )
            if custom_repositories_configured:
                installer_bin = self.INSTALLER_BINARY(
                    no_cache=bool(context.get("no_cache", False)),
                ).loaded_abspath
                assert installer_bin
                self.exec(
                    bin_name=installer_bin,
                    cmd=["update", "-qq"],
                    timeout=timeout,
                )

        # ``apt-cache search --names-only`` returns lines like ``<name> - <description>``.
        # Routing through ``self.exec`` lets apt's setup_PATH/INSTALLER_BINARY
        # auto-recover from a missing/broken apt-get on the ambient PATH
        # (e.g. CI runners where the linuxbrew copy is unusable). The
        # deadlock filter in ``BinProvider.INSTALLER_BINARY`` keeps it
        # safe under restrictive ``--binproviders`` configs.
        self.INSTALLER_BINARY(no_cache=bool(context.get("no_cache", False)))
        proc = self.exec(
            bin_name="apt-cache",
            cmd=["search", "--names-only", str(bin_name)],
            quiet=True,
            timeout=timeout,
        )
        results: list[ShallowBinary] = []
        for line in proc.stdout.splitlines():
            pkg_name, _, description = line.partition(" - ")
            pkg_name = pkg_name.strip()
            if not pkg_name or str(bin_name) not in pkg_name:
                continue
            results.append(
                Binary(
                    name=pkg_name,
                    description=description.strip(),
                    binproviders=[self],
                    overrides={self.name: {"install_args": [pkg_name]}},
                ),
            )
        return results

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

        with self.apt_lock():
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

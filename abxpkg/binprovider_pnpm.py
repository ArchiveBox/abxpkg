#!/usr/bin/env python3

__package__ = "abxpkg"

import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import ClassVar, Self

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
from .config import load_derived_cache
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
    INSTALLER_BINPROVIDERS: ClassVar[tuple[BinProviderName, ...] | None] = ("npm",)

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
        if str(bin_name) in {"browsers", "puppeteer-browsers"}:
            return ("@puppeteer/browsers",)
        return TypeAdapter(InstallArgs).validate_python(
            super().default_install_args_handler(bin_name, **context)
            or [str(bin_name)],
        )

    def default_docs_url_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> str | None:
        package = self._docs_url_package_name(bin_name, allow_leading_at=True)
        if not package:
            return None
        return f"https://www.npmjs.com/package/{package}"

    @computed_field
    @property
    def is_valid(self) -> bool:
        return super().is_valid

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Derive pnpm's managed node_modules/.bin dir from install_root."""
        if self.install_root is not None:
            expected_bin_dir = self.install_root / "node_modules" / ".bin"
            if self.bin_dir is None or (
                self.bin_dir.name == ".bin"
                and self.bin_dir.parent.name == "node_modules"
                and self.bin_dir != expected_bin_dir
            ):
                self.bin_dir = expected_bin_dir
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

    def _cached_installer_binary(self, no_cache: bool = False):
        if not no_cache and self._INSTALLER_BINARY and self._INSTALLER_BINARY.is_valid:
            return self._INSTALLER_BINARY

        derived_env_path = self.derived_env_path
        if no_cache or not derived_env_path or not derived_env_path.is_file():
            return None

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
            loaded = self.load_cached_binary(self.INSTALLER_BIN, Path(cached_abspath))
            if loaded and loaded.loaded_abspath:
                self._INSTALLER_BINARY = loaded
                return loaded
        return None

    def _cache_node_dependency(self, no_cache: bool = False) -> None:
        try:
            node_loaded = Binary(
                name="node",
                binproviders=[EnvProvider(install_root=None, bin_dir=None)],
            ).load(no_cache=no_cache)
        except Exception:
            node_loaded = None
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

    def _installer_provider_root(self) -> Path:
        lib_dir = os.environ.get("ABXPKG_LIB_DIR")
        if (
            self.install_root is not None
            and lib_dir
            and str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
        ):
            return Path(lib_dir) / "npm" / "packages" / "pnpm"
        if self.install_root is not None:
            return self.install_root / "npm"
        return self.cache_dir / "npm"

    def _load_installer_at(self, abspath: Path, no_cache: bool = False):
        loaded = EnvProvider(
            PATH=str(abspath.parent),
            install_root=None,
            bin_dir=None,
        ).load(bin_name=self.INSTALLER_BIN, no_cache=True)
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
                    cache_kind="dependency",
                )
            self._INSTALLER_BINARY = loaded
            self._cache_node_dependency(no_cache=no_cache)
            return loaded
        return None

    def _install_installer_binary(self, no_cache: bool = False):
        from .binprovider_npm import NpmProvider

        npm_root = self._installer_provider_root()
        loaded = Binary(
            name=self.INSTALLER_BIN,
            binproviders=[
                NpmProvider(
                    install_root=npm_root,
                    postinstall_scripts=True,
                    min_release_age=0,
                ),
            ],
            postinstall_scripts=True,
            min_release_age=0,
        ).install(no_cache=no_cache)
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
                    cache_kind="dependency",
                )
            self._INSTALLER_BINARY = loaded
            self._cache_node_dependency(no_cache=no_cache)
        return loaded

    def INSTALLER_BINARY(self, no_cache: bool = False):
        cached = self._cached_installer_binary(no_cache=no_cache)
        if cached is not None:
            return cached

        env_var = f"{self.INSTALLER_BIN.upper()}_BINARY"
        manual = os.environ.get(env_var)
        if manual and os.path.isabs(manual) and Path(manual).is_file():
            loaded = self._load_installer_at(Path(manual), no_cache=no_cache)
            if loaded is not None:
                return loaded

        host_installer = bin_abspath(
            self.INSTALLER_BIN,
            PATH=os.environ.get("PATH", ""),
        )
        if host_installer:
            loaded = self._load_installer_at(host_installer, no_cache=no_cache)
            if loaded is not None:
                return loaded

        local_installer = (
            self._installer_provider_root()
            / "node_modules"
            / ".bin"
            / str(
                self.INSTALLER_BIN,
            )
        )
        if local_installer.is_file() and os.access(local_installer, os.X_OK):
            loaded = self._load_installer_at(local_installer, no_cache=no_cache)
            if loaded is not None:
                return loaded

        loaded = self._install_installer_binary(no_cache=no_cache)
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

    @staticmethod
    def _package_name_from_install_args(install_args: InstallArgs) -> str:
        main_package = next(
            (arg for arg in install_args if arg and not arg.startswith("-")),
            "",
        )
        if not main_package:
            return ""
        if main_package.startswith("@"):
            return "@" + main_package[1:].split("@", 1)[0]
        return main_package.split("@", 1)[0]

    def _node_modules_dir(self) -> Path | None:
        if self.install_root:
            return self.install_root / "node_modules"
        try:
            pnpm_abspath = self.INSTALLER_BINARY().loaded_abspath
            assert pnpm_abspath
            return Path(
                self.exec(
                    bin_name=pnpm_abspath,
                    cmd=["root", "--global"],
                    timeout=self.version_timeout,
                    quiet=True,
                ).stdout.strip(),
            )
        except Exception:
            return None

    def _installed_package_dir(self, bin_name: str) -> Path | None:
        install_args = self.get_install_args(bin_name, quiet=True) or [bin_name]
        package = self._package_name_from_install_args(install_args)
        modules_dir = self._node_modules_dir()
        if not package or modules_dir is None:
            return None
        package_dir = modules_dir / package
        return package_dir if package_dir.is_dir() else None

    def _installed_package_json(self, bin_name: str) -> dict:
        package_dir = self._installed_package_dir(bin_name)
        if package_dir is None:
            return {}
        package_json_path = package_dir / "package.json"
        try:
            loaded = json.loads(package_json_path.read_text())
        except Exception:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _installed_package_version(self, bin_name: str) -> SemVer | None:
        version = self._installed_package_json(bin_name).get("version")
        return SemVer.parse(version) if isinstance(version, str) else None

    def _provided_bin_dir(self, no_cache: bool = False) -> Path | None:
        bin_dir = (
            self.bin_dir if self.bin_dir is not None else Path(self.ENV["PNPM_HOME"])
        )
        return bin_dir if bin_dir.is_dir() else None

    def _available_cli_paths(self, no_cache: bool = False) -> dict[str, HostBinPath]:
        bin_dir = self._provided_bin_dir(no_cache=no_cache)
        if bin_dir is None:
            return {}
        cli_paths: dict[str, HostBinPath] = {}
        for entry in sorted(bin_dir.iterdir(), key=lambda path: path.name):
            if not (entry.is_file() or entry.is_symlink()):
                continue
            if not os.access(entry, os.R_OK):
                continue
            try:
                cli_paths[entry.name] = TypeAdapter(HostBinPath).validate_python(entry)
            except Exception:
                continue
        return cli_paths

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

    def default_search_handler(
        self,
        bin_name: BinName,
        min_version: SemVer | None = None,
        min_release_age: float | None = None,
        timeout: int | None = None,
        **context,
    ) -> list:
        """Search the npm registry and return installable pnpm package matches."""
        from .binary import Binary

        results: list = []
        seen: set[str] = set()

        def append_result(pkg: dict) -> None:
            pkg_name = pkg.get("name", "")
            if (
                not pkg_name
                or not (pkg_name[0].isalpha() or pkg_name[0] == "@")
                or pkg_name in seen
                or str(bin_name).lower() not in pkg_name.lower()
            ):
                return
            version_str = pkg.get("version", "")
            description = pkg.get("description", "") or pkg_name
            seen.add(pkg_name)
            results.append(
                Binary(
                    name=pkg_name,
                    description=f"{version_str} - {description}".strip(" -"),
                    binproviders=[self],
                    overrides={self.name: {"install_args": [pkg_name]}},
                ),
            )

        registry_url = (
            "https://registry.npmjs.org/-/v1/search?text="
            + urllib.parse.quote(str(bin_name))
            + "&size=25"
        )
        try:
            with urllib.request.urlopen(
                registry_url,
                timeout=timeout or self.version_timeout,
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}

        for entry in data.get("objects", []):
            append_result(entry.get("package", {}))

        if str(bin_name) not in seen:
            exact_url = (
                "https://registry.npmjs.org/"
                + urllib.parse.quote(str(bin_name), safe="")
                + "/latest"
            )
            try:
                with urllib.request.urlopen(
                    exact_url,
                    timeout=timeout or self.version_timeout,
                ) as resp:
                    append_result(json.loads(resp.read().decode("utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
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
        return self._available_cli_paths(no_cache=no_cache).get(str(bin_name))

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> SemVer | None:
        installed_package_version = self._installed_package_version(str(bin_name))
        if installed_package_version:
            return installed_package_version

        try:
            pnpm_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert pnpm_abspath
        except Exception:
            pnpm_abspath = None

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
        if pnpm_abspath is not None:
            try:
                json_output = self.exec(
                    bin_name=pnpm_abspath,
                    cmd=[
                        "ls",
                        f"--dir={self.install_root}"
                        if self.install_root
                        else "--global",
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
            version = self._version_from_exec(
                bin_name,
                abspath=abspath,
                timeout=timeout,
            )
            if version:
                return version
        except ValueError:
            return None
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

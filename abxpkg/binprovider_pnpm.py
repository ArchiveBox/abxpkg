#!/usr/bin/env python3

__package__ = "abxpkg"

import json
import os
import shlex
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, Self, cast

from platformdirs import user_cache_path
from pydantic import Field, TypeAdapter, computed_field, model_validator

from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abxpkg_cache_dir_default,
    abxpkg_ephemeral_cache_dir_default,
    abxpkg_ephemeral_cache_home_default,
    abxpkg_install_root_default,
    bin_abspath,
)
from .binary import Binary
from .binprovider import (
    BinaryOverrides,
    BinProvider,
    EnvProvider,
    env_flag_is_true,
    log_method_call,
    remap_kwargs,
)
from .config import load_derived_cache
from .exceptions import BinaryInstallError, BinaryLoadError, BinProviderInstallError
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
    INSTALLER_BINPROVIDERS: ClassVar[tuple[BinProviderName, ...] | None] = (
        "env",
        "npm",
    )
    FIRST_WRITER_ENV_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"NODE_MODULES_DIR", "NODE_MODULE_DIR"},
    )
    CACHE_CONTEXT_ENV_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"NPM_BINARY", "PNPM_HOME"},
    )

    @classmethod
    def host_projection_target(cls, source_path: Path) -> Path | None:
        """Resolve a pnpm shell shim to the executable package script it wraps."""
        launcher_path = source_path.resolve(strict=False)
        try:
            with launcher_path.open("rb") as launcher_file:
                launcher_bytes = launcher_file.read(64 * 1024)
        except OSError:
            return None
        if not launcher_bytes.startswith(b"#!/bin/sh"):
            return None
        try:
            launcher_text = launcher_bytes.decode("utf-8")
        except UnicodeError:
            return None

        relative_prefix = "$basedir/../.pnpm/"
        for line in reversed(launcher_text.splitlines()):
            if relative_prefix not in line:
                continue
            try:
                tokens = shlex.split(line)
            except ValueError:
                continue
            for token in tokens:
                if not token.startswith(relative_prefix):
                    continue
                relative_target = token.removeprefix("$basedir/")
                package_store = (launcher_path.parent.parent / ".pnpm").resolve(
                    strict=False,
                )
                target = (launcher_path.parent / relative_target).resolve(
                    strict=False,
                )
                if (
                    target.is_relative_to(package_store)
                    and target.is_file()
                    and os.access(target, os.X_OK)
                ):
                    return target
        return None

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
            env["NODE_PATH"] = node_modules_dir
        return env

    def get_cache_info(
        self,
        bin_name: BinName,
        abspath: HostBinPath,
    ) -> dict[str, list[Path]] | None:
        cache_info = super().get_cache_info(bin_name, abspath)
        if cache_info is None or self.install_root is None:
            return cache_info

        for package in self.get_package_names(str(bin_name)):
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
        except (
            AssertionError,
            BinProviderInstallError,
            BinaryInstallError,
            OSError,
            ValueError,
        ):
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
        package = self._docs_url_package_name(bin_name)
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
        if env_flag_is_true("ABXPKG_NO_CACHE"):
            return abxpkg_ephemeral_cache_dir_default("pnpm")
        specific_cache_dir = os.environ.get("ABXPKG_PNPM_CACHE_DIR", "").strip()
        if specific_cache_dir:
            return Path(specific_cache_dir).expanduser().resolve()
        managed_lib_dir = self._managed_lib_dir()
        default_cache_dir = (
            managed_lib_dir / "cache" / "pnpm"
            if managed_lib_dir is not None
            else abxpkg_cache_dir_default("pnpm") or Path(USER_CACHE_PATH)
        )
        if self._ensure_writable_cache_dir(default_cache_dir):
            return default_cache_dir
        return Path(tempfile.gettempdir()) / f"abxpkg-pnpm-store-{os.getuid()}"

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from install_root/bin_dir, or PNPM_HOME in global mode."""
        path_entries: list[str | Path] = []
        managed_lib_dir = self._managed_lib_dir()
        if managed_lib_dir is not None:
            path_entries.append(managed_lib_dir / "env" / "bin")
        if self.bin_dir:
            path_entries.append(self.bin_dir)
        else:
            # In global mode, pnpm puts shims under PNPM_HOME (from env, or
            # ``<cache_dir>/pnpm-home`` — the same fallback exec() uses).
            pnpm_home = os.environ.get("PNPM_HOME") or str(
                self.cache_dir / "pnpm-home",
            )
            path_entries.append(pnpm_home)
        if self._INSTALLER_BINARY and self._INSTALLER_BINARY.loaded_abspath:
            path_entries.append(self._INSTALLER_BINARY.loaded_abspath.parent)
        npm_binary = os.environ.get("NPM_BINARY")
        if npm_binary and os.path.isabs(npm_binary) and Path(npm_binary).is_file():
            path_entries.append(Path(npm_binary).parent)
        self.PATH = self._merge_PATH(*path_entries, PATH=self.PATH)
        super().setup_PATH(no_cache=no_cache)

    def exec_env_providers(self) -> list[BinProvider]:
        providers = super().exec_env_providers()
        managed_lib_dir = self._managed_lib_dir()
        if managed_lib_dir is not None:
            from .binprovider_node import NodeProvider

            node_provider = NodeProvider(install_root=managed_lib_dir / "node")
            if node_provider.bin_dir and (node_provider.bin_dir / "node").is_file():
                self._append_unique_provider(providers, node_provider)
        return providers

    def supports_cached_exec(self) -> bool:
        # Global pnpm execution injects --config.global-bin-dir dynamically.
        return self.install_root is not None

    def _exec_bin_abspath(self, bin_abspath: Path) -> Path:
        installer = self._INSTALLER_BINARY
        if (
            installer is not None
            and installer.loaded_abspath == bin_abspath
            and installer.loaded_binprovider is not None
        ):
            return installer.loaded_binprovider._exec_bin_abspath(bin_abspath)
        return super()._exec_bin_abspath(bin_abspath)

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

    def _managed_env_provider(self) -> EnvProvider:
        """Return the host-discovery provider for this pnpm installation."""
        managed_lib_dir = self._managed_lib_dir()
        if managed_lib_dir is None:
            return EnvProvider()
        env_root = managed_lib_dir / "env"
        return EnvProvider(install_root=env_root, bin_dir=env_root / "bin")

    def _cache_lifecycle_dependency(
        self,
        bin_name: BinName,
        no_cache: bool = False,
    ):
        from . import PROVIDER_CLASS_BY_NAME

        provider_names: tuple[BinProviderName, ...]
        overrides: BinaryOverrides = {}
        min_version = None
        if str(bin_name) == "node":
            provider_names = ("env", "node", "brew", "apt")
            min_version = SemVer.parse("22.12.0")
            overrides = cast(
                BinaryOverrides,
                {
                    "apt": {
                        "install_args": ["nodejs"],
                    },
                    "brew": {
                        "install_args": ["node"],
                    },
                },
            )
        elif str(bin_name) == "npm":
            provider_names = ("env", "node", "brew", "apt")
            overrides = cast(
                BinaryOverrides,
                {
                    "apt": {
                        "install_args": ["npm"],
                    },
                    "brew": {
                        "install_args": ["node"],
                    },
                },
            )
        else:
            provider_names = ("env",)

        env_provider = self._managed_env_provider()
        managed_lib_dir = self._managed_lib_dir()
        providers: list[BinProvider] = []
        for provider_name in provider_names:
            if provider_name not in PROVIDER_CLASS_BY_NAME:
                continue
            if provider_name == "env":
                providers.append(env_provider)
            elif provider_name == "node" and managed_lib_dir is not None:
                providers.append(
                    PROVIDER_CLASS_BY_NAME[provider_name](
                        install_root=managed_lib_dir / "node",
                    ),
                )
            else:
                providers.append(PROVIDER_CLASS_BY_NAME[provider_name]())
        try:
            loaded = Binary(
                name=bin_name,
                binproviders=providers,
                overrides=overrides,
                postinstall_scripts=True,
                min_release_age=0,
                min_version=min_version,
            ).install(no_cache=no_cache)
        except (
            AssertionError,
            BinaryLoadError,
            BinProviderInstallError,
            BinaryInstallError,
            OSError,
            ValueError,
        ):
            loaded = None
        if (
            loaded
            and loaded.loaded_abspath
            and loaded.loaded_version
            and loaded.loaded_sha256
        ):
            projected_abspath = env_provider.project_binary(loaded, bin_name)
            if projected_abspath is not None:
                loaded = loaded.model_copy(update={"loaded_abspath": projected_abspath})
            loaded_abspath = loaded.loaded_abspath
            loaded_version = loaded.loaded_version
            loaded_sha256 = loaded.loaded_sha256
            if not (loaded_abspath and loaded_version and loaded_sha256):
                return loaded
            self.write_cached_binary(
                bin_name,
                loaded_abspath,
                loaded_version,
                loaded_sha256,
                resolved_provider_name=(
                    loaded.loaded_binprovider.name
                    if loaded.loaded_binprovider is not None
                    else self.name
                ),
                resolved_provider=loaded.loaded_binprovider,
                cache_kind="dependency",
            )
        return loaded

    def _cache_lifecycle_dependencies(self, no_cache: bool = False):
        return {
            "node": self._cache_lifecycle_dependency("node", no_cache=no_cache),
            "npm": self._cache_lifecycle_dependency("npm", no_cache=no_cache),
        }

    @staticmethod
    def _pnpm_package_for_node(node_version: SemVer | None) -> str:
        """Select the newest pnpm major supported by the discovered Node runtime."""
        version = tuple(node_version) if node_version is not None else None
        if version is None or version >= (18, 12, 0):
            return "pnpm@10.19.0"
        if version >= (16, 14, 0):
            return "pnpm@8"
        if version >= (14, 6, 0):
            return "pnpm@7"
        if version >= (12, 17, 0):
            return "pnpm@6"
        if version >= (10, 16, 0):
            return "pnpm@5"
        if version >= (10, 13, 0):
            return "pnpm@4"
        return "pnpm@3"

    def _managed_lib_dir(self) -> Path | None:
        """Return the ABX-managed lib root implied by install_root, if any.

        Hook tests and crawls often pass a per-run ABXPKG_LIB_DIR through the
        resolved provider object without mutating process-wide os.environ. pnpm
        must keep package installs, its self-bootstrapped installer, and the
        content-addressable store under that same root; otherwise node_modules
        can be linked to a store from a different crawl/test run and pnpm
        rejects the install with ERR_PNPM_UNEXPECTED_STORE.
        """
        if self.install_root is None:
            return None
        install_root = self.install_root
        if install_root.name == "pnpm":
            return install_root.parent
        if (
            install_root.parent.name == "packages"
            and install_root.parent.parent.name == "pnpm"
        ):
            return install_root.parent.parent.parent
        return None

    def _installer_provider_root(self) -> Path:
        managed_lib_dir = self._managed_lib_dir()
        if managed_lib_dir is not None:
            return managed_lib_dir / "npm" / "packages" / "pnpm"
        if self.install_root is not None:
            return self.install_root / "npm"
        return self.cache_dir / "npm"

    def _load_installer_at(self, abspath: Path, no_cache: bool = False):
        env_provider = (
            self._managed_env_provider()
            if self._managed_lib_dir() is not None
            else EnvProvider(PATH=str(abspath.parent), install_root=None, bin_dir=None)
        )
        # INSTALLER_BINARY already selected this exact candidate. Keep the
        # projection provider constrained to its directory so a different,
        # newer pnpm elsewhere on ambient PATH cannot replace it here.
        env_provider.PATH = str(abspath.parent)
        loaded = env_provider.load(bin_name=self.INSTALLER_BIN, no_cache=True)
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
            self._cache_lifecycle_dependencies(no_cache=no_cache)
            return loaded
        return None

    def _install_installer_binary(self, no_cache: bool = False):
        from .binprovider_npm import NpmProvider

        npm_root = self._installer_provider_root()
        npm_provider = NpmProvider(
            install_root=npm_root,
            postinstall_scripts=True,
            min_release_age=0,
        )
        npm_installer = npm_provider.INSTALLER_BINARY(no_cache=no_cache)
        dependencies = self._cache_lifecycle_dependencies(no_cache=no_cache)
        node_loaded = dependencies["node"]
        pnpm_package = self._pnpm_package_for_node(
            node_loaded.loaded_version if node_loaded is not None else None,
        )
        npm_provider = npm_provider.get_provider_with_overrides(
            overrides={"pnpm": {"install_args": [pnpm_package]}},
        )
        npm_provider._INSTALLER_BINARY = npm_installer

        # npm is a host dependency. Project it into the managed env/bin before
        # giving it to the npm provider so no programmatic path relies on the
        # ambient host path (or the human-convenience LIB_DIR/bin directory).
        try:
            host_npm = Binary(
                name="npm",
                binproviders=[self._managed_env_provider()],
            ).load(no_cache=no_cache)
        except (
            AssertionError,
            BinProviderInstallError,
            BinaryInstallError,
            OSError,
            ValueError,
        ):
            host_npm = None
        if host_npm and host_npm.loaded_abspath:
            npm_provider._INSTALLER_BINARY = host_npm

        loaded = Binary(
            name=self.INSTALLER_BIN,
            binproviders=[npm_provider],
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
                    resolved_provider=loaded.loaded_binprovider,
                    cache_kind="dependency",
                )
            self._INSTALLER_BINARY = loaded
            self._cache_lifecycle_dependencies(no_cache=no_cache)
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

        from .binprovider_npm import NpmProvider

        installer_root = self._installer_provider_root()
        dependencies = self._cache_lifecycle_dependencies(no_cache=no_cache)
        node_loaded = dependencies["node"]
        pnpm_package = self._pnpm_package_for_node(
            node_loaded.loaded_version if node_loaded is not None else None,
        )
        installer_provider = NpmProvider(
            install_root=installer_root,
            postinstall_scripts=True,
            min_release_age=0,
        ).get_provider_with_overrides(
            overrides={"pnpm": {"install_args": [pnpm_package]}},
        )
        with installer_provider.mutation_lock():
            local_installer = (
                installer_root
                / "node_modules"
                / ".bin"
                / str(
                    self.INSTALLER_BIN,
                )
            )
            if local_installer.is_file() and os.access(local_installer, os.X_OK):
                loaded = installer_provider.load(
                    self.INSTALLER_BIN,
                    quiet=True,
                    no_cache=True,
                )
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
                    self._cache_lifecycle_dependencies(no_cache=no_cache)
                    return loaded

            return self._install_installer_binary(no_cache=no_cache)

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
        pnpm_home = Path(self.ENV["PNPM_HOME"])
        if self.install_root is None:
            # Global installs require PNPM_HOME to exist before pnpm starts.
            pnpm_home.mkdir(parents=True, exist_ok=True)
        installer_abspath = (
            self._INSTALLER_BINARY.loaded_abspath
            if self._INSTALLER_BINARY is not None
            else None
        )
        is_installer = str(bin_name) == str(self.INSTALLER_BIN) or (
            installer_abspath is not None
            and Path(bin_name).resolve(strict=False)
            == Path(installer_abspath).resolve(strict=False)
        )
        if self.install_root is None and is_installer:
            cmd = (f"--config.global-bin-dir={pnpm_home}", *cmd)
        if env_flag_is_true("ABXPKG_NO_CACHE"):
            env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
            env["XDG_CACHE_HOME"] = str(abxpkg_ephemeral_cache_home_default())
            kwargs["env"] = env
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
            self.euid = self._managed_install_euid()
        if not no_cache:
            self._ensure_writable_cache_dir(self.cache_dir)
        managed_dirs = (self.install_root,) if self.install_root else (self.bin_dir,)
        for managed_dir in managed_dirs:
            if managed_dir is None:
                continue
            managed_dir.mkdir(parents=True, exist_ok=True)
            if os.geteuid() == 0 and self.EUID != 0:
                pw_record = self.get_pw_record(self.EUID)
                os.chown(managed_dir, self.EUID, pw_record.pw_gid)

    def _managed_install_euid(self) -> int:
        """Return the uid that should own pnpm-managed package installs."""
        if self.install_root is not None and self.install_root.is_dir():
            existing_owner = self.detect_euid(
                owner_paths=(self.install_root,),
                preserve_root=True,
            )
            if existing_owner != 0:
                return existing_owner
        sudo_uid = self._sudo_managed_install_euid()
        if sudo_uid is not None:
            return sudo_uid
        return self.detect_euid(
            owner_paths=(self.install_root,),
            preserve_root=True,
        )

    def _sudo_managed_install_euid(
        self,
        *,
        current_euid: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> int | None:
        """Return SUDO_UID for root-invoked managed installs, when usable."""
        if self.install_root is None:
            return None
        if current_euid is None:
            current_euid = os.geteuid()
        if current_euid != 0:
            return None
        sudo_uid = (environ or os.environ).get("SUDO_UID")
        if not sudo_uid:
            return None
        try:
            uid = int(sudo_uid)
        except ValueError:
            return None
        if uid > 0 and self.uid_has_passwd_entry(uid):
            return uid
        return None

    def _store_dir(self, no_cache: bool = False) -> Path:
        existing_store_dir = self._existing_store_dir()
        if existing_store_dir is not None:
            return existing_store_dir
        if not no_cache:
            return self.cache_dir
        return abxpkg_ephemeral_cache_dir_default("pnpm")

    def _existing_store_dir(self) -> Path | None:
        """Return pnpm's recorded store for this install root, if one exists."""
        if self.install_root is None:
            return None
        modules_yaml = self.install_root / "node_modules" / ".modules.yaml"
        try:
            text = modules_yaml.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(text)
            store_dir = data.get("storeDir") if isinstance(data, dict) else None
            if isinstance(store_dir, str) and store_dir.strip():
                return Path(store_dir).expanduser()
        except json.JSONDecodeError:
            pass
        try:
            for line in text.splitlines():
                key, separator, value = line.partition(":")
                if separator and key.strip().strip("'\"") == "storeDir":
                    store_dir = value.strip().strip("'\"")
                    return Path(store_dir).expanduser() if store_dir else None
        except OSError:
            return None
        return None

    def _exec_env(self, no_cache: bool = False) -> dict[str, str] | None:
        if not no_cache:
            return None
        env = os.environ.copy()
        env["XDG_CACHE_HOME"] = str(abxpkg_ephemeral_cache_home_default())
        return env

    def _linked_bin_path(self, bin_name: BinName | HostBinPath) -> Path | None:
        """Return the managed shim path for a pnpm-installed executable, if any."""
        if self.bin_dir is None:
            return None
        return self.bin_dir / str(bin_name)

    def cached_binary_state_mismatch(
        self,
        bin_name: BinName,
        cached_record: Mapping[str, object],
    ) -> bool:
        package_names = self.get_package_names(bin_name)
        if self.install_root is not None:
            modules_dir = self.install_root / "node_modules"
            for package in package_names:
                if not (modules_dir / package / "package.json").exists():
                    return True
            installed_version = self._installed_package_version(str(bin_name))
        else:
            raw_abspath = cached_record.get("abspath")
            installed_version = (
                self._installed_abspath_package_version(
                    Path(raw_abspath),
                    package_names=set(package_names),
                )
                if isinstance(raw_abspath, str)
                else None
            )
        raw_cached_version = cached_record.get("loaded_version")
        cached_version = (
            SemVer.parse(raw_cached_version)
            if isinstance(raw_cached_version, (str, bytes))
            else None
        )
        if installed_version is None:
            return True
        if cached_version is not None and installed_version != cached_version:
            return True
        return False

    @classmethod
    def _installed_abspath_package_version(
        cls,
        abspath: Path,
        *,
        package_names: set[str],
    ) -> SemVer | None:
        """Read the package version behind a global pnpm executable without pnpm."""
        package_target = cls.host_projection_target(abspath) or abspath.resolve(
            strict=False,
        )
        for parent in package_target.parents:
            package_json = parent / "package.json"
            try:
                package = json.loads(package_json.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if (
                not isinstance(package, dict)
                or package.get("name") not in package_names
            ):
                continue
            version = package.get("version")
            return SemVer.parse(version) if isinstance(version, str) else None
        return None

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
        except (
            AssertionError,
            BinProviderInstallError,
            BinaryInstallError,
            OSError,
            ValueError,
        ):
            return None

    def _installed_package_dir(self, bin_name: str) -> Path | None:
        package = self.get_package_names(bin_name)[0]
        modules_dir = self._node_modules_dir()
        if not package or modules_dir is None:
            return None
        package_dir = modules_dir / package
        if package_dir.is_dir():
            return package_dir
        if not self._project_declares_package(package):
            return None
        virtual_store = modules_dir / ".pnpm"
        if not virtual_store.is_dir():
            return None
        for candidate in sorted(
            virtual_store.glob(f"*/node_modules/{package}/package.json"),
        ):
            package_dir = candidate.parent
            if package_dir.is_dir():
                return package_dir
        return None

    def _project_declares_package(self, package: str) -> bool:
        if self.install_root is None:
            return True
        package_json = self.install_root / "package.json"
        try:
            project = json.loads(package_json.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(project, dict):
            return False
        dependency_groups = (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        )
        return any(
            isinstance(project.get(group), dict) and package in project[group]
            for group in dependency_groups
        )

    def _installed_package_json(self, bin_name: str) -> dict:
        package_dir = self._installed_package_dir(bin_name)
        if package_dir is None:
            return {}
        package_json_path = package_dir / "package.json"
        try:
            loaded = json.loads(package_json_path.read_text())
        except (OSError, json.JSONDecodeError):
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
            except ValueError:
                continue
        return cli_paths

    def _refresh_bin_link(
        self,
        bin_name: BinName | HostBinPath,
        target: HostBinPath,
    ) -> HostBinPath:
        """Recreate the managed shim wrapper pointing at the resolved pnpm executable."""
        link_path = self._linked_bin_path(bin_name)
        assert link_path is not None, "_refresh_bin_link requires bin_dir to be set"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        target_path = Path(target).expanduser().resolve(strict=False)
        wrapper = f'#!/bin/sh\nexec {shlex.quote(str(target_path))} "$@"\n'
        # Idempotent refresh: skip when shim already runs the target.
        # Rewriting on every load() bumps mtime and churns the inode,
        # which invalidates fingerprint caches unnecessarily.
        if link_path.is_file() and not link_path.is_symlink():
            try:
                if link_path.read_text() == wrapper:
                    return TypeAdapter(HostBinPath).validate_python(link_path)
            except OSError:
                pass
        self._write_executable_wrapper(link_path, wrapper)
        return TypeAdapter(HostBinPath).validate_python(link_path)

    @staticmethod
    def _write_executable_wrapper(path: Path, contents: str) -> None:
        temp_fd, temp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        try:
            os.fchmod(temp_fd, 0o755)
            with os.fdopen(temp_fd, "w", encoding="utf-8") as wrapper_file:
                wrapper_file.write(contents)
                wrapper_file.flush()
                os.fsync(wrapper_file.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _refresh_pnpm_exec_link(
        self,
        bin_name: BinName | HostBinPath,
        package: str,
        no_cache: bool = False,
        require_declared: bool = True,
    ) -> HostBinPath | None:
        """Recreate a managed shim that asks this pnpm project to run a binary."""
        if self.bin_dir is None or self.install_root is None:
            return None
        if require_declared and not self._project_declares_package(package):
            return None
        try:
            installer = self.INSTALLER_BINARY(no_cache=no_cache)
            pnpm_abspath = installer.loaded_abspath
        except (
            AssertionError,
            BinProviderInstallError,
            BinaryInstallError,
            OSError,
            ValueError,
        ):
            pnpm_abspath = None
        if pnpm_abspath is None:
            return None

        link_path = self._linked_bin_path(bin_name)
        assert link_path is not None, "_refresh_pnpm_exec_link requires bin_dir"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        wrapper = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(str(pnpm_abspath))} "
            f"--dir {shlex.quote(str(self.install_root))} "
            f'exec {shlex.quote(str(bin_name))} "$@"\n'
        )
        if link_path.is_file() and not link_path.is_symlink():
            try:
                if link_path.read_text() == wrapper:
                    return TypeAdapter(HostBinPath).validate_python(link_path)
            except OSError:
                pass
        self._write_executable_wrapper(link_path, wrapper)
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

        store_dir = self._store_dir(no_cache)
        self._ensure_writable_cache_dir(store_dir)
        cmd: list[str] = [
            "add",
            "--loglevel=error",
            f"--store-dir={store_dir}",
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

        proc = self.exec(
            bin_name=installer_bin,
            cmd=cmd,
            timeout=timeout,
            env=self._exec_env(no_cache),
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", install_args, proc)
        package = self.get_package_names(bin_name, install_args)[0]
        linked_bin_path = self._linked_bin_path(
            TypeAdapter(BinName).validate_python(bin_name),
        )
        if package and (linked_bin_path is None or not linked_bin_path.exists()):
            self._refresh_pnpm_exec_link(
                TypeAdapter(BinName).validate_python(bin_name),
                package,
                no_cache=no_cache,
                require_declared=False,
            )
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

        store_dir = self._store_dir(no_cache)
        self._ensure_writable_cache_dir(store_dir)
        cmd: list[str] = [
            "add" if min_version is not None else "update",
            "--loglevel=error",
            f"--store-dir={store_dir}",
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

        proc = self.exec(
            bin_name=installer_bin,
            cmd=cmd,
            timeout=timeout,
            env=self._exec_env(no_cache),
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
        store_dir = self._store_dir(no_cache)
        self._ensure_writable_cache_dir(store_dir)
        cmd: list[str] = [
            "remove",
            "--loglevel=error",
            f"--store-dir={store_dir}",
        ]
        cmd.append(f"--dir={self.install_root}" if self.install_root else "--global")
        cmd.extend(install_args)

        proc = self.exec(
            bin_name=installer_bin,
            cmd=cmd,
            timeout=timeout,
            env=self._exec_env(no_cache),
        )
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", install_args, proc)
        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        if str(bin_name) == self.INSTALLER_BIN:
            installer = self.INSTALLER_BINARY(no_cache=no_cache)
            return installer.loaded_abspath
        direct_abspath = self._available_cli_paths(no_cache=no_cache).get(
            str(bin_name),
        )
        if direct_abspath:
            return direct_abspath

        package = self.get_package_names(str(bin_name))[0]
        package_dir = self._installed_package_dir(str(bin_name))
        if package_dir is None:
            return self._refresh_pnpm_exec_link(
                bin_name,
                package or str(bin_name),
                no_cache=no_cache,
            )
        package_info = self._installed_package_json(str(bin_name))
        package_bins = package_info.get("bin", {})
        if isinstance(package_bins, str):
            package_bins = {package_info.get("name") or str(bin_name): package_bins}
        if not isinstance(package_bins, dict):
            return self._refresh_pnpm_exec_link(
                bin_name,
                package or str(bin_name),
                no_cache=no_cache,
            )

        for alt_bin_name, package_bin_path in package_bins.items():
            alt_abspath = bin_abspath(
                alt_bin_name,
                PATH=str(self.bin_dir) if self.bin_dir else self.PATH,
            )
            if alt_abspath:
                resolved_abspath = TypeAdapter(HostBinPath).validate_python(
                    alt_abspath,
                )
                if str(alt_bin_name) == str(bin_name) or self.bin_dir is None:
                    return resolved_abspath
                return self._refresh_bin_link(bin_name, resolved_abspath)
            if not isinstance(package_bin_path, str) or self.bin_dir is None:
                continue
            package_abspath = (package_dir / package_bin_path).resolve(strict=False)
            if package_abspath.is_file():
                return self._refresh_bin_link(
                    bin_name,
                    TypeAdapter(HostBinPath).validate_python(package_abspath),
                )
        package_json = package_dir / "package.json"
        if package_json.is_file():
            return TypeAdapter(HostBinPath).validate_python(package_json)
        return self._refresh_pnpm_exec_link(
            bin_name,
            package or str(bin_name),
            no_cache=no_cache,
        )

    def _get_version_at_abspath(
        self,
        bin_name: BinName,
        installed_abspath: HostBinPath,
        *,
        quiet: bool,
    ) -> SemVer | None:
        installed_package_version = self._installed_package_version(str(bin_name))
        if installed_package_version is not None:
            return installed_package_version
        return super()._get_version_at_abspath(
            bin_name,
            installed_abspath,
            quiet=quiet,
        )

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
        except (
            AssertionError,
            BinProviderInstallError,
            BinaryInstallError,
            OSError,
            ValueError,
        ):
            pnpm_abspath = None

        # Fallback: ask `pnpm ls --json` for the installed version of the
        # main package, and finally fall back to reading its package.json.
        package = self.get_package_names(str(bin_name))[0]
        if pnpm_abspath is not None:
            try:
                json_output = self.exec(
                    bin_name=pnpm_abspath,
                    cmd=[
                        "ls",
                        f"--store-dir={self._store_dir(no_cache)}",
                        f"--dir={self.install_root}"
                        if self.install_root
                        else "--global",
                        "--depth=0",
                        "--json",
                        package,
                    ],
                    timeout=timeout,
                    quiet=True,
                    env=self._exec_env(no_cache),
                ).stdout.strip()
                listing = json.loads(json_output)
                if isinstance(listing, list):
                    listing = listing[0] if listing else {}
                return listing["dependencies"][package]["version"]
            except (
                KeyError,
                TypeError,
                json.JSONDecodeError,
                BinProviderInstallError,
                OSError,
                ValueError,
            ):
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

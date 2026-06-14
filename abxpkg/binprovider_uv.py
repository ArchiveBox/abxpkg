#!/usr/bin/env python3
__package__ = "abxpkg"
import json
import os
import shutil
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import ClassVar, Self
from platformdirs import user_cache_path
from pydantic import Field, TypeAdapter, computed_field, model_validator
from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abxpkg_cache_dir_default,
    abxpkg_install_root_default,
    bin_abspath,
)
from .binary import Binary
from .binprovider import (
    BinProvider,
    EnvProvider,
    env_flag_is_true,
    log_method_call,
    remap_kwargs,
)
from .config import load_derived_cache
from .logging import format_command, format_subprocess_output, get_logger
from .semver import SemVer

USER_CACHE_PATH = user_cache_path("uv", "abxpkg")
logger = get_logger(__name__)


class UvProvider(BinProvider):
    """Standalone ``uv`` package manager provider.
    Has two modes, picked based on whether ``install_root`` is set:
    1. **Hermetic venv mode** (``install_root=Path(...)``): creates a dedicated
       venv at the requested path via ``uv venv`` and installs packages
       into it via ``uv pip install --python <venv>/bin/python``, the same
       way ``PipProvider`` does when configured with ``install_root``. This is
       the idiomatic "install a Python library + its CLI entrypoints into
       an isolated environment" path.
    2. **Global tool mode** (``install_root=None``): delegates to
       ``uv tool install`` which lays out a fresh venv under
       ``UV_TOOL_DIR`` per tool and writes shims into ``UV_TOOL_BIN_DIR``.
       This is the idiomatic "install a CLI tool globally" path.
    Security:
    - ``--no-build`` for ``postinstall_scripts=False`` (wheels only).
    - ``--exclude-newer=<ISO8601>`` for ``min_release_age``.
    """

    name: BinProviderName = "uv"
    _log_emoji = "🚀"
    INSTALLER_BIN: BinName = "uv"
    INSTALLER_BINPROVIDERS: ClassVar[tuple[BinProviderName, ...] | None] = ("pip",)
    PATH: PATHStr = ""  # Starts empty; setup_PATH() lazily uses install_root/venv/bin in venv mode, or UV_TOOL_BIN_DIR/~/.local/bin in tool mode.
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABXPKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABXPKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )
    # None = global ``uv tool`` mode, otherwise install_root is the provider root.
    # In install_root mode the actual virtualenv lives at install_root/venv
    # so provider metadata like derived.env can stay next to it.
    # Default: ABXPKG_UV_ROOT > ABXPKG_LIB_DIR/uv > None.
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("uv"),
        validation_alias="uv_venv",
    )
    # Managed venv mode fills this with ``<install_root>/venv/bin`` in detect_euid_to_use();
    # tool mode may accept an explicit ``uv_tool_bin_dir`` override or leave it unset.
    bin_dir: Path | None = Field(default=None, validation_alias="uv_tool_bin_dir")

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        env: dict[str, str] = {
            "UV_ACTIVE": "1",
            "UV_CACHE_DIR": str(self.cache_dir),
        }
        if self.install_root:
            venv_root = self.install_root / "venv"
            env["VIRTUAL_ENV"] = str(venv_root)
            for sp in sorted(
                (venv_root / "lib").glob("python*/site-packages"),
            ):
                env["PYTHONPATH"] = ":" + str(sp)
                break
            return env
        env["UV_TOOL_DIR"] = str(self.tool_dir)
        if self.bin_dir:
            env["UV_TOOL_BIN_DIR"] = str(self.bin_dir)
        return env

    def supports_min_release_age(self, action, no_cache: bool = False) -> bool:
        return action in ("install", "update")

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        return action in ("install", "update")

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

    def _installer_provider_root(self) -> Path:
        lib_dir = os.environ.get("ABXPKG_LIB_DIR")
        if (
            self.install_root is not None
            and lib_dir
            and str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
        ):
            return Path(lib_dir) / "pip" / "packages" / "uv"
        if self.install_root is not None:
            return self.install_root / "pip"
        return self.cache_dir / "pip"

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
            return loaded
        return None

    def _install_installer_binary(self, no_cache: bool = False):
        from .binprovider_pip import PipProvider

        pip_root = self._installer_provider_root()
        loaded = Binary(
            name=self.INSTALLER_BIN,
            binproviders=[
                PipProvider(
                    install_root=pip_root,
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
            / "venv"
            / "bin"
            / str(
                self.INSTALLER_BIN,
            )
        )
        if local_installer.is_file() and os.access(local_installer, os.X_OK):
            loaded = self._load_installer_at(local_installer, no_cache=no_cache)
            if loaded is not None:
                return loaded

        return self._install_installer_binary(no_cache=no_cache)

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.install_root:
            venv_python = self.install_root / "venv" / "bin" / "python"
            if venv_python.exists() and not (
                venv_python.is_file() and os.access(venv_python, os.X_OK)
            ):
                return False
        return super().is_valid

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Derive uv's managed virtualenv bin_dir from install_root when configured."""
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "venv" / "bin"
        return self

    @property
    def cache_dir(self) -> Path:
        """Return uv's shared download/build cache dir."""
        return abxpkg_cache_dir_default("uv") or Path(
            os.environ.get("UV_CACHE_DIR") or USER_CACHE_PATH,
        )

    def _cache_args(self, *, no_cache: bool = False) -> list[str]:
        if no_cache or not self._ensure_writable_cache_dir(self.cache_dir):
            return ["--no-cache"]
        return [f"--cache-dir={self.cache_dir}"]

    @property
    def tool_dir(self) -> Path:
        """Return uv's global tool install root used in ``uv tool`` mode."""
        return Path(
            os.environ.get("UV_TOOL_DIR")
            or (Path("~").expanduser() / ".local" / "share" / "uv" / "tools"),
        )

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from install_root/venv/bin in venv mode, or UV tool bin dirs in tool mode."""
        if self.install_root:
            bin_dir = self.bin_dir
            assert bin_dir is not None
            self.PATH = self._merge_PATH(
                bin_dir,
                PATH=self.PATH,
                prepend=True,
            )
        elif self.bin_dir:
            self.PATH = self._merge_PATH(
                self.bin_dir,
                PATH=self.PATH,
                prepend=True,
            )
        else:
            default_bin = Path(
                os.environ.get("UV_TOOL_BIN_DIR")
                or (Path("~").expanduser() / ".local" / "bin"),
            )
            self.PATH = self._merge_PATH(default_bin, PATH=self.PATH, prepend=True)
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
                owner_paths=(self.install_root, self.tool_dir, self.bin_dir),
                preserve_root=True,
            )
        self._ensure_writable_cache_dir(self.cache_dir)
        if self.install_root:
            self._ensure_venv(no_cache=no_cache)
        else:
            self.tool_dir.mkdir(parents=True, exist_ok=True)
            if self.bin_dir:
                self.bin_dir.mkdir(parents=True, exist_ok=True)

    def _ensure_venv(self, *, no_cache: bool = False) -> None:
        """Create the managed uv virtualenv on first use when install_root is pinned."""
        assert self.install_root is not None
        venv_root = self.install_root / "venv"
        venv_python = venv_root / "bin" / "python"
        if venv_python.is_file() and os.access(venv_python, os.X_OK):
            return
        self.install_root.parent.mkdir(parents=True, exist_ok=True)
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                *self._cache_args(no_cache=no_cache),
                "venv",
                str(venv_root),
            ],
            quiet=True,
            timeout=self.install_timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", ["uv venv"], proc)

    @staticmethod
    def _release_age_cutoff(min_release_age: float | None) -> str | None:
        """Translate ``min_release_age`` days into uv's ``--exclude-newer`` timestamp."""
        if min_release_age is None or min_release_age <= 0:
            return None
        from datetime import datetime, timedelta, timezone

        return (datetime.now(timezone.utc) - timedelta(days=min_release_age)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )

    def _pip_flags(
        self,
        *,
        install_args: InstallArgs,
        postinstall_scripts: bool,
        min_release_age: float | None,
    ) -> list[str]:
        """Build the shared ``uv pip`` security flags list."""
        combined = tuple(install_args)
        flags: list[str] = []
        if not postinstall_scripts and not any(
            arg == "--no-build" or arg.startswith("--no-build=") for arg in combined
        ):
            flags.append("--no-build")
        cutoff = self._release_age_cutoff(min_release_age)
        if cutoff and not any(
            arg == "--exclude-newer" or arg.startswith("--exclude-newer=")
            for arg in combined
        ):
            flags.append(f"--exclude-newer={cutoff}")
        return flags

    @staticmethod
    def _package_name_from_install_arg(install_arg: str) -> str | None:
        """Extract a bare Python package name from a uv install arg when possible."""
        if not install_arg or install_arg.startswith("-"):
            return None
        if "://" in install_arg:
            return None
        if install_arg.startswith((".", "/", "~")):
            return None
        package_name = re.split(r"[<>=!~;]", install_arg, maxsplit=1)[0]
        package_name = package_name.split("[", 1)[0].strip()
        return package_name or None

    def _package_name_for_bin(self, bin_name: BinName, **context) -> str:
        """Pick the owning Python package name used for uv metadata lookups."""
        install_args = self.get_install_args(str(bin_name), **context) or [
            str(bin_name),
        ]
        for install_arg in install_args:
            package_name = self._package_name_from_install_arg(install_arg)
            if package_name:
                return package_name
        return str(bin_name)

    def get_cache_info(
        self,
        bin_name: BinName,
        abspath: HostBinPath,
    ) -> dict[str, list[Path]] | None:
        cache_info = super().get_cache_info(bin_name, abspath)
        if cache_info is None or self.install_root is None:
            return cache_info

        package_name = self._package_name_for_bin(str(bin_name))
        normalized_name = package_name.lower().replace("-", "_")
        metadata_files = sorted(
            ((self.install_root / "venv") / "lib").glob(
                f"python*/site-packages/{normalized_name}*.dist-info/METADATA",
            ),
        ) or sorted(
            ((self.install_root / "venv") / "lib").glob(
                f"python*/site-packages/{normalized_name}*.dist-info/PKG-INFO",
            ),
        )
        if metadata_files:
            cache_info["fingerprint_paths"].append(metadata_files[0])
        return cache_info

    def _version_from_uv_metadata(
        self,
        package_name: str,
        timeout: int | None = None,
        no_cache: bool = False,
    ) -> SemVer | None:
        """Read a package version from ``uv pip show`` or ``uv tool list`` metadata."""
        try:
            uv_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert uv_abspath
        except Exception:
            return None
        if self.install_root:
            proc = self.exec(
                bin_name=uv_abspath,
                cmd=[
                    *self._cache_args(no_cache=no_cache),
                    "pip",
                    "show",
                    "--python",
                    str(self.install_root / "venv" / "bin" / "python"),
                    package_name,
                ],
                timeout=timeout,
                quiet=True,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if line.startswith("Version: "):
                        return SemVer.parse(line.split("Version: ", 1)[1])
            return None
        proc = self.exec(
            bin_name=uv_abspath,
            cmd=[*self._cache_args(no_cache=no_cache), "tool", "list"],
            timeout=timeout,
            quiet=True,
        )
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("-"):
                continue
            parts = line.split(" v", 1)
            if len(parts) == 2 and parts[0] == package_name:
                return SemVer.parse(parts[1])
        return None

    @staticmethod
    def _package_names_from_install_args(
        install_args: InstallArgs,
        fallback: str,
    ) -> list[str]:
        return [
            arg.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].split("<", 1)[0]
            for arg in install_args
            if arg and not arg.startswith("-")
        ] or [fallback]

    def _clear_venv_site_packages_pycache(self) -> None:
        if self.install_root is None:
            return
        for site_packages in ((self.install_root / "venv") / "lib").glob(
            "python*/site-packages",
        ):
            for pycache_dir in site_packages.rglob("__pycache__"):
                if pycache_dir.exists():
                    logger.info(
                        "$ %s",
                        format_command(["rm", "-rf", str(pycache_dir)]),
                    )
                shutil.rmtree(pycache_dir, ignore_errors=True)

    def default_search_handler(
        self,
        bin_name: BinName,
        min_version: SemVer | None = None,
        min_release_age: float | None = None,
        timeout: int | None = None,
        **context,
    ) -> list:
        """Resolve the latest published version for an exact PyPI package name via the PyPI JSON API.

        ``uv`` has no ``uv search`` subcommand and ``uv pip`` no longer
        ships ``index versions``, so we hit the same PyPI JSON endpoint
        ``uv pip install`` uses to resolve versions.
        """
        from .binary import Binary

        url = f"https://pypi.org/pypi/{urllib.parse.quote(str(bin_name))}/json"
        try:
            with urllib.request.urlopen(
                url,
                timeout=timeout or self.version_timeout,
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError:
            return []
        info = data.get("info", {}) or {}
        pkg_name = info.get("name", str(bin_name))
        version_str = info.get("version", "")
        summary = info.get("summary", "") or pkg_name
        return [
            Binary(
                name=pkg_name,
                description=f"{version_str} - {summary}".strip(" -"),
                binproviders=[self],
                overrides={self.name: {"install_args": [pkg_name]}},
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
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
        min_release_age = 7.0 if min_release_age is None else min_release_age
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}>={min_version}"
                if arg
                and not arg.startswith("-")
                and not any(c in arg for c in ">=<!=~")
                else arg
                for arg in install_args
            ]
        flags = self._pip_flags(
            install_args=install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        if self.install_root:
            if min_version:
                tool_names = self._package_names_from_install_args(
                    install_args,
                    bin_name,
                )
                installed_versions = [
                    self._version_from_uv_metadata(
                        package_name,
                        timeout=timeout,
                        no_cache=no_cache,
                    )
                    for package_name in tool_names
                ]
                if any(
                    version and version < min_version for version in installed_versions
                ):
                    uninstall_proc = self.exec(
                        bin_name=installer_bin,
                        cmd=[
                            "pip",
                            "uninstall",
                            "--python",
                            str(self.install_root / "venv" / "bin" / "python"),
                            *tool_names,
                        ],
                        timeout=timeout,
                    )
                    if (
                        uninstall_proc.returncode != 0
                        and "No packages to uninstall"
                        not in (uninstall_proc.stderr or "")
                    ):
                        self._raise_proc_error("install", tool_names, uninstall_proc)
                    # When install() is revalidating a too-old package, uv's
                    # in-place upgrade can leave importable stale bytecode even
                    # though package metadata has advanced. Remove pyc caches
                    # before reinstalling so the loaded CLI and metadata agree.
                    self._clear_venv_site_packages_pycache()
            cmd = [
                *self._cache_args(no_cache=no_cache),
                "pip",
                "install",
                "--python",
                str(self.install_root / "venv" / "bin" / "python"),
                *flags,
                *install_args,
            ]
        else:
            cmd = [
                *self._cache_args(no_cache=no_cache),
                "tool",
                "install",
                "--force",
                *flags,
                *install_args,
            ]
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
                f"{arg}>={min_version}"
                if arg
                and not arg.startswith("-")
                and not any(c in arg for c in ">=<!=~")
                else arg
                for arg in install_args
            ]
        flags = self._pip_flags(
            install_args=install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        if self.install_root:
            # Do an explicit uninstall + install cycle instead of
            # ``uv pip install --upgrade --reinstall`` so the venv's
            # site-packages is fully repopulated from scratch (uv's
            # in-place upgrade path can leave stale files otherwise).
            tool_names = self._package_names_from_install_args(
                install_args,
                bin_name,
            )
            uninstall_proc = self.exec(
                bin_name=installer_bin,
                cmd=[
                    "pip",
                    "uninstall",
                    "--python",
                    str(self.install_root / "venv" / "bin" / "python"),
                    *tool_names,
                ],
                timeout=timeout,
            )
            # Treat "no packages to uninstall" as a no-op success.
            if uninstall_proc.returncode != 0 and "No packages to uninstall" not in (
                uninstall_proc.stderr or ""
            ):
                self._raise_proc_error("update", tool_names, uninstall_proc)
            # Existing installs may contain bytecode from older abxpkg
            # versions. Remove it before reinstalling so stale bytecode cannot
            # shadow the freshly installed source.
            self._clear_venv_site_packages_pycache()
            cmd = [
                *self._cache_args(no_cache=no_cache),
                "pip",
                "install",
                "--python",
                str(self.install_root / "venv" / "bin" / "python"),
                *flags,
                *install_args,
            ]
        else:
            # ``uv tool install --force`` creates a fresh per-tool venv each
            # time, so there's no stale-compiled-artifact hazard.
            cmd = [
                *self._cache_args(no_cache=no_cache),
                "tool",
                "install",
                "--force",
                *flags,
                *install_args,
            ]
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
        # Strip version pins / extras from package specs so both
        # ``uv pip uninstall`` and ``uv tool uninstall`` get bare names.
        tool_names = [
            arg.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].split("<", 1)[0]
            for arg in install_args
            if arg and not arg.startswith("-")
        ] or [bin_name]
        if self.install_root:
            cmd = [
                "pip",
                "uninstall",
                "--python",
                str(self.install_root / "venv" / "bin" / "python"),
                *tool_names,
            ]
        else:
            cmd = ["tool", "uninstall", *tool_names]
        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", tool_names, proc)
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
            installer_binary = self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return None
        # Fallback: ``uv pip show`` for venv mode.
        if self.install_root:
            tool_name = self._package_name_for_bin(str(bin_name), **context)
            assert installer_binary.loaded_abspath
            proc = self.exec(
                bin_name=installer_binary.loaded_abspath,
                cmd=[
                    "pip",
                    "show",
                    "--python",
                    str(self.install_root / "venv" / "bin" / "python"),
                    tool_name,
                ],
                timeout=self.version_timeout,
                quiet=True,
            )
            if proc.returncode == 0:
                candidate = self.install_root / "venv" / "bin" / str(bin_name)
                if candidate.exists():
                    return TypeAdapter(HostBinPath).validate_python(candidate)
                site_packages_locations = [
                    Path(line.split("Location: ", 1)[1])
                    for line in proc.stdout.splitlines()
                    if line.startswith("Location: ")
                ]
                module_names = [tool_name.replace("-", "_").replace(".", "_")]
                for line in proc.stdout.splitlines():
                    if line.startswith("Name: "):
                        package_name = line.split("Name: ", 1)[1].strip()
                        normalized_package_name = package_name.replace(
                            "-",
                            "_",
                        ).replace(".", "_")
                        if normalized_package_name not in module_names:
                            module_names.append(normalized_package_name)
                for location in site_packages_locations:
                    for module_name in module_names:
                        for module_candidate in (
                            location / module_name / "__init__.py",
                            location / f"{module_name}.py",
                        ):
                            if module_candidate.exists():
                                return TypeAdapter(HostBinPath).validate_python(
                                    module_candidate,
                                )
        else:
            tool_name = self._package_name_for_bin(str(bin_name), **context)
            candidate = self.tool_dir / tool_name / "bin" / str(bin_name)
            if candidate.exists():
                return TypeAdapter(HostBinPath).validate_python(candidate)
        return None

    def default_docs_url_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> str | None:
        package = self._package_name_for_bin(str(bin_name), **context) or str(bin_name)
        if not package:
            return None
        return f"https://pypi.org/project/{package}"

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> SemVer | None:
        tool_name = self._package_name_for_bin(str(bin_name), **context)
        metadata_version = self._version_from_uv_metadata(
            tool_name,
            timeout=timeout,
            no_cache=no_cache,
        )
        if metadata_version:
            return metadata_version
        if abspath is None or os.access(abspath, os.X_OK):
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
        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_uv.py load black
    # ./binprovider_uv.py install black
    result = uv = UvProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(uv, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)

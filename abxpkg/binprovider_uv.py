#!/usr/bin/env python3
__package__ = "abxpkg"
import os
import shutil
import re
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
    bin_abspath,
)
from .binprovider import BinProvider, env_flag_is_true, log_method_call, remap_kwargs
from .logging import format_command, format_subprocess_output, get_logger
from .semver import SemVer
from .windows_compat import (
    VENV_BIN_SUBDIR,
    VENV_PYTHON_BIN,
    venv_site_packages_dirs,
)

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
            for sp in venv_site_packages_dirs(venv_root):
                env["PYTHONPATH"] = os.pathsep + str(sp)
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

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.install_root:
            venv_python = self.install_root / "venv" / VENV_BIN_SUBDIR / VENV_PYTHON_BIN
            if venv_python.exists() and not (
                venv_python.is_file() and os.access(venv_python, os.X_OK)
            ):
                return False
        return super().is_valid

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Derive uv's managed virtualenv bin_dir from install_root when configured."""
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "venv" / VENV_BIN_SUBDIR
        return self

    @property
    def cache_dir(self) -> Path:
        """Return uv's shared download/build cache dir."""
        return Path(USER_CACHE_PATH)

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
        venv_python = venv_root / VENV_BIN_SUBDIR / VENV_PYTHON_BIN
        if venv_python.is_file() and os.access(venv_python, os.X_OK):
            return
        self.install_root.parent.mkdir(parents=True, exist_ok=True)
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        cache_arg = (
            "--no-cache"
            if no_cache or not self._ensure_writable_cache_dir(self.cache_dir)
            else f"--cache-dir={self.cache_dir}"
        )
        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "venv",
                cache_arg,
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
        site_packages_dirs = venv_site_packages_dirs(self.install_root / "venv")
        metadata_files: list[Path] = []
        for sp in site_packages_dirs:
            metadata_files = sorted(
                sp.glob(f"{normalized_name}*.dist-info/METADATA"),
            ) or sorted(
                sp.glob(f"{normalized_name}*.dist-info/PKG-INFO"),
            )
            if metadata_files:
                break
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
                    "pip",
                    "show",
                    "--python",
                    str(self.install_root / "venv" / VENV_BIN_SUBDIR / VENV_PYTHON_BIN),
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
            cmd=["tool", "list"],
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
        cache_arg = (
            "--no-cache"
            if no_cache or not self._ensure_writable_cache_dir(self.cache_dir)
            else f"--cache-dir={self.cache_dir}"
        )
        if self.install_root:
            # ``--compile-bytecode`` tells uv to compile ``.pyc`` files at
            # install time, overwriting any stale bytecode that Python may
            # have previously auto-generated for an older version of the
            # same package (wheel-provided source mtimes can collide with
            # existing ``.pyc`` headers and defeat Python's mtime-based
            # invalidation). See ``default_update_handler`` for context.
            cmd = [
                "pip",
                "install",
                "--python",
                str(self.install_root / "venv" / VENV_BIN_SUBDIR / VENV_PYTHON_BIN),
                "--compile-bytecode",
                cache_arg,
                *flags,
                *install_args,
            ]
        else:
            cmd = [
                "tool",
                "install",
                "--force",
                cache_arg,
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
        cache_arg = (
            "--no-cache"
            if no_cache or not self._ensure_writable_cache_dir(self.cache_dir)
            else f"--cache-dir={self.cache_dir}"
        )
        if self.install_root:
            # Do an explicit uninstall + install cycle instead of
            # ``uv pip install --upgrade --reinstall`` so the venv's
            # site-packages is fully repopulated from scratch (uv's
            # in-place upgrade path can leave stale files otherwise).
            # ``--compile-bytecode`` forces uv to write fresh ``.pyc``
            # files at install time, which overwrites any stale bytecode
            # Python auto-generated earlier (wheel-provided source mtimes
            # can collide with existing ``.pyc`` headers and defeat
            # Python's mtime-based invalidation).
            tool_names = [
                arg.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].split("<", 1)[0]
                for arg in install_args
                if arg and not arg.startswith("-")
            ] or [bin_name]
            uninstall_proc = self.exec(
                bin_name=installer_bin,
                cmd=[
                    "pip",
                    "uninstall",
                    "--python",
                    str(self.install_root / "venv" / VENV_BIN_SUBDIR / VENV_PYTHON_BIN),
                    *tool_names,
                ],
                timeout=timeout,
            )
            # Treat "no packages to uninstall" as a no-op success.
            if uninstall_proc.returncode != 0 and "No packages to uninstall" not in (
                uninstall_proc.stderr or ""
            ):
                self._raise_proc_error("update", tool_names, uninstall_proc)
            # Belt-and-suspenders: ``--compile-bytecode`` below makes uv
            # rewrite ``.pyc`` files at install time, but on older uv
            # releases (and on some wheel layouts where the source mtime
            # is preserved across versions) the rewrite can be skipped if
            # uv decides the ``.pyc`` is "already up to date" against the
            # newly-written source. Wipe every ``__pycache__`` under the
            # venv's site-packages between the uninstall and the install
            # so Python is forced to recompile from the freshly-written
            # source. Targeted, not the whole venv.
            for site_packages in venv_site_packages_dirs(
                self.install_root / "venv",
            ):
                for pycache_dir in site_packages.rglob("__pycache__"):
                    if pycache_dir.exists():
                        logger.info(
                            "$ %s",
                            format_command(["rm", "-rf", str(pycache_dir)]),
                        )
                    shutil.rmtree(pycache_dir, ignore_errors=True)
            cmd = [
                "pip",
                "install",
                "--python",
                str(self.install_root / "venv" / VENV_BIN_SUBDIR / VENV_PYTHON_BIN),
                "--compile-bytecode",
                cache_arg,
                *flags,
                *install_args,
            ]
        else:
            # ``uv tool install --force`` creates a fresh per-tool venv each
            # time, so there's no stale-compiled-artifact hazard.
            cmd = [
                "tool",
                "install",
                "--force",
                cache_arg,
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
                str(self.install_root / "venv" / VENV_BIN_SUBDIR / VENV_PYTHON_BIN),
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
                    str(self.install_root / "venv" / VENV_BIN_SUBDIR / VENV_PYTHON_BIN),
                    tool_name,
                ],
                timeout=self.version_timeout,
                quiet=True,
            )
            if proc.returncode == 0:
                # ``bin_abspath`` wraps ``shutil.which`` which honors ``PATHEXT``
                # on Windows, so ``<bin>.exe`` / ``.cmd`` / ``.bat`` variants
                # dropped by pip/uv console-script install are resolved too.
                venv_bin_dir = self.install_root / "venv" / VENV_BIN_SUBDIR
                resolved = bin_abspath(str(bin_name), PATH=str(venv_bin_dir))
                if resolved is not None:
                    return TypeAdapter(HostBinPath).validate_python(resolved)
        else:
            tool_name = self._package_name_for_bin(str(bin_name), **context)
            tool_bin_dir = self.tool_dir / tool_name / VENV_BIN_SUBDIR
            resolved = bin_abspath(str(bin_name), PATH=str(tool_bin_dir))
            if resolved is not None:
                return TypeAdapter(HostBinPath).validate_python(resolved)
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
        tool_name = self._package_name_for_bin(str(bin_name), **context)
        return self._version_from_uv_metadata(
            tool_name,
            timeout=timeout,
            no_cache=no_cache,
        )


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

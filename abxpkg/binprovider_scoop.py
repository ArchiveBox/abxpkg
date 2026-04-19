#!/usr/bin/env python3
"""Scoop (https://scoop.sh) package manager provider — Windows ``brew`` equivalent.

``scoop install <pkg>`` drops binaries into ``%USERPROFILE%\\scoop\\shims``
which is already on ``PATH`` after Scoop's bootstrapper runs. It's a
user-scoped package manager (no UAC prompts), which makes it the closest
structural match to Homebrew on Unix. Only registered as a default
provider when ``IS_WINDOWS`` is true (see ``abxpkg/__init__.py``).
"""

__package__ = "abxpkg"

import os
from pathlib import Path

from pydantic import Field, TypeAdapter, computed_field

from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abxpkg_install_root_default,
    bin_abspath,
)
from .binprovider import BinProvider, remap_kwargs
from .logging import format_subprocess_output
from .semver import SemVer
from .windows_compat import link_binary


_USER_PROFILE = Path(os.environ.get("USERPROFILE") or str(Path.home()))
# Scoop's default single-user install prefix. ``ABXPKG_SCOOP_ROOT`` /
# ``ABXPKG_LIB_DIR`` override this via ``abxpkg_install_root_default``.
DEFAULT_SCOOP_ROOT = _USER_PROFILE / "scoop"


class ScoopProvider(BinProvider):
    """Installs Windows binaries via `Scoop <https://scoop.sh>`_.

    Maps each abxpkg lifecycle verb onto the matching ``scoop`` subcommand:
    ``install`` / ``update`` / ``uninstall``. Binaries land under
    ``<install_root>/apps/<pkg>/current/``; scoop itself publishes
    auto-generated PS/batch wrappers under ``<install_root>/shims/``
    while abxpkg's managed symlinks live under ``<install_root>/bin/``
    (same ``bin_dir`` convention as brew / cargo / gem / etc.).
    """

    name: BinProviderName = "scoop"
    _log_emoji = "🥄"
    INSTALLER_BIN: BinName = "scoop"

    # Starts seeded with the known layout dirs so resolution works even
    # before setup_PATH() runs: abxpkg's managed ``bin/`` dir first, then
    # scoop's native ``shims/`` (where its .ps1/.cmd wrappers live), then
    # the raw ``apps/`` tree as a last-resort lookup for the actual exes.
    PATH: PATHStr = os.pathsep.join(
        [
            str(DEFAULT_SCOOP_ROOT / "bin"),
            str(DEFAULT_SCOOP_ROOT / "shims"),
            str(DEFAULT_SCOOP_ROOT / "apps"),
        ],
    )

    install_root: Path | None = Field(
        default_factory=lambda: (
            abxpkg_install_root_default("scoop") or DEFAULT_SCOOP_ROOT
        ),
        validation_alias="scoop_root",
    )
    # bin_dir is unset until setup_PATH() resolves it from install_root,
    # then holds ``<install_root>/bin`` — the abxpkg-managed shim dir
    # where ``_link_loaded_binary`` writes symlinks/hardlinks to
    # scoop-installed binaries (mirrors brew / cargo / gem layout).
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        # Tell scoop to use our ``install_root`` for both SCOOP (user apps)
        # and SCOOP_GLOBAL (global apps). Keeping them identical avoids
        # accidentally writing to ``C:\\ProgramData\\scoop`` when running
        # under a privileged shell.
        if not self.install_root:
            return {}
        return {
            "SCOOP": str(self.install_root),
            "SCOOP_GLOBAL": str(self.install_root),
        }

    def setup_PATH(self, no_cache: bool = False) -> None:
        install_root = self.install_root
        if install_root is not None:
            if self.bin_dir is None:
                self.bin_dir = install_root / "bin"
            self.PATH = self._merge_PATH(
                install_root / "bin",
                install_root / "shims",
                install_root / "apps",
                PATH=self.PATH,
                prepend=True,
            )
        super().setup_PATH(no_cache=no_cache)

    def supports_min_release_age(self, action, no_cache: bool = False) -> bool:
        return False

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        return False

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        """Resolve a scoop-installed binary.

        The base class only searches ``bin_dir`` when it's set, but scoop
        drops its generated shims under ``<install_root>/shims/`` — not
        ``<install_root>/bin/``. So we check the managed ``bin/`` dir
        first for any previously linked shim, fall through to
        ``self.PATH`` (which includes ``shims/`` + ``apps/``) to locate
        scoop's own shim, then link the resolved path into ``bin_dir``
        so subsequent lookups hit the managed ``bin/`` symlink directly.
        """
        bin_name_str = str(bin_name)
        self.setup_PATH(no_cache=no_cache)
        abspath = None
        if self.bin_dir is not None:
            abspath = bin_abspath(bin_name_str, PATH=str(self.bin_dir))
        if not abspath:
            abspath = bin_abspath(bin_name_str, PATH=self.PATH)
        if not abspath:
            return None
        link_name = Path(bin_name_str).name
        if self.bin_dir is None or not link_name or link_name in {".", ".."}:
            return TypeAdapter(HostBinPath).validate_python(abspath)
        # ``link_binary`` internally short-circuits when source == link_path
        # so second-lookup ``abspath`` paths already in ``bin_dir`` don't
        # get unlinked + recreated (which would destroy hardlink/copy shims
        # on Windows).
        result = link_binary(Path(abspath), self.bin_dir / link_name)
        return TypeAdapter(HostBinPath).validate_python(result)

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
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        proc = self.exec(
            bin_name=installer_bin,
            cmd=["install", *install_args],
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
        install_args = install_args or self.get_install_args(bin_name)
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        proc = self.exec(
            bin_name=installer_bin,
            cmd=["update", *install_args],
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
        return True

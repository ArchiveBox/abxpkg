"""POSIX compatibility shims for Windows hosts.

abxpkg was written for Unix: it reads the ``pwd`` database, runs
subprocesses with ``preexec_fn=drop_privileges``, chowns cache dirs to
the calling user, and symlinks managed binaries into its ``bin_dir``.
None of those concepts map 1:1 onto Windows. Rather than sprinkling
``if IS_WINDOWS`` branches across every provider, everything that
differs between Windows and Unix is funnelled through the six functions
exposed here. Each one either does the real Unix thing or a sensible
Windows-equivalent no-op / fallback.

Also exposes ``IS_WINDOWS``, ``DEFAULT_PATH`` (OS-appropriate default
``PATH``), and ``UNIX_ONLY_PROVIDER_NAMES`` (the set of providers that
get filtered out of ``DEFAULT_PROVIDER_NAMES`` on Windows — see
``abxpkg/__init__.py``). The Windows ``brew`` equivalent is
:class:`~abxpkg.binprovider_scoop.ScoopProvider`, which lives in its own
module per the ``binprovider_*.py`` convention.
"""

from __future__ import annotations

__package__ = "abxpkg"

import os
import platform
import shutil
import stat
import subprocess
import tempfile
from collections import namedtuple
from pathlib import Path
from typing import Any
from collections.abc import Callable


IS_WINDOWS: bool = platform.system().lower() == "windows"

# Providers that can't meaningfully run on Windows — filtered out of
# ``ALL_PROVIDERS`` / ``DEFAULT_PROVIDER_NAMES`` in ``abxpkg/__init__.py``
# when ``IS_WINDOWS`` is true. ``scoop`` takes brew's place on Windows.
# ``docker`` is excluded because its install handler writes a ``/bin/sh``
# shim; the CLI itself works fine once installed outside abxpkg.
UNIX_ONLY_PROVIDER_NAMES: frozenset[str] = frozenset(
    {"apt", "brew", "nix", "bash", "ansible", "pyinfra", "docker"},
)

# Mirrors the 7-tuple layout of :class:`pwd.struct_passwd` so Unix and
# Windows call sites can treat the two interchangeably.
PwdRecord = namedtuple(
    "PwdRecord",
    "pw_name pw_passwd pw_uid pw_gid pw_gecos pw_dir pw_shell",
)


def _windows_default_path() -> str:
    """Reasonable default ``PATH`` for a Windows host, ``os.pathsep``-joined.

    Mirrors what stock ``cmd.exe`` / PowerShell sessions see (``System32``,
    Program Files) and appends the per-user install dirs that Scoop /
    Python / Cargo write to. Resolves ``%SystemRoot%`` / ``%ProgramFiles%``
    etc. dynamically so it also works on non-default drive letters.
    """
    windir = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app = os.environ.get(
        "LOCALAPPDATA",
        str(Path.home() / "AppData" / "Local"),
    )
    user_profile = os.environ.get("USERPROFILE", str(Path.home()))
    return os.pathsep.join(
        [
            rf"{windir}\System32",
            windir,
            rf"{windir}\System32\Wbem",
            rf"{windir}\System32\WindowsPowerShell\v1.0",
            rf"{program_files}\Git\cmd",
            rf"{program_files}\Git\bin",
            rf"{program_files}\nodejs",
            program_files,
            program_files_x86,
            rf"{local_app}\Microsoft\WindowsApps",
            rf"{local_app}\Programs\Python\Python313",
            rf"{local_app}\Programs\Python\Python313\Scripts",
            rf"{user_profile}\scoop\shims",
            rf"{user_profile}\.cargo\bin",
        ],
    )


DEFAULT_PATH: str = (
    _windows_default_path()
    if IS_WINDOWS
    else (
        "/home/linuxbrew/.linuxbrew/bin"
        ":/opt/homebrew/bin"
        ":/usr/local/sbin"
        ":/usr/local/bin"
        ":/usr/sbin"
        ":/usr/bin"
        ":/sbin"
        ":/bin"
    )
)


def get_current_euid() -> int:
    """Effective UID on Unix, ``-1`` sentinel on Windows.

    ``-1`` matches ``UNKNOWN_EUID`` in ``base_types`` — the downstream
    passwd/chown helpers treat anything ``< 0`` as "skip this step".
    """
    return -1 if IS_WINDOWS else os.geteuid()


def get_current_egid() -> int:
    """Effective GID on Unix, ``-1`` sentinel on Windows."""
    return -1 if IS_WINDOWS else os.getegid()


def uid_has_passwd_entry(uid: int) -> bool:
    """Whether ``pwd.getpwuid(uid)`` would succeed.

    Windows has no passwd database — treat every UID as valid so the
    EUID heuristics in :class:`BinProvider` don't wrongly bail out.
    """
    if IS_WINDOWS:
        return True
    import pwd

    try:
        pwd.getpwuid(uid)
    except KeyError:
        return False
    return True


def get_pw_record(uid: int) -> Any:
    """Return a ``pwd.struct_passwd``-compatible record for ``uid``.

    Unix: delegates to :func:`pwd.getpwuid`, falling back to a synthesized
    record when the current-user UID has no passwd entry (e.g. uid-mapped
    containers). Windows: always synthesizes from ``USERNAME`` /
    ``USERPROFILE`` / ``COMSPEC`` since there is no passwd DB at all.
    """
    if not IS_WINDOWS:
        import pwd

        try:
            return pwd.getpwuid(uid)
        except KeyError:
            if uid != os.geteuid():
                raise
            return pwd.struct_passwd(
                (
                    os.environ.get("USER") or os.environ.get("LOGNAME") or str(uid),
                    "x",
                    uid,
                    os.getegid(),
                    "",
                    os.environ.get("HOME", tempfile.gettempdir()),
                    os.environ.get("SHELL", "/bin/sh"),
                ),
            )

    name = (
        os.environ.get("USERNAME")
        or os.environ.get("USER")
        or os.environ.get("LOGNAME")
        or (str(uid) if uid >= 0 else "user")
    )
    home = (
        os.environ.get("USERPROFILE")
        or os.environ.get("HOME")
        or str(Path.home())
        or tempfile.gettempdir()
    )
    shell = os.environ.get("COMSPEC") or os.environ.get("SHELL") or ""
    safe_uid = uid if uid >= 0 else 0
    return PwdRecord(name, "x", safe_uid, safe_uid, "", home, shell)


def ensure_writable_cache_dir(
    cache_dir: Path,
    uid: int,
    gid: int,
) -> bool:
    """Create ``cache_dir`` and ensure ``uid``/``gid`` can write to it.

    Unix: chown + chmod group/world-writable so cross-user caches keep
    working under ``sudo``. Windows: NTFS ACL inheritance already gives
    the creating user full control, so we just mkdir and return.
    """
    if cache_dir.exists() and not cache_dir.is_dir():
        return False
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not IS_WINDOWS and uid >= 0 and gid >= 0:
        try:
            os.chown(cache_dir, uid, gid)
        except (PermissionError, OSError):
            pass
        try:
            cache_dir.chmod(
                cache_dir.stat().st_mode | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH,
            )
        except (PermissionError, OSError):
            pass

    return cache_dir.is_dir() and os.access(cache_dir, os.W_OK)


def drop_privileges_preexec(uid: int, gid: int) -> Callable[[], None] | None:
    """Return a ``preexec_fn`` that drops the child to ``(uid, gid)``.

    ``subprocess`` explicitly rejects a non-``None`` ``preexec_fn`` on
    Windows, and ``os.setuid`` / ``os.setgid`` don't exist there, so we
    return ``None`` (which ``subprocess`` accepts happily) whenever the
    platform is Windows or the caller passed the ``UNKNOWN_EUID`` (``-1``)
    sentinel. The same escape hatch is what prevents the sudo branch in
    :meth:`BinProvider.exec` from ever firing on Windows.
    """
    if IS_WINDOWS or uid < 0 or gid < 0:
        return None

    def _drop() -> None:
        try:
            os.setuid(uid)
            os.setgid(gid)
        except Exception:
            pass

    return _drop


def link_binary(source: Path, link_path: Path) -> Path:
    """Point ``link_path`` at ``source`` and return the path to expose.

    Unix: creates (or refreshes) a symlink. Windows: ``symlink_to``
    requires Administrator or Developer Mode so we try it first, then
    fall back to a hardlink (same volume only), then a plain file copy,
    then — if everything fails — return ``source`` unchanged so the
    binary is still usable even when no managed shim could be written.
    """
    source = Path(source).expanduser().absolute()

    if link_path.exists() or link_path.is_symlink():
        # Guard against ``source == link_path``: on Windows the managed
        # shim is typically a hardlink or copy (since ``symlink_to`` needs
        # admin / dev mode), so the ``is_symlink()`` early-return below
        # would miss it and we'd ``unlink`` the only copy of the binary.
        if source == link_path.expanduser().absolute():
            return link_path
        try:
            if link_path.is_symlink() and link_path.readlink() == source:
                return link_path
        except OSError:
            pass
        try:
            link_path.unlink()
        except OSError:
            return source

    link_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        link_path.symlink_to(source)
        return link_path
    except (OSError, NotImplementedError):
        pass

    if IS_WINDOWS:
        # Windows's hardlink/copy fallback breaks venv-aware Python
        # interpreters: CPython uses the invoked path to find its
        # ``pyvenv.cfg``, so a hardlinked or copied ``python.exe`` sitting
        # outside its venv's ``Scripts`` dir can't locate that config and
        # runs as a plain system Python (``VIRTUAL_ENV`` / ``site-packages``
        # become wrong, which cascades into every downstream provider
        # that resolves Python). When ``source`` lives inside a venv,
        # skip the copy and return ``source`` unchanged so the caller
        # still gets a working venv-aware interpreter — just not a
        # managed shim path.
        if not (
            (source.parent / "pyvenv.cfg").exists()
            or (source.parent.parent / "pyvenv.cfg").exists()
        ):
            try:
                os.link(source, link_path)
                return link_path
            except OSError:
                pass
            try:
                shutil.copy2(source, link_path)
                return link_path
            except OSError:
                pass

    return source


def chown_recursive(sudo_bin: str, path: Path, uid: int, gid: int) -> int:
    """``sudo chown -R uid:gid path``; no-op returning 0 on Windows.

    Only used by the ansible / pyinfra providers after a privileged
    install writes root-owned files into a user-writable temp dir.
    """
    if IS_WINDOWS or uid < 0 or gid < 0:
        return 0
    return subprocess.run(
        [sudo_bin, "-n", "chown", "-R", f"{uid}:{gid}", str(path)],
        capture_output=True,
        text=True,
    ).returncode


# -------------------------------------------------------------------------
# Python venv layout (used by PipProvider / UvProvider)
#
# CPython's ``venv`` module writes a different on-disk layout on Windows
# vs. everything else:
#
#    layout       | Unix                          | Windows
#    scripts dir  | ``<venv>/bin``                | ``<venv>/Scripts``
#    python exe   | ``python`` (no suffix)        | ``python.exe``
#    pip exe      | ``pip``                       | ``pip.exe``
#    site-packages| ``<venv>/lib/pythonX.Y/sp``   | ``<venv>/Lib/sp``
#                 |  (one versioned subdir)       |  (flat)
#
# Centralized here so every managed-venv provider agrees on the paths
# regardless of platform.

VENV_BIN_SUBDIR: str = "Scripts" if IS_WINDOWS else "bin"
_EXE_SUFFIX: str = ".exe" if IS_WINDOWS else ""
VENV_PYTHON_BIN: str = f"python{_EXE_SUFFIX}"
VENV_PIP_BIN: str = f"pip{_EXE_SUFFIX}"


def venv_site_packages_dirs(venv_root: Path) -> list[Path]:
    """Resolve a venv's ``site-packages`` dirs regardless of OS layout.

    Unix: ``<venv>/lib/pythonX.Y/site-packages`` (versioned subdir).
    Windows: ``<venv>/Lib/site-packages`` (flat, no Python version).
    Returned list is sorted and may be empty when the venv hasn't been
    created yet.
    """
    unix = sorted((venv_root / "lib").glob("python*/site-packages"))
    if unix:
        return unix
    windows = venv_root / "Lib" / "site-packages"
    return [windows] if windows.is_dir() else []


def scripts_dir_from_site_packages(site_packages: Path) -> Path:
    """Navigate from a ``site-packages`` path to the matching scripts dir.

    Unix layout is ``<prefix>/lib/pythonX.Y/site-packages`` — three
    parents up lands at ``<prefix>``. Windows is ``<prefix>/Lib/
    site-packages`` — only two parents up. Appends the OS-appropriate
    ``VENV_BIN_SUBDIR`` either way.
    """
    prefix = (
        site_packages.parent.parent
        if IS_WINDOWS
        else site_packages.parent.parent.parent
    )
    return prefix / VENV_BIN_SUBDIR

from __future__ import annotations

import os
import shutil
import sys
import time
import tomllib
from pathlib import Path
from typing import Any


_OPTS_WITH_VALUES = {
    "--lib",
    "--binproviders",
    "--dry-run",
    "--debug",
    "--no-cache",
    "--min-version",
    "--postinstall-scripts",
    "--min-release-age",
    "--overrides",
    "--install-root",
    "--bin-dir",
    "--euid",
    "--install-timeout",
    "--version-timeout",
    "--abspath",
    "--version",
    "--install-args",
    "--packages",
}
_UNSAFE_FLAGS = {
    "--dry-run",
    "--no-cache",
    "--update",
    "--global",
    "--install-root",
    "--bin-dir",
    "--euid",
    "--abspath",
    "--version",
    "--install-args",
    "--packages",
    "--overrides",
}
_TRUTHY = {"1", "true", "yes", "on"}


class _Fallback(Exception):
    pass


def _parse_script_metadata(
    script_path: Path,
    max_lines: int = 50,
) -> dict[str, Any] | None:
    try:
        lines = script_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    scan_limit = min(len(lines), max_lines)
    block_start: int | None = None
    for i in range(scan_limit):
        if "/// script" in lines[i]:
            block_start = i + 1
            break
    if block_start is None:
        return None
    block_end: int | None = None
    for i in range(block_start, len(lines)):
        stripped = lines[i].strip()
        if stripped.endswith("///") and "/// script" not in stripped:
            block_end = i
            break
    if block_end is None:
        return None
    toml_lines: list[str] = []
    for i in range(block_start, block_end):
        stripped = lines[i].strip()
        parts = stripped.split(None, 1)
        toml_lines.append(parts[1] if len(parts) > 1 else "")
    try:
        return tomllib.loads("\n".join(toml_lines))
    except Exception:
        return None


def _pop_option_value(argv: list[str], index: int) -> tuple[str | None, int]:
    token = argv[index]
    if "=" in token:
        return token.split("=", 1)[1], index + 1
    if index + 1 >= len(argv):
        return None, index + 1
    return argv[index + 1], index + 2


def _bool_option_is_true(argv: list[str], index: int) -> tuple[bool, int]:
    token = argv[index]
    if "=" in token:
        return token.split("=", 1)[1].strip().lower() in _TRUTHY, index + 1
    if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
        return argv[index + 1].strip().lower() in _TRUTHY, index + 2
    return True, index + 1


def _parse_fast_argv(argv: list[str]) -> tuple[dict[str, str], str, Path, list[str]]:
    options: dict[str, str] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in {"run", "exec"}:
            i += 1
            break
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            if name in _UNSAFE_FLAGS:
                raise _Fallback
            if name in _OPTS_WITH_VALUES:
                value, i = _pop_option_value(argv, i)
                if value is not None:
                    options[name] = value
                continue
            i += 1
            continue
        raise _Fallback
    else:
        raise _Fallback

    script_mode = False
    while i < len(argv):
        token = argv[i]
        if token == "--script":
            script_mode = True
            i += 1
            continue
        if token == "--install":
            i += 1
            continue
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            if name in _UNSAFE_FLAGS:
                raise _Fallback
            if name in _OPTS_WITH_VALUES:
                value, i = _pop_option_value(argv, i)
                if value is not None:
                    options[name] = value
                continue
            i += 1
            continue
        break

    if not script_mode or i >= len(argv):
        raise _Fallback
    binary_name = argv[i]
    script_args = argv[i + 1 :]
    if not script_args:
        raise _Fallback
    return options, binary_name, Path(script_args[0]), script_args


def _prepend_path(env: dict[str, str], *paths: Path) -> None:
    existing = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    merged: list[str] = []
    seen: set[str] = set()
    for raw_path in [*(str(path) for path in paths if path.exists()), *existing]:
        if raw_path in seen:
            continue
        seen.add(raw_path)
        merged.append(raw_path)
    env["PATH"] = os.pathsep.join(merged)


def _append_env_path(env: dict[str, str], key: str, value: Path) -> None:
    value_str = str(value)
    existing = [part for part in env.get(key, "").split(os.pathsep) if part]
    if value_str not in existing:
        env[key] = os.pathsep.join([*existing, value_str]) if existing else value_str


def _venv_site_packages(venv_root: Path) -> list[Path]:
    lib_dir = venv_root / "lib"
    if not lib_dir.is_dir():
        return []
    return [
        site_packages
        for site_packages in sorted(lib_dir.glob("python*/site-packages"))
        if site_packages.is_dir()
    ]


def _site_packages_pth_paths(site_packages: Path) -> list[Path]:
    paths: list[Path] = []
    try:
        pth_files = sorted(site_packages.glob("*.pth"))
    except OSError:
        return paths
    for pth_file in pth_files:
        try:
            lines = pth_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            entry = line.strip()
            if (
                not entry
                or entry.startswith("#")
                or entry.startswith("import ")
                or entry.startswith("import\t")
            ):
                continue
            path = Path(entry).expanduser()
            if not path.is_absolute():
                path = site_packages / path
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved.exists():
                paths.append(resolved)
    return paths


def _append_venv_import_paths(env: dict[str, str], venv_root: Path) -> None:
    for site_packages in _venv_site_packages(venv_root):
        _append_env_path(env, "PYTHONPATH", site_packages)
        for pth_path in _site_packages_pth_paths(site_packages):
            _append_env_path(env, "PYTHONPATH", pth_path)


def _is_python_binary(binary_name: str) -> bool:
    return binary_name in {"python", "python3"} or binary_name.startswith("python3.")


def _apply_abxpkg_lib_env(
    env: dict[str, str],
    options: dict[str, str],
    binary_name: str,
) -> None:
    raw_lib = options.get("--lib") or env.get("ABXPKG_LIB_DIR") or env.get("LIB_DIR")
    if not raw_lib:
        return
    lib_dir = Path(raw_lib).expanduser().resolve()
    active_py_env = (
        Path(env["ACTIVE_PY_ENV"]).expanduser().resolve()
        if env.get("ACTIVE_PY_ENV")
        else None
    )
    use_active_python = bool(
        active_py_env and env.get("ACTIVE_PY_BIN") and _is_python_binary(binary_name),
    )
    inherited_venvs = []
    for key in ("VIRTUAL_ENV", "ACTIVE_PY_ENV"):
        if not env.get(key):
            continue
        inherited_venv = Path(env[key]).expanduser().resolve()
        if inherited_venv not in inherited_venvs:
            inherited_venvs.append(inherited_venv)
    env["ABXPKG_LIB_DIR"] = str(lib_dir)
    if env.get("LIB_DIR"):
        env["LIB_DIR"] = str(Path(env["LIB_DIR"]).expanduser().resolve())

    providers = options.get("--binproviders") or env.get("ABXPKG_BINPROVIDERS", "")
    provider_names = {name.strip() for name in providers.split(",") if name.strip()}
    if not provider_names:
        provider_names = {"env", "uv", "pnpm", "npm"}

    path_entries = []
    lib_bin_dir = Path(env.get("LIB_BIN_DIR", lib_dir / "bin"))
    path_entries.append(lib_bin_dir)
    if "env" in provider_names:
        path_entries.append(lib_dir / "env" / "bin")
    if "uv" in provider_names:
        uv_root = Path(env.get("ABXPKG_UV_ROOT", lib_dir / "uv"))
        uv_venv = uv_root / "venv"
        env["UV_ACTIVE"] = "1"
        default_cache_root = (
            Path.home() / "Library" / "Caches"
            if sys.platform == "darwin"
            else Path(env.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        )
        env["UV_CACHE_DIR"] = str(default_cache_root / "abxpkg" / "uv")
        env["VIRTUAL_ENV"] = str(uv_venv)
        path_entries.append(uv_venv / "bin")
        _append_venv_import_paths(env, uv_venv)
        for inherited_venv in inherited_venvs:
            if inherited_venv == uv_venv:
                continue
            if use_active_python and inherited_venv == active_py_env:
                continue
            _append_venv_import_paths(env, inherited_venv)
    if "pnpm" in provider_names:
        pnpm_root = Path(env.get("ABXPKG_PNPM_ROOT", lib_dir / "pnpm"))
        pnpm_bin = pnpm_root / "node_modules" / ".bin"
        env.setdefault("PNPM_HOME", str(pnpm_bin))
        path_entries.append(pnpm_bin)
        _append_env_path(env, "NODE_PATH", pnpm_root / "node_modules")
    if "npm" in provider_names:
        npm_root = Path(env.get("ABXPKG_NPM_ROOT", lib_dir / "npm"))
        path_entries.append(npm_root / "node_modules" / ".bin")
        _append_env_path(env, "NODE_PATH", npm_root / "node_modules")
    _prepend_path(env, *path_entries)


def try_fast_script_run(argv: list[str] | None = None) -> bool:
    started_at = time.perf_counter_ns()
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        options, binary_name, script_path, script_args = _parse_fast_argv(argv)
        if not script_path.is_file():
            raise _Fallback
        meta = _parse_script_metadata(script_path)
        if meta is None or meta.get("dependencies"):
            raise _Fallback
        tool_section = meta.get("tool")
        tool_config = (
            tool_section.get("abxpkg", {}) if isinstance(tool_section, dict) else {}
        )
        if not isinstance(tool_config, dict):
            raise _Fallback

        env = os.environ.copy()
        for key, value in tool_config.items():
            if isinstance(value, (str, int, float, bool)):
                env.setdefault(str(key), str(value))
            else:
                raise _Fallback
        _apply_abxpkg_lib_env(env, options, binary_name)
        active_py_bin = env.get("ACTIVE_PY_BIN")
        if active_py_bin and _is_python_binary(binary_name):
            active_py_path = Path(active_py_bin).expanduser()
            executable = str(active_py_path) if active_py_path.is_file() else None
        else:
            executable = shutil.which(binary_name, path=env.get("PATH"))
        if executable is None:
            raise _Fallback
        env["ABXPKG_FAST_SCRIPT"] = "1"
        env["ABXPKG_FAST_SCRIPT_OVERHEAD_NS"] = str(time.perf_counter_ns() - started_at)
        os.execvpe(executable, [executable, *script_args], env)
    except _Fallback:
        return False
    return True


def main() -> None:
    if try_fast_script_run():
        return
    from .cli import main as cli_main

    cli_main()


def abx_main() -> None:
    from .cli import abx_main as cli_abx_main

    cli_abx_main()


__all__ = ["abx_main", "main", "try_fast_script_run"]

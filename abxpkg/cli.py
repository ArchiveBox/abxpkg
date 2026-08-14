from __future__ import annotations

import json
import os
import re
import stat
import sys

# Keep typing-only imports off the warm CLI path.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, cast
else:

    def cast(_type, value):
        return value


_NONE_STRINGS = frozenset({"", "none", "null"})
_HANDLER_KEYS = frozenset({"abspath", "version", "install_args", "packages"})
_VALUE_OPTIONS = frozenset(
    {
        "--lib",
        "--binproviders",
        "--overrides",
        "--abspath",
        "--version",
        "--install-args",
        "--packages",
        "--postinstall-scripts",
        "--min-release-age",
        "--install-timeout",
        "--version-timeout",
        "--deps-from",
        "--no-cache",
        "--debug",
        "--dry-run",
    },
)


class ScriptOptions:
    """Minimal option surface used before the full CLI is imported."""

    dry_run = False
    debug = False
    no_cache = False
    min_version = None
    postinstall_scripts = None
    min_release_age = None
    overrides = None
    install_root = None
    bin_dir = None
    euid = None
    install_timeout = None
    version_timeout = None

    def __init__(self, *, lib_dir: str, provider_names: list[str]) -> None:
        self.lib_dir = lib_dir
        self.provider_names = provider_names


def _none_or_stripped(raw: str | None) -> str | None:
    if raw is None:
        return None
    stripped = raw.strip()
    return None if stripped.lower() in _NONE_STRINGS else stripped


def _env_flag_is_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_lib_dir(raw_value: str | None) -> str:
    env_value = os.environ.get("ABXPKG_LIB_DIR")
    if _none_or_stripped(raw_value) is None and raw_value is not None:
        os.environ.pop("ABXPKG_LIB_DIR", None)
        from .config import default_abxpkg_lib_dir

        return os.path.realpath(os.path.expanduser(default_abxpkg_lib_dir()))
    if _none_or_stripped(env_value) is None and env_value is not None:
        os.environ.pop("ABXPKG_LIB_DIR", None)
        from .config import default_abxpkg_lib_dir

        return os.path.realpath(os.path.expanduser(default_abxpkg_lib_dir()))

    if raw_value or _none_or_stripped(env_value):
        lib_dir = raw_value or str(env_value)
    else:
        from .config import default_abxpkg_lib_dir

        lib_dir = os.fspath(default_abxpkg_lib_dir())
    resolved = os.path.realpath(os.path.expanduser(lib_dir))
    os.environ["ABXPKG_LIB_DIR"] = resolved
    return resolved


def merge_binary_overrides(
    base: dict[str, Any] | None,
    override: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge Binary.overrides maps with the second argument taking precedence."""

    if not base:
        return json.loads(json.dumps(override)) if override else None
    merged = json.loads(json.dumps(base))
    if not override:
        return merged

    stack: list[tuple[dict[str, Any], dict[str, Any]]] = [(merged, override)]
    while stack:
        target, source = stack.pop()
        for key, value in source.items():
            existing = target.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                stack.append((existing, value))
            else:
                target[key] = value
    return merged


def normalize_binary_overrides(
    provider_names: list[str],
    *,
    overrides: dict[str, Any] | None = None,
    handler_overrides: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Normalize every override spelling into Binary.overrides before use.

    Cache keys are derived from provider state, so CLI aliases like --abspath
    and the equivalent --overrides JSON must converge before providers are
    constructed. Keeping only one representation prevents stale-cache bugs
    where two user-facing spellings accidentally assemble different contexts.
    """

    provider_defaults = (
        {provider_name: dict(handler_overrides) for provider_name in provider_names}
        if handler_overrides
        else None
    )
    return merge_binary_overrides(provider_defaults, overrides)


def parse_script_metadata(
    script_path: str | os.PathLike[str],
    max_lines: int = 50,
) -> dict[str, Any] | None:
    import tomllib

    script_path = os.fspath(script_path)
    try:
        with open(script_path, encoding="utf-8", errors="replace") as script_file:
            text = script_file.read()
    except OSError as err:
        raise RuntimeError(f"cannot read script {script_path}: {err}") from err

    lines = text.splitlines()
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
        parts = lines[i].strip().split(None, 1)
        toml_lines.append(parts[1] if len(parts) > 1 else "")

    try:
        return tomllib.loads("\n".join(toml_lines))
    except Exception as err:
        raise RuntimeError(
            f"invalid TOML in /// script block of {script_path}: {err}",
        ) from err


def _pop_option_value(argv: list[str], index: int) -> tuple[str | None, int]:
    token = argv[index]
    if "=" in token:
        return token.split("=", 1)[1], index + 1
    if index + 1 >= len(argv):
        return None, index + 1
    return argv[index + 1], index + 2


def _parse_script_argv(
    argv: list[str],
) -> tuple[dict[str, str], str, str, list[str]] | None:
    options: dict[str, str] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in {"run", "exec"}:
            i += 1
            break
        if token == "--update":
            return None
        if token == "--install":
            i += 1
            continue
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            if name in _VALUE_OPTIONS:
                value, i = _pop_option_value(argv, i)
                if value is not None:
                    options[name] = value
                continue
            i += 1
            continue
        return None
    else:
        return None

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
        if token == "--update":
            return None
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            if name in _VALUE_OPTIONS:
                value, i = _pop_option_value(argv, i)
                if value is not None:
                    options[name] = value
                continue
            i += 1
            continue
        break

    if not script_mode or i + 1 >= len(argv):
        return None
    binary_name = argv[i]
    script_args = argv[i + 1 :]
    return options, binary_name, script_args[0], script_args


def _script_dependency_paths(
    raw_value: str | None,
    script_path: str | os.PathLike[str],
) -> list[Path]:
    from pathlib import Path

    resolved_script = Path(script_path)
    paths: list[Path] = []
    for raw_spec in (raw_value or "").split(","):
        spec = raw_spec.strip()
        if not spec:
            continue
        raw_path, _, _selector = spec.partition(":")
        path = Path(raw_path)
        if not path.is_absolute():
            path = resolved_script.parent / path
        paths.append(path.expanduser().resolve(strict=False))
    return paths


def _script_cache_context(
    raw_options: dict[str, str],
    binary_name: str,
    script_path: str | os.PathLike[str],
    meta: dict[str, Any],
    options: Any,
) -> tuple[str, list[Path]] | None:
    from pathlib import Path

    if set(raw_options) - {"--lib", "--binproviders", "--deps-from"}:
        return None
    base_context = warm_run_context(options)
    if base_context is None:
        return None

    resolved_script = Path(script_path).expanduser().resolve(strict=False)
    dependency_paths = _script_dependency_paths(
        raw_options.get("--deps-from"),
        resolved_script,
    )
    template_text = json.dumps(meta, separators=(",", ":"), sort_keys=True)
    try:
        template_text += "".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in dependency_paths
        )
    except OSError:
        return None
    template_env_names = sorted(
        set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template_text)),
    )
    tool_section = meta.get("tool")
    tool_config = (
        tool_section.get("abxpkg", {}) if isinstance(tool_section, dict) else {}
    )
    tool_env_names = sorted(
        str(key) for key in tool_config if key != "runtime_binproviders"
    )
    context = json.dumps(
        {
            "base": json.loads(base_context),
            "binary_name": binary_name,
            "script_path": str(resolved_script),
            "deps_from": raw_options.get("--deps-from"),
            "template_env": {name: os.environ.get(name) for name in template_env_names},
            "tool_env": {name: os.environ.get(name) for name in tool_env_names},
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return context, [resolved_script, *dependency_paths]


def warm_run_context(options: Any) -> str | None:
    """Return the canonical context for CLI runs eligible for direct cache exec."""
    if any(
        (
            options.dry_run,
            options.debug,
            options.no_cache,
            options.min_version is not None,
            options.postinstall_scripts is not None,
            options.min_release_age is not None,
            options.overrides is not None,
            options.install_root is not None,
            options.bin_dir is not None,
            options.euid is not None,
            options.install_timeout is not None,
            options.version_timeout is not None,
        ),
    ):
        return None
    return json.dumps(
        {
            "lib_dir": str(options.lib_dir),
            "provider_names": list(options.provider_names),
            "abxpkg_env": {
                key: value
                for key, value in sorted(os.environ.items())
                if key.startswith("ABXPKG_")
                and key not in {"ABXPKG_LIB_DIR", "ABXPKG_BINPROVIDERS"}
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _parse_warm_run_argv(
    argv: list[str],
) -> tuple[str | None, str | None, str, list[str]] | None:
    lib_dir: str | None = None
    provider_names: str | None = None
    command_seen = False
    i = 0
    while i < len(argv):
        token = argv[i]
        if not command_seen and token in {"run", "exec"}:
            command_seen = True
            i += 1
            continue
        if token.startswith("--lib") or token.startswith("--binproviders"):
            name = token.split("=", 1)[0]
            if name not in {"--lib", "--binproviders"}:
                return None
            value, i = _pop_option_value(argv, i)
            if value is None:
                return None
            if name == "--lib":
                lib_dir = value
            else:
                provider_names = value
            continue
        if token.startswith("-") or not command_seen:
            return None
        return lib_dir, provider_names, token, argv[i + 1 :]
    return None


def _fingerprints_match(raw_fingerprints: object) -> bool:
    if not isinstance(raw_fingerprints, list) or not raw_fingerprints:
        return False
    for raw_fingerprint in raw_fingerprints:
        if not isinstance(raw_fingerprint, dict):
            return False
        fingerprint = cast(dict[str, object], raw_fingerprint)
        raw_path = fingerprint.get("path")
        if not isinstance(raw_path, str):
            return False
        try:
            stat_result = os.stat(raw_path)
        except OSError:
            return False
        if fingerprint != {
            "path": os.path.realpath(os.path.expanduser(raw_path)),
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "mode": stat.S_IMODE(stat_result.st_mode),
            "euid": stat_result.st_uid,
        }:
            return False
    return True


def _cached_records(
    lib_dir: str | os.PathLike[str],
    provider_names: list[str],
    binary_name: str,
):
    for provider_name in provider_names:
        derived_env_path = os.path.join(lib_dir, provider_name, "derived.env")
        try:
            fd = os.open(
                derived_env_path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
        except OSError:
            continue
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_mode & 0o022
            ):
                continue
            with os.fdopen(fd, encoding="utf-8") as cache_file:
                fd = -1
                contents = cache_file.read()
                after = os.fstat(cache_file.fileno())
            stable_fields = (
                "st_dev",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
                "st_uid",
                "st_mode",
            )
            if any(
                getattr(before, field) != getattr(after, field)
                for field in stable_fields
            ):
                continue
            cache = _load_current_derived_cache_text(contents)
        except OSError:
            continue
        finally:
            if fd >= 0:
                os.close(fd)
        for record in cache.values():
            if (
                isinstance(record, dict)
                and record.get("provider_name") == provider_name
                and record.get("bin_name") == binary_name
                and _fingerprints_match(record.get("fingerprint"))
            ):
                yield record


def _load_current_derived_cache_text(
    contents: str,
) -> dict[str, dict[str, object]]:
    prefix = "ABXPKG_DERIVED_CACHE="
    raw_value = next(
        (
            line[len(prefix) :]
            for line in contents.splitlines()
            if line.startswith(prefix)
        ),
        "",
    )
    if raw_value.startswith("'") and raw_value.endswith("'"):
        raw_value = raw_value[1:-1].replace("'\"'\"'", "'")
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _find_executable(name: str, path: str) -> str | None:
    candidates = (
        (name,)
        if os.path.dirname(name)
        else tuple(
            os.path.join(directory, name) for directory in path.split(os.pathsep)
        )
    )
    return next(
        (
            candidate
            for candidate in candidates
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK)
        ),
        None,
    )


def _validated_cached_plan(
    raw_plan: object,
    run_context: str,
) -> tuple[str, dict[str, str]] | None:
    if os.getuid() != os.geteuid():
        return None
    if not isinstance(raw_plan, dict):
        return None
    exec_plan = cast(dict[str, object], raw_plan)
    if (
        exec_plan.get("version") != 5
        or exec_plan.get("run_context") != run_context
        or exec_plan.get("euid") != os.geteuid()
        or not _fingerprints_match(exec_plan.get("fingerprint"))
    ):
        return None
    exec_abspath = exec_plan.get("abspath")
    is_script = exec_plan.get("script")
    env = exec_plan.get("env")
    env_base = exec_plan.get("env_base")
    resolutions = exec_plan.get("resolutions")
    if (
        not isinstance(exec_abspath, str)
        or not os.path.isabs(exec_abspath)
        or not os.access(exec_abspath, os.X_OK)
        or not isinstance(is_script, bool)
        or not isinstance(env, dict)
        or not isinstance(env_base, dict)
        or not isinstance(resolutions, list)
        or not resolutions
        or any(not isinstance(key, str) for key in env)
        or any(not isinstance(value, str) for value in env.values())
        or any(not isinstance(key, str) for key in env_base)
        or any(
            value is not None and not isinstance(value, str)
            for value in env_base.values()
        )
    ):
        return None
    typed_env = cast(dict[str, str], env)
    typed_env_base = cast(dict[str, str | None], env_base)
    if any(
        os.environ.get(key) != value
        for key, value in typed_env_base.items()
        if not (is_script and key in typed_env)
    ):
        return None
    final_env = os.environ.copy()
    final_env.update(typed_env)
    final_env["PWD"] = os.getcwd()
    for raw_resolution in resolutions:
        if not isinstance(raw_resolution, dict):
            return None
        resolution = cast(dict[str, object], raw_resolution)
        name = resolution.get("name")
        abspath = resolution.get("abspath")
        selected_path = resolution.get("selected_path")
        ambient_abspath = resolution.get("ambient_abspath")
        if (
            not isinstance(name, str)
            or not isinstance(abspath, str)
            or not isinstance(selected_path, str)
            or (ambient_abspath is not None and not isinstance(ambient_abspath, str))
        ):
            return None
        if selected_path:
            selected_command = _find_executable(name, selected_path)
            if selected_command is None or os.path.realpath(
                selected_command,
            ) != os.path.realpath(abspath):
                return None
        if not (is_script and "PATH" in typed_env):
            current_ambient = _find_executable(name, os.environ.get("PATH", ""))
            resolved_ambient = (
                os.path.realpath(current_ambient)
                if current_ambient is not None
                else None
            )
            if resolved_ambient != ambient_abspath:
                return None
    return exec_abspath, final_env


def _exec_cached_plan(
    raw_plan: object,
    run_context: str,
    binary_args: list[str],
) -> int | None:
    validated = _validated_cached_plan(raw_plan, run_context)
    if validated is None:
        return None
    exec_abspath, final_env = validated
    try:
        os.execvpe(
            exec_abspath,
            [exec_abspath, *binary_args],
            final_env,
        )
    except OSError as err:
        print(f"abxpkg: failed to exec {exec_abspath}: {err}", file=sys.stderr)
        return 1
    return 1


def _format_cached_load(record: dict[str, object]) -> str | None:
    version = record.get("loaded_version")
    abspath = record.get("abspath")
    provider_name = record.get("resolved_provider_name")
    bin_name = record.get("bin_name")
    if (
        not isinstance(version, str)
        or not isinstance(abspath, str)
        or not os.path.isabs(abspath)
        or not isinstance(provider_name, str)
        or not isinstance(bin_name, str)
    ):
        return None

    rendered_abspath = f'"{abspath}"' if " " in abspath else abspath
    line = f"{version.ljust(12)} {rendered_abspath} ({provider_name}) {bin_name}"
    cwd = os.getcwd()
    if cwd != "/":
        line = line.replace(cwd + os.sep, "." + os.sep).replace(cwd, ".")
    home = os.path.expanduser("~")
    if home and home != "/":
        line = line.replace(home + os.sep, "~" + os.sep).replace(home, "~")
    return line


def _load_cached(argv: list[str]) -> int | None:
    if len(argv) != 2 or argv[0] != "load" or argv[1].startswith("-"):
        return None
    binary_name = argv[1]
    if os.path.isabs(binary_name):
        return None

    lib_dir = _resolve_lib_dir(None)
    raw_names = os.environ.get("ABXPKG_BINPROVIDERS")
    if raw_names is None:
        import abxpkg as package

        provider_names = list(package.DEFAULT_PROVIDER_NAMES)
    else:
        provider_names = [name.strip() for name in raw_names.split(",") if name.strip()]
    if not provider_names or any(
        re.fullmatch(r"[a-z][a-z0-9_-]*", name) is None for name in provider_names
    ):
        return None

    os.environ["ABXPKG_LIB_DIR"] = str(lib_dir)
    if raw_names is not None:
        os.environ["ABXPKG_BINPROVIDERS"] = ",".join(provider_names)
    else:
        os.environ.pop("ABXPKG_BINPROVIDERS", None)
    run_context = warm_run_context(
        ScriptOptions(lib_dir=lib_dir, provider_names=provider_names),
    )
    if run_context is None:
        return None

    for provider_name in provider_names:
        matches: list[str] = []
        for record in _cached_records(lib_dir, [provider_name], binary_name):
            raw_plan = record.get("exec_plan")
            if _validated_cached_plan(raw_plan, run_context) is None:
                continue
            line = _format_cached_load(record)
            if line is None:
                continue
            matches.append(line)
        if len(matches) > 1:
            return None
        if matches:
            print(matches[0])
            return 0
    return None


def _run_cached(argv: list[str]) -> int | None:
    if any(
        _env_flag_is_true(name)
        for name in ("ABXPKG_DRY_RUN", "DRY_RUN", "ABXPKG_DEBUG", "ABXPKG_NO_CACHE")
    ):
        return None

    loaded_returncode = _load_cached(argv)
    if loaded_returncode is not None:
        return loaded_returncode

    script_parsed = _parse_script_argv(argv)
    if script_parsed is not None:
        raw_options, binary_name, script_path, script_args = script_parsed
        if not os.path.isfile(script_path):
            return None
        try:
            meta = parse_script_metadata(script_path)
        except RuntimeError:
            return None
        if meta is None:
            return None
        raw_lib_dir = raw_options.get("--lib")
        lib_dir = _resolve_lib_dir(raw_lib_dir)
        raw_names = raw_options.get("--binproviders")
        if raw_names is None:
            raw_names = os.environ.get("ABXPKG_BINPROVIDERS")
        if raw_names is None:
            import abxpkg as package

            provider_names = list(package.DEFAULT_PROVIDER_NAMES)
        else:
            provider_names = [
                name.strip() for name in raw_names.split(",") if name.strip()
            ]
        if not provider_names or any(
            re.fullmatch(r"[a-z][a-z0-9_-]*", name) is None for name in provider_names
        ):
            return None
        os.environ["ABXPKG_LIB_DIR"] = str(lib_dir)
        if raw_names is not None:
            os.environ["ABXPKG_BINPROVIDERS"] = ",".join(provider_names)
        else:
            os.environ.pop("ABXPKG_BINPROVIDERS", None)

        tool_section = meta.get("tool")
        tool_config = (
            tool_section.get("abxpkg", {}) if isinstance(tool_section, dict) else {}
        )
        for key, value in tool_config.items():
            if key != "runtime_binproviders":
                os.environ.setdefault(str(key), str(value))
        cache_context = _script_cache_context(
            raw_options,
            binary_name,
            script_path,
            meta,
            ScriptOptions(lib_dir=lib_dir, provider_names=provider_names),
        )
        if cache_context is None:
            return None
        import hashlib
        import abxpkg as package

        run_context, _fingerprint_paths = cache_context
        plan_key = hashlib.sha256(run_context.encode()).hexdigest()
        for record in _cached_records(
            lib_dir,
            list(package.ALL_PROVIDER_NAMES),
            binary_name,
        ):
            raw_plans = record.get("script_exec_plans")
            if not isinstance(raw_plans, dict):
                continue
            result = _exec_cached_plan(
                raw_plans.get(plan_key),
                run_context,
                script_args,
            )
            if result is not None:
                return result
        return None

    parsed = _parse_warm_run_argv(argv)
    if parsed is None:
        return None
    raw_lib_dir, raw_provider_names, binary_name, binary_args = parsed
    lib_dir = _resolve_lib_dir(raw_lib_dir)

    raw_names = raw_provider_names
    if raw_names is None:
        raw_names = os.environ.get("ABXPKG_BINPROVIDERS")
    if raw_names is None:
        import abxpkg as package

        provider_names = list(package.DEFAULT_PROVIDER_NAMES)
    else:
        provider_names = [name.strip() for name in raw_names.split(",") if name.strip()]
    if not provider_names or any(
        re.fullmatch(r"[a-z][a-z0-9_-]*", name) is None for name in provider_names
    ):
        return None
    os.environ["ABXPKG_LIB_DIR"] = str(lib_dir)
    if raw_names is not None:
        os.environ["ABXPKG_BINPROVIDERS"] = ",".join(provider_names)
    else:
        os.environ.pop("ABXPKG_BINPROVIDERS", None)

    context = warm_run_context(
        ScriptOptions(lib_dir=lib_dir, provider_names=provider_names),
    )
    if context is None:
        return None

    for record in _cached_records(lib_dir, provider_names, binary_name):
        result = _exec_cached_plan(
            record.get("exec_plan"),
            context,
            binary_args,
        )
        if result is not None:
            return result
    return None


def main() -> None:
    cached_returncode = _run_cached(sys.argv[1:])
    if cached_returncode is not None:
        raise SystemExit(cached_returncode)

    from .click_cli import main as click_main

    click_main()


def abx_main() -> None:
    from .click_cli import abx_main as click_abx_main

    click_abx_main()


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    if name in {
        "ALL_PROVIDER_NAMES",
        "DEFAULT_PROVIDER_NAMES",
        "PROVIDER_CLASS_BY_NAME",
    }:
        import abxpkg as package

        value = getattr(package, name)
        globals()[name] = value
        return value
    from . import click_cli

    for override_name in (
        "build_binary",
        "build_providers",
        "run_binary_command",
    ):
        if override_name in globals():
            setattr(click_cli, override_name, globals()[override_name])
    value = getattr(click_cli, name)
    return value


__all__ = [
    "abx_main",
    "main",
    "parse_script_metadata",
]

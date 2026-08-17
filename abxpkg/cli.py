from __future__ import annotations

import json
import os
import re
import sys

# Keep typing-only imports off the warm CLI path.
TYPE_CHECKING = False
if TYPE_CHECKING:
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


def _expand_dependency_value(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        return re.sub(
            r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda match: values.get(match.group(1), match.group(0)),
            value,
        )
    if isinstance(value, list):
        return [_expand_dependency_value(item, values) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _expand_dependency_value(item, values)
            for key, item in value.items()
        }
    return value


def _deps_from_config_specs(
    raw_specs: list[str] | tuple[str, ...],
    *,
    base_path: str | os.PathLike[str],
    lib_dir: str | os.PathLike[str],
) -> list[Any]:
    from pathlib import Path

    deps: list[Any] = []
    values = {key: str(value) for key, value in os.environ.items()}
    values["ABXPKG_LIB_DIR"] = os.fspath(lib_dir)
    for raw_spec_group in raw_specs:
        for raw_spec in str(raw_spec_group or "").split(","):
            spec = raw_spec.strip()
            if not spec:
                continue
            raw_path, _, selector = spec.partition(":")
            deps_path = Path(raw_path)
            if not deps_path.is_absolute():
                deps_path = Path(base_path) / deps_path
            root = json.loads(deps_path.read_text())
            selected: Any = root
            for part in (selector or "dependencies").split("."):
                selected = selected[part]

            properties = root.get("properties") if isinstance(root, dict) else None
            if isinstance(properties, dict):
                for key, prop in properties.items():
                    if (
                        key not in values
                        and isinstance(prop, dict)
                        and "default" in prop
                    ):
                        values[str(key)] = str(prop["default"])

            selected_items = selected if isinstance(selected, list) else [selected]
            for selected_item in selected_items:
                expanded = _expand_dependency_value(selected_item, values)
                if isinstance(selected_item, dict) and isinstance(expanded, dict):
                    template_name = str(selected_item.get("name") or "").strip()
                    template_match = re.fullmatch(
                        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
                        template_name,
                    )
                    if template_match:
                        expanded = dict(expanded)
                        env_key = template_match.group(1)
                        expanded["_abxpkg_env_key"] = env_key
                        prop = (
                            properties.get(env_key)
                            if isinstance(properties, dict)
                            else None
                        )
                        if isinstance(prop, dict) and isinstance(
                            prop.get("default"),
                            str,
                        ):
                            expanded["_abxpkg_declared_name"] = prop["default"]
                deps.append(expanded)
    return deps


def _script_cache_context(
    raw_options: dict[str, str],
    binary_name: str,
    script_path: str | os.PathLike[str],
    meta: dict[str, Any],
    options: Any,
    explicit_provider_selection: bool,
) -> str | None:
    from pathlib import Path

    if set(raw_options) - {"--lib", "--binproviders", "--deps-from"}:
        return None
    base_context = warm_run_context(options, binary_name)
    if base_context is None:
        return None

    resolved_script = Path(script_path).expanduser().resolve(strict=False)
    try:
        dependencies = _deps_from_config_specs(
            (raw_options.get("--deps-from", ""),),
            base_path=resolved_script.parent,
            lib_dir=options.lib_dir,
        )
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    tool_section = meta.get("tool")
    tool_config = (
        tool_section.get("abxpkg", {}) if isinstance(tool_section, dict) else {}
    )
    tool_env_names = sorted(
        str(key) for key in tool_config if key != "runtime_binproviders"
    )
    projected_bin_dir = os.path.abspath(os.path.join(options.lib_dir, "env", "bin"))
    caller_path = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if not entry or os.path.abspath(entry) != projected_bin_dir
    )
    context = json.dumps(
        {
            "base": json.loads(base_context),
            "binary_name": binary_name,
            "dependencies": dependencies,
            "exec_env_inputs": {
                "PATH": caller_path,
                **{
                    name: os.environ.get(name)
                    for name in ("NODE_PATH", "PYTHONPATH", "LD_LIBRARY_PATH")
                },
            },
            "explicit_provider_selection": explicit_provider_selection,
            "metadata": meta,
            "tool_env": {name: os.environ.get(name) for name in tool_env_names},
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return context


def warm_run_context(options: Any, binary_name: str) -> str | None:
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
    from .config import abxpkg_cache_env

    context = {
        "lib_dir": str(options.lib_dir),
        "provider_names": list(options.provider_names),
        "abxpkg_env": abxpkg_cache_env(os.environ),
    }
    if binary_name in {"python", "python3"} and "env" in options.provider_names:
        context["runtime_prefix"] = os.path.abspath(sys.prefix)
    return json.dumps(
        context,
        separators=(",", ":"),
        sort_keys=True,
    )


def _parse_warm_argv(
    argv: list[str],
) -> tuple[str, str | None, str | None, str, list[str]] | None:
    lib_dir: str | None = None
    provider_names: str | None = None
    command: str | None = None
    i = 0
    while i < len(argv):
        token = argv[i]
        if command is None and token in {"load", "run", "exec"}:
            command = token
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
        if token.startswith("-") or command is None:
            return None
        return command, lib_dir, provider_names, token, argv[i + 1 :]
    return None


def _script_request(
    dependency: object,
    default_provider_names: list[str],
) -> tuple[dict[str, object], list[str], str | None, str | None] | None:
    request = {"name": dependency} if isinstance(dependency, str) else None
    if isinstance(dependency, dict):
        typed_dependency = cast(dict[str, object], dependency)
        if "name" in typed_dependency:
            request = typed_dependency
    if request is None:
        return None
    if not isinstance(request.get("name"), str) or not request["name"]:
        raise ValueError("invalid binary name")
    raw_provider_names = (
        request["binproviders"] if "binproviders" in request else default_provider_names
    )
    if isinstance(raw_provider_names, str):
        provider_names = [
            name.strip() for name in raw_provider_names.split(",") if name.strip()
        ]
    elif isinstance(raw_provider_names, list):
        provider_names = [
            str(name).strip() for name in raw_provider_names if str(name).strip()
        ]
    else:
        raise ValueError("invalid binary providers")
    if not provider_names or any(
        re.fullmatch(r"[a-z][a-z0-9_-]*", name) is None for name in provider_names
    ):
        raise ValueError("invalid binary providers")
    request = dict(request)
    request["binproviders"] = provider_names
    env_key = request.pop("_abxpkg_env_key", None)
    declared_name = request.pop("_abxpkg_declared_name", None)
    return (
        request,
        provider_names,
        str(env_key) if env_key else None,
        str(declared_name) if declared_name else None,
    )


def _cached_request_projection(
    lib_dir: str | os.PathLike[str],
    request: dict[str, object],
    provider_names: list[str],
    *,
    ignored_env_base_keys: tuple[str, ...] = (),
) -> tuple[dict[str, object], dict[str, object], str, dict[str, str]] | None:
    from .config import _load_cached_request_projection

    return _load_cached_request_projection(
        lib_dir,
        request,
        provider_names,
        ignored_env_base_keys=ignored_env_base_keys,
    )


def _cached_script_request_projection(
    lib_dir: str | os.PathLike[str],
    request: dict[str, object],
    provider_names: list[str],
    env_key: str | None,
    declared_name: str | None,
) -> tuple[dict[str, object], dict[str, object], str, dict[str, str]] | None:
    resolved = _cached_request_projection(
        lib_dir,
        request,
        provider_names,
        ignored_env_base_keys=(env_key,) if env_key else (),
    )
    requested_name = request.get("name")
    if (
        resolved is not None
        or declared_name is None
        or not isinstance(requested_name, str)
        or not os.path.isabs(requested_name)
    ):
        return resolved

    declared_request = {**request, "name": declared_name}
    resolved = _cached_request_projection(
        lib_dir,
        declared_request,
        provider_names,
        ignored_env_base_keys=(env_key,) if env_key else (),
    )
    if resolved is None:
        return None
    record = resolved[0]
    resolved_abspath = record.get("abspath")
    if not isinstance(resolved_abspath, str) or os.path.realpath(
        resolved_abspath,
    ) != os.path.realpath(requested_name):
        return None
    return resolved


def _cached_records(
    lib_dir: str | os.PathLike[str],
    provider_names: list[str],
    binary_name: str,
):
    from .config import _cached_records as records

    yield from records(lib_dir, provider_names, binary_name)


def _exec_cached_script_requests(
    *,
    lib_dir: str | os.PathLike[str],
    provider_names: list[str],
    binary_name: str,
    dependencies: list[object],
    explicit_provider_selection: bool,
    script_args: list[str],
) -> int | None:
    resolved_dependencies: list[
        tuple[dict[str, object], dict[str, object], str | None]
    ] = []
    target_request: dict[str, object] = {
        "name": binary_name,
        "binproviders": provider_names,
    }
    target_provider_names = provider_names
    target_env_key: str | None = None
    target_declared_name: str | None = None
    target_declaration_seen = False

    for dependency in dependencies:
        try:
            parsed = _script_request(dependency, provider_names)
        except ValueError:
            return None
        if parsed is None:
            continue
        request, dependency_provider_names, env_key, declared_name = parsed
        if request["name"] == binary_name or declared_name == binary_name:
            if target_declaration_seen:
                return None
            target_declaration_seen = True
            target_request = request
            target_env_key = env_key
            if explicit_provider_selection:
                target_request["binproviders"] = provider_names
            else:
                target_provider_names = dependency_provider_names
            target_declared_name = declared_name
            continue
        resolved = _cached_script_request_projection(
            lib_dir,
            request,
            dependency_provider_names,
            env_key,
            declared_name,
        )
        if resolved is None:
            return None
        record, projection, _exec_abspath, _validation_env = resolved
        resolved_dependencies.append((record, projection, env_key))

    target = _cached_script_request_projection(
        lib_dir,
        target_request,
        target_provider_names,
        target_env_key,
        target_declared_name,
    )
    if target is None:
        return None
    target_record, target_projection, exec_abspath, target_validation_env = target
    if not os.access(exec_abspath, os.X_OK):
        return None

    from .config import build_exec_env

    final_env = os.environ.copy()
    for record, _projection, env_key in resolved_dependencies:
        if env_key:
            abspath = record.get("abspath")
            if not isinstance(abspath, str):
                return None
            final_env[env_key] = abspath
    if target_env_key:
        target_abspath = target_record.get("abspath")
        if not isinstance(target_abspath, str):
            return None
        final_env[target_env_key] = target_abspath
    dependency_layers = [
        layer
        for _record, projection, _env_key in resolved_dependencies
        for layer in cast(list[dict[str, object]], projection.get("provider_layers"))
    ]
    target_provider_layers = cast(
        list[dict[str, object]],
        target_projection.get("provider_layers"),
    )
    target_provider_layer = target_provider_layers[0]
    dependency_layers = [
        layer
        for layer in dependency_layers
        if not (
            layer.get("provider_name") == target_provider_layer.get("provider_name")
            and (
                target_provider_layer.get("install_root") is None
                and target_provider_layer.get("bin_dir") is None
                or (
                    layer.get("install_root")
                    == target_provider_layer.get("install_root")
                    and layer.get("bin_dir") == target_provider_layer.get("bin_dir")
                )
            )
        )
    ]
    target_layers = cast(
        list[dict[str, object]],
        target_projection.get("target_layers"),
    )
    user_env = {
        key: target_validation_env[key]
        for key in ("HOME", "LOGNAME", "USER")
        if key in target_validation_env
    }
    final_env = build_exec_env(dependency_layers, base_env=final_env)
    final_env = build_exec_env(
        target_layers,
        base_env=final_env,
    )
    final_env.update(user_env)
    final_env["PWD"] = os.getcwd()
    try:
        os.execvpe(exec_abspath, [exec_abspath, *script_args], final_env)
    except OSError as err:
        print(f"abxpkg: failed to exec {exec_abspath}: {err}", file=sys.stderr)
        return 1
    return 1


def _validated_cached_plan(
    raw_plan: object,
    run_context: str,
) -> tuple[str, dict[str, str]] | None:
    from .config import _validated_cached_plan as validate

    return validate(raw_plan, run_context)


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
    parsed = _parse_warm_argv(argv)
    if parsed is None:
        return None
    command, raw_lib_dir, raw_names, binary_name, binary_args = parsed
    if command != "load" or binary_args or os.path.isabs(binary_name):
        return None

    lib_dir = _resolve_lib_dir(raw_lib_dir)
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
    run_context = warm_run_context(
        ScriptOptions(lib_dir=lib_dir, provider_names=provider_names),
        binary_name,
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
            explicit_provider_selection=raw_names is not None,
        )
        if cache_context is None:
            return None
        import hashlib
        import abxpkg as package

        run_context = cache_context
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
        if tool_config.get("runtime_binproviders"):
            return None
        dependencies = [
            *meta.get("dependencies", []),
            *_deps_from_config_specs(
                (raw_options.get("--deps-from", ""),),
                base_path=os.path.dirname(os.path.realpath(script_path)),
                lib_dir=lib_dir,
            ),
        ]
        return _exec_cached_script_requests(
            lib_dir=lib_dir,
            provider_names=provider_names,
            binary_name=binary_name,
            dependencies=dependencies,
            explicit_provider_selection=raw_names is not None,
            script_args=script_args,
        )

    parsed = _parse_warm_argv(argv)
    if parsed is None:
        return None
    command, raw_lib_dir, raw_provider_names, binary_name, binary_args = parsed
    if command not in {"run", "exec"}:
        return None
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
        binary_name,
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

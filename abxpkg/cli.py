from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import build_exec_env, default_abxpkg_lib_dir
from .exceptions import ABXPkgError
from .logging import format_exception_with_output


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


@dataclass(slots=True)
class ScriptOptions:
    lib_dir: Path
    provider_names: list[str]
    dry_run: bool = False
    debug: bool = False
    no_cache: bool = False
    min_version: str | None = None
    postinstall_scripts: bool | None = None
    min_release_age: float | None = None
    overrides: dict[str, Any] | None = None
    install_root: Path | None = None
    bin_dir: Path | None = None
    euid: int | None = None
    install_timeout: int | None = None
    version_timeout: int | None = None


def _none_or_stripped(raw: str | None) -> str | None:
    if raw is None:
        return None
    stripped = raw.strip()
    return None if stripped.lower() in _NONE_STRINGS else stripped


def _env_flag_is_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_bool(raw: str | None) -> bool | None:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    lowered = stripped.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected bool, got {raw!r}")


def _parse_float(raw: str | None) -> float | None:
    stripped = _none_or_stripped(raw)
    return None if stripped is None else float(stripped)


def _parse_int(raw: str | None) -> int | None:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    as_float = float(stripped)
    as_int = int(as_float)
    if as_float != as_int:
        raise ValueError(f"expected int, got {raw!r}")
    return as_int


def _parse_json_or_string(raw: str | None) -> Any:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _parse_json_object(raw: str | None) -> dict[str, Any] | None:
    parsed = _parse_json_or_string(raw)
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def _resolve_lib_dir(raw_value: str | None) -> Path:
    env_value = os.environ.get("ABXPKG_LIB_DIR")
    if _none_or_stripped(raw_value) is None and raw_value is not None:
        os.environ.pop("ABXPKG_LIB_DIR", None)
        return default_abxpkg_lib_dir().expanduser().resolve()
    if _none_or_stripped(env_value) is None and env_value is not None:
        os.environ.pop("ABXPKG_LIB_DIR", None)
        return default_abxpkg_lib_dir().expanduser().resolve()

    lib_dir = Path(
        raw_value or _none_or_stripped(env_value) or default_abxpkg_lib_dir(),
    )
    resolved = lib_dir.expanduser().resolve()
    os.environ["ABXPKG_LIB_DIR"] = str(resolved)
    return resolved


def _default_provider_names() -> list[str]:
    from . import DEFAULT_PROVIDER_NAMES

    return list(DEFAULT_PROVIDER_NAMES)


def _parse_provider_names(raw_value: str | None) -> list[str]:
    from . import ALL_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

    if raw_value is None:
        env_value = os.environ.get("ABXPKG_BINPROVIDERS")
        raw_value = (
            env_value if env_value is not None else ",".join(_default_provider_names())
        )

    provider_names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_value.split(","):
        name = raw_name.strip()
        if not name or name in seen:
            continue
        provider_names.append(name)
        seen.add(name)

    invalid = [name for name in provider_names if name not in PROVIDER_CLASS_BY_NAME]
    if invalid:
        valid = ", ".join(ALL_PROVIDER_NAMES)
        raise ValueError(
            f"unknown provider name(s): {', '.join(invalid)}. Valid providers: {valid}",
        )
    os.environ["ABXPKG_BINPROVIDERS"] = ",".join(provider_names)
    return provider_names


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
    script_path: Path,
    max_lines: int = 50,
) -> dict[str, Any] | None:
    try:
        text = script_path.read_text(encoding="utf-8", errors="replace")
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
) -> tuple[dict[str, str], str, Path, list[str]] | None:
    options: dict[str, str] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in {"run", "exec"}:
            i += 1
            break
        if token == "--install" or token == "--update":
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
    return options, binary_name, Path(script_args[0]), script_args


def _script_options(raw_options: dict[str, str]) -> ScriptOptions:
    provider_names = _parse_provider_names(raw_options.get("--binproviders"))
    handler_overrides = {
        key: value
        for key, value in (
            ("abspath", _parse_json_or_string(raw_options.get("--abspath"))),
            ("version", _parse_json_or_string(raw_options.get("--version"))),
            ("install_args", _parse_json_or_string(raw_options.get("--install-args"))),
            ("packages", _parse_json_or_string(raw_options.get("--packages"))),
        )
        if value is not None
    }
    return ScriptOptions(
        lib_dir=_resolve_lib_dir(raw_options.get("--lib")),
        provider_names=provider_names,
        dry_run=_parse_bool(raw_options.get("--dry-run"))
        or _env_flag_is_true("ABXPKG_DRY_RUN")
        or _env_flag_is_true("DRY_RUN"),
        debug=_parse_bool(raw_options.get("--debug"))
        or _env_flag_is_true("ABXPKG_DEBUG"),
        no_cache=_parse_bool(raw_options.get("--no-cache"))
        or _env_flag_is_true("ABXPKG_NO_CACHE"),
        postinstall_scripts=_parse_bool(raw_options.get("--postinstall-scripts")),
        min_release_age=_parse_float(raw_options.get("--min-release-age")),
        overrides=normalize_binary_overrides(
            provider_names,
            overrides=_parse_json_object(raw_options.get("--overrides")),
            handler_overrides=handler_overrides or None,
        ),
        install_timeout=_parse_int(raw_options.get("--install-timeout")),
        version_timeout=_parse_int(raw_options.get("--version-timeout")),
    )


def _build_providers(provider_names: list[str], options: ScriptOptions):
    from . import PROVIDER_CLASS_BY_NAME

    providers = []
    for provider_name in provider_names:
        provider_kwargs: dict[str, Any] = {"dry_run": options.dry_run}
        for key in (
            "install_root",
            "bin_dir",
            "euid",
            "install_timeout",
            "version_timeout",
        ):
            value = getattr(options, key)
            if value is not None:
                provider_kwargs[key] = value
        providers.append(PROVIDER_CLASS_BY_NAME[provider_name](**provider_kwargs))
    return providers


def _expand_script_value(
    value: Any,
    options: ScriptOptions,
    properties: dict[str, Any] | None = None,
) -> Any:
    properties = properties or {}
    if isinstance(value, str):
        return re.sub(
            r"\{([A-Z0-9_]+)\}",
            lambda match: str(
                os.environ.get(match.group(1))
                or properties.get(match.group(1), {}).get("default")
                or (
                    str(options.lib_dir)
                    if match.group(1) == "ABXPKG_LIB_DIR"
                    else match.group(0)
                ),
            ),
            value,
        )
    if isinstance(value, list):
        return [_expand_script_value(item, options, properties) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _expand_script_value(item, options, properties)
            for key, item in value.items()
        }
    return value


def _parse_runtime_provider_names(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_names = value.split(",")
    elif isinstance(value, list):
        raw_names = [str(name) for name in value]
    else:
        return []
    names: list[str] = []
    for raw_name in raw_names:
        name = str(raw_name).strip()
        if name and name not in names:
            names.append(name)
    return names


def _dependency_options(dep: dict[str, Any], options: ScriptOptions) -> ScriptOptions:
    dep = _expand_script_value(dep, options)
    provider_names = options.provider_names
    if isinstance(dep.get("binproviders"), list):
        provider_names = [str(name) for name in dep["binproviders"]]
    elif isinstance(dep.get("binproviders"), str):
        provider_names = [
            name.strip() for name in dep["binproviders"].split(",") if name.strip()
        ]

    handler_overrides: dict[str, Any] = {}
    dep_overrides = (
        dep.get("overrides") if isinstance(dep.get("overrides"), dict) else None
    )
    values: dict[str, Any] = {
        "lib_dir": options.lib_dir,
        "provider_names": provider_names,
        "dry_run": options.dry_run,
        "debug": options.debug,
        "no_cache": options.no_cache,
        "min_version": options.min_version,
        "postinstall_scripts": options.postinstall_scripts,
        "min_release_age": options.min_release_age,
        "overrides": options.overrides,
        "install_root": options.install_root,
        "bin_dir": options.bin_dir,
        "euid": options.euid,
        "install_timeout": options.install_timeout,
        "version_timeout": options.version_timeout,
    }

    for key, value in dep.items():
        if value is None or key in {"name", "binproviders"}:
            continue
        if key in _HANDLER_KEYS:
            handler_overrides[key] = value
        elif key == "overrides":
            continue
        elif key in values and values[key] is None:
            values[key] = (
                Path(value).expanduser().resolve()
                if key in {"install_root", "bin_dir"}
                else value
            )
    dep_binary_overrides = normalize_binary_overrides(
        provider_names,
        overrides=dep_overrides,
        handler_overrides=handler_overrides or None,
    )
    # Dependency metadata is the closest statement of intent for that binary.
    # Merge caller-wide overrides first so per-dependency install/version/path
    # values always win and both CLI aliases and JSON overrides hit one cache key.
    values["overrides"] = merge_binary_overrides(
        options.overrides,
        dep_binary_overrides,
    )
    return ScriptOptions(**values)


def _script_deps_from(
    raw_value: str | None,
    script_path: Path,
    options: ScriptOptions,
) -> list[Any]:
    deps: list[Any] = []
    properties: dict[str, Any] = {}
    for raw_spec in (raw_value or "").split(","):
        spec = raw_spec.strip()
        if not spec:
            continue
        raw_path, _, selector = spec.partition(":")
        deps_path = Path(raw_path)
        if not deps_path.is_absolute():
            deps_path = script_path.parent / deps_path
        root = json.loads(deps_path.read_text())
        selected: Any = root
        for part in (selector or "dependencies").split("."):
            selected = selected[part]
        if isinstance(root, dict):
            properties.update(root.get("properties", {}))
        deps.extend(_expand_script_value(selected, options, properties))
    return deps


def _build_binary(binary_name: str, options: ScriptOptions):
    from . import DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME, Binary

    provider_names = options.provider_names
    if provider_names == list(DEFAULT_PROVIDER_NAMES):
        for provider_class in PROVIDER_CLASS_BY_NAME.values():
            if provider_class.model_fields["INSTALLER_BIN"].default != binary_name:
                continue
            preferred = getattr(provider_class, "INSTALLER_BINPROVIDERS", None)
            if preferred:
                provider_names = [
                    name for name in preferred if name in options.provider_names
                ]
            break

    kwargs: dict[str, Any] = {
        "name": binary_name,
        "binproviders": _build_providers(provider_names, options),
    }
    for key in ("min_version", "postinstall_scripts", "min_release_age"):
        value = getattr(options, key)
        if value is not None:
            kwargs[key] = value
    if options.overrides is not None:
        kwargs["overrides"] = options.overrides
    return Binary(**kwargs)


def _format_error(err: Exception) -> str:
    return format_exception_with_output(err)


def _runtime_exec_providers(binary, runtime_providers):
    if not binary.loaded_binprovider:
        return []
    return [
        provider
        for provider in runtime_providers
        if not _same_runtime_provider(provider, binary.loaded_binprovider)
    ]


def _same_runtime_provider(provider, loaded_provider) -> bool:
    # Runtime env merging only needs to dedupe providers by the fields that
    # change their filesystem/env surface. Some caller-provided provider-like
    # objects only implement the execution protocol, so missing optional layout
    # fields must compare as absent instead of failing before the command runs.
    if provider.name != loaded_provider.name:
        return False
    loaded_install_root = getattr(loaded_provider, "install_root", None)
    loaded_bin_dir = getattr(loaded_provider, "bin_dir", None)
    if loaded_install_root is None and loaded_bin_dir is None:
        return True
    return (
        getattr(provider, "install_root", None) == loaded_install_root
        and getattr(provider, "bin_dir", None) == loaded_bin_dir
    )


def _run_script(argv: list[str]) -> int | None:
    parsed = _parse_script_argv(argv)
    if parsed is None:
        return None
    raw_options, binary_name, script_path, script_args = parsed
    if not script_path.is_file():
        print(f"abxpkg: script not found: {script_path}", file=sys.stderr)
        return 1
    meta = parse_script_metadata(script_path)
    if meta is None:
        print(f"abxpkg: no /// script metadata found in {script_path}", file=sys.stderr)
        return 1

    tool_section = meta.get("tool")
    tool_config = (
        tool_section.get("abxpkg", {}) if isinstance(tool_section, dict) else {}
    )
    for key, value in tool_config.items():
        if key == "runtime_binproviders":
            continue
        os.environ.setdefault(str(key), str(value))

    options = _script_options(raw_options)
    runtime_provider_names = (
        _parse_runtime_provider_names(
            tool_config.get("runtime_binproviders"),
        )
        or options.provider_names
    )
    runtime_providers = _build_providers(runtime_provider_names, options)
    binary_options = options

    try:
        dependencies = [
            *meta.get("dependencies", []),
            *_script_deps_from(raw_options.get("--deps-from"), script_path, options),
        ]
        for dep in dependencies:
            if isinstance(dep, str):
                dep_name = dep
                dep_options = options
            elif isinstance(dep, dict) and dep.get("name"):
                dep_name = str(dep["name"])
                dep_options = _dependency_options(dep, options)
            else:
                continue

            if dep_name == binary_name:
                binary_options = dep_options
                continue

            dep_binary = _build_binary(dep_name, dep_options).install(
                dry_run=options.dry_run,
                no_cache=options.no_cache,
            )
            if dep_binary.loaded_binprovider:
                runtime_providers.append(dep_binary.loaded_binprovider)

        target_binary = _build_binary(binary_name, binary_options)
        binary = target_binary.install(
            dry_run=options.dry_run,
            no_cache=options.no_cache,
        )
    except ABXPkgError as err:
        print(
            f"abxpkg: failed to resolve dependency: {_format_error(err)}",
            file=sys.stderr,
        )
        return 1

    if options.dry_run:
        return 0
    if (
        not binary.is_valid
        or not binary.loaded_binprovider
        or not binary.loaded_abspath
    ):
        print(f"abxpkg: {binary_name}: binary could not be loaded", file=sys.stderr)
        return 1

    exec_providers = _runtime_exec_providers(binary, runtime_providers)
    final_env = build_exec_env(
        # The loaded provider owns the target executable. Merge it before
        # sibling dependency providers so single-value runtime aliases like
        # NODE_MODULES_DIR describe the target provider, while NODE_PATH/PATH
        # still include the full dependency chain.
        providers=[
            binary.loaded_binprovider,
            *binary.loaded_binprovider.exec_env_providers(),
            *exec_providers,
        ],
        base_env=os.environ.copy(),
    )
    exec_abspath = binary.loaded_binprovider._exec_bin_abspath(
        Path(binary.loaded_abspath),
    )
    argv = [str(exec_abspath), *script_args]
    try:
        os.execvpe(str(exec_abspath), argv, final_env)
    except OSError as err:
        print(f"abxpkg: failed to exec {exec_abspath}: {err}", file=sys.stderr)
        return 1
    return 1


def main() -> None:
    script_returncode = _run_script(sys.argv[1:])
    if script_returncode is not None:
        raise SystemExit(script_returncode)

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

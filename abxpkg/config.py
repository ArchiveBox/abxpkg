from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from functools import lru_cache

# Keep typing-only imports off cache reads.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import ClassVar, Protocol, cast

    class SupportsExecEnv(Protocol):
        name: str
        PATH: str
        install_root: Path | None
        bin_dir: Path | None
        EXEC_ONLY_ENV_KEYS: ClassVar[frozenset[str]]
        FIRST_WRITER_ENV_KEYS: ClassVar[frozenset[str]]

        def setup_PATH(self) -> None: ...

        def execution_PATH(self) -> str: ...

        @property
        def ENV(self) -> dict[str, str]: ...
else:

    def cast(_type, value):
        return value


DERIVED_CACHE_KEY = "ABXPKG_DERIVED_CACHE"
_SHELL_SINGLE_QUOTE_ESCAPE = "'\"'\"'"
_BINARY_REQUEST_CACHE_FIELDS = (
    "name",
    "min_version",
    "postinstall_scripts",
    "min_release_age",
    "binproviders",
    "overrides",
    "install_root",
    "bin_dir",
    "euid",
    "dry_run",
    "no_cache",
    "install_timeout",
    "version_timeout",
    "abspath",
    "version",
    "install_args",
    "packages",
)
_BINARY_REQUEST_PATH_FIELDS = frozenset({"abspath", "bin_dir", "install_root"})
_OPERATIONAL_ABXPKG_ENV_KEYS = frozenset(
    {
        "ABXPKG_BINPROVIDERS",
        "ABXPKG_DEBUG",
        "ABXPKG_DRY_RUN",
        "ABXPKG_LIB_DIR",
        "ABXPKG_NO_CACHE",
        "ABXPKG_TMP_CACHE_DIR",
    },
)


def abxpkg_cache_env(env: Mapping[str, str]) -> dict[str, str]:
    """Project environment variables that can change binary resolution."""
    return {
        key: value
        for key, value in sorted(env.items())
        if key == "VIRTUAL_ENV"
        or key.startswith("ABXPKG_")
        and key not in _OPERATIONAL_ABXPKG_ENV_KEYS
    }


def _canonical_request_path(value: object) -> object:
    if not isinstance(value, (str, os.PathLike)):
        return value
    path = os.path.expanduser(os.fspath(value))
    return os.path.realpath(path) if os.path.isabs(path) else path


def binary_request_cache_key(
    request: Mapping[str, object],
    *,
    default_provider_names: Iterable[str],
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the exact cache identity shared by event and script requests."""

    provider_names = request.get("binproviders") or default_provider_names
    if isinstance(provider_names, str):
        provider_names = [
            name.strip() for name in provider_names.split(",") if name.strip()
        ]
    elif isinstance(provider_names, Iterable):
        provider_names = [
            str(name).strip() for name in provider_names if str(name).strip()
        ]
    else:
        provider_names = [str(name).strip() for name in default_provider_names]
    payload = {
        field: request.get(field)
        for field in _BINARY_REQUEST_CACHE_FIELDS
        if field != "binproviders"
    }
    raw_name = payload["name"]
    if isinstance(raw_name, (str, os.PathLike)) and os.path.isabs(
        os.path.expanduser(os.fspath(raw_name)),
    ):
        payload["name"] = _canonical_request_path(raw_name)
    for field in _BINARY_REQUEST_PATH_FIELDS:
        payload[field] = _canonical_request_path(payload[field])
    min_release_age = payload["min_release_age"]
    if isinstance(min_release_age, (int, float)):
        payload["min_release_age"] = float(min_release_age)
    raw_overrides = payload["overrides"]
    if isinstance(raw_overrides, Mapping):
        payload["overrides"] = {
            provider_name: {
                key: _canonical_request_path(value)
                if key in _BINARY_REQUEST_PATH_FIELDS
                else float(value)
                if key == "min_release_age" and isinstance(value, (int, float))
                else value
                for key, value in provider_overrides.items()
            }
            if isinstance(provider_overrides, Mapping)
            else provider_overrides
            for provider_name, provider_overrides in raw_overrides.items()
        }
    if payload["overrides"] == {}:
        payload["overrides"] = None
    payload["dry_run"] = bool(payload["dry_run"])
    payload["no_cache"] = bool(payload["no_cache"])
    payload["binproviders"] = provider_names
    if payload["name"] in {"python", "python3"} and "env" in provider_names:
        payload["runtime_prefix"] = os.path.abspath(sys.prefix)
    payload["abxpkg_env"] = abxpkg_cache_env(
        env if env is not None else os.environ,
    )
    canonical = json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def default_abxpkg_lib_dir() -> Path:
    from platformdirs import user_config_path

    return user_config_path("abx") / "lib"


@lru_cache(maxsize=32)
def _forbidden_convenience_lib_bins(abxpkg_lib_dir: str | None) -> frozenset[Path]:
    from pathlib import Path

    lib_dirs = [Path(abxpkg_lib_dir)] if abxpkg_lib_dir else []
    lib_dirs.append(default_abxpkg_lib_dir())
    return frozenset((lib_dir.expanduser().absolute() / "bin") for lib_dir in lib_dirs)


def is_forbidden_convenience_lib_bin(path: str | Path | None) -> bool:
    """True only for flat abxpkg lib ``bin`` convenience directories.

    Install flows can create that directory for humans, but abxpkg must not
    use it for PATH-based discovery or runtime execution. Provider-owned dirs
    like ``ABXPKG_LIB_DIR/env/bin`` and ``ABXPKG_LIB_DIR/playwright/bin``
    remain valid runtime paths.
    """
    if path is None:
        return False
    try:
        from pathlib import Path

        candidate = Path(path).expanduser().absolute()
        forbidden_dirs = _forbidden_convenience_lib_bins(
            os.environ.get("ABXPKG_LIB_DIR"),
        )
    except Exception:
        return False
    return candidate in forbidden_dirs


def _split_path(path_value: str | None) -> list[str]:
    return [
        entry
        for entry in str(path_value or "").split(os.pathsep)
        if entry and not is_forbidden_convenience_lib_bin(entry)
    ]


def apply_exec_env(
    exec_env: Mapping[str, str],
    env: MutableMapping[str, str],
) -> None:
    """Apply one execution-time env layer to ``env`` in place.

    Value semantics:
    - ``"value"`` overwrites the existing value
    - ``":value"`` appends to the existing value
    - ``"value:"`` prepends to the existing value
    """

    for key, value in exec_env.items():
        if value.startswith(":"):
            existing = env.get(key, "")
            env[key] = f"{existing}{value}" if existing else value[1:]
        elif value.endswith(":"):
            existing = env.get(key, "")
            env[key] = f"{value}{existing}" if existing else value[:-1]
        else:
            env[key] = value


def merge_exec_path(
    *path_layers: str | None,
    base_path: str | None = None,
) -> str:
    """Merge PATH prefixes in precedence order, then append ``base_path``.

    Earlier ``path_layers`` have higher precedence than later ones.
    Duplicate entries are removed while preserving first occurrence.
    """

    merged: list[str] = []
    seen: set[str] = set()

    for layer in (*path_layers, base_path):
        for entry in _split_path(layer):
            if entry in seen:
                continue
            seen.add(entry)
            merged.append(entry)

    return os.pathsep.join(merged)


def build_exec_env(
    providers: Iterable[SupportsExecEnv | Mapping[str, object]] = (),
    *,
    base_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    include_exec_only_env: bool = True,
) -> dict[str, str]:
    """Build the final env used for runtime execution.

    This is intentionally execution-only. Provider resolution continues to use
    each provider's own ``PATH`` and lookup logic independently.
    """

    env = dict(os.environ if base_env is None else base_env)
    provider_path_prepend_layers: list[str] = []
    provider_path_append_layers: list[str] = []
    extra_path_prepend_layers: list[str] = []
    extra_path_append_layers: list[str] = []
    pathlike_prepend_layers: dict[str, list[str]] = {
        "NODE_PATH": [],
        "PYTHONPATH": [],
    }
    pathlike_append_layers: dict[str, list[str]] = {
        "NODE_PATH": [],
        "PYTHONPATH": [],
    }

    def consume_PATH_env(
        layer: MutableMapping[str, str],
        *,
        prepend_layers: list[str],
        append_layers: list[str],
    ) -> None:
        value = layer.pop("PATH", None)
        if not value:
            return
        if value.startswith(":"):
            append_layers.append(value[1:])
        elif value.endswith(":"):
            prepend_layers.append(value[:-1])
        else:
            prepend_layers.append(value)

    def consume_pathlike_env(layer: MutableMapping[str, str]) -> None:
        for key in pathlike_append_layers:
            value = layer.pop(key, None)
            if not value:
                continue
            if value.startswith(":"):
                pathlike_append_layers[key].append(value[1:])
            elif value.endswith(":"):
                pathlike_prepend_layers[key].append(value[:-1])
            else:
                pathlike_append_layers[key].append(value)

    if extra_env:
        extra_layer = dict(extra_env)
        consume_PATH_env(
            extra_layer,
            prepend_layers=extra_path_prepend_layers,
            append_layers=extra_path_append_layers,
        )
        consume_pathlike_env(extra_layer)
        apply_exec_env(extra_layer, env)

    seen_providers: set[int] = set()
    first_writer_provider_keys: set[str] = set()
    for provider in providers:
        provider_id = id(provider)
        if provider_id in seen_providers:
            continue
        seen_providers.add(provider_id)
        if isinstance(provider, Mapping):
            layer: dict[str, object] = {
                str(key): value for key, value in provider.items()
            }
            raw_env = layer.get("env")
            if not isinstance(raw_env, Mapping):
                continue
            provider_env = {
                str(key): value
                for key, value in raw_env.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            raw_exec_only_keys = layer.get("exec_only_env_keys")
            exec_only_keys = (
                raw_exec_only_keys if isinstance(raw_exec_only_keys, list) else []
            )
            raw_first_writer_keys = layer.get("first_writer_env_keys")
            first_writer_keys = (
                raw_first_writer_keys if isinstance(raw_first_writer_keys, list) else []
            )
            provider_path = layer.get("execution_path")
        else:
            provider.setup_PATH()
            provider_env = dict(provider.ENV)
            exec_only_keys = provider.EXEC_ONLY_ENV_KEYS
            first_writer_keys = provider.FIRST_WRITER_ENV_KEYS
            provider_path = provider.execution_PATH()
        if not include_exec_only_env:
            for key in exec_only_keys:
                if isinstance(key, str):
                    provider_env.pop(key, None)
        for key in first_writer_keys:
            if not isinstance(key, str):
                continue
            if key in first_writer_provider_keys:
                provider_env.pop(key, None)
            elif provider_env.get(key):
                first_writer_provider_keys.add(key)
        consume_PATH_env(
            provider_env,
            prepend_layers=provider_path_prepend_layers,
            append_layers=provider_path_append_layers,
        )
        if provider_path:
            provider_path_prepend_layers.append(str(provider_path))
        consume_pathlike_env(provider_env)
        apply_exec_env(provider_env, env)

    merged_path = merge_exec_path(
        *provider_path_prepend_layers,
        *extra_path_prepend_layers,
        env.get("PATH", ""),
        *provider_path_append_layers,
        *extra_path_append_layers,
    )
    if merged_path:
        env["PATH"] = merged_path
    for key, append_layers in pathlike_append_layers.items():
        merged_pathlike = merge_exec_path(
            *pathlike_prepend_layers[key],
            env.get(key, ""),
            *append_layers,
        )
        if merged_pathlike:
            env[key] = merged_pathlike

    return env


def provider_exec_env_layers(
    providers: Iterable[SupportsExecEnv],
) -> list[dict[str, object]]:
    """Serialize provider-owned runtime environment behavior for cache reuse."""

    layers: list[dict[str, object]] = []
    seen_providers: set[int] = set()
    for provider in providers:
        provider_id = id(provider)
        if provider_id in seen_providers:
            continue
        seen_providers.add(provider_id)
        provider.setup_PATH()
        layers.append(
            {
                "provider_name": provider.name,
                "install_root": (
                    str(provider.install_root)
                    if provider.install_root is not None
                    else None
                ),
                "bin_dir": str(provider.bin_dir)
                if provider.bin_dir is not None
                else None,
                "env": dict(provider.ENV),
                "execution_path": str(provider.execution_PATH()),
                "exec_only_env_keys": sorted(provider.EXEC_ONLY_ENV_KEYS),
                "first_writer_env_keys": sorted(provider.FIRST_WRITER_ENV_KEYS),
            },
        )
    return layers


def parse_dotenv_values(contents: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if not key:
            continue
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            if value.startswith("'"):
                # write_dotenv_values uses shell quoting for arbitrary strings.
                # Single-quoted JSON must be unwrapped as shell text first:
                # ast.literal_eval would consume JSON backslashes and corrupt
                # nested cache keys such as ["provider","bin",...,"{\"...\"}"].
                values[key] = value[1:-1].replace(_SHELL_SINGLE_QUOTE_ESCAPE, "'")
                continue
            try:
                import ast

                values[key] = str(ast.literal_eval(value))
                continue
            except Exception:
                pass
            try:
                import shlex

                values[key] = shlex.split(value)[0]
                continue
            except Exception:
                pass
        values[key] = value

    return values


def _read_regular_file(path: Path) -> str | None:
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, encoding="utf-8") as file:
            fd = -1
            return file.read()
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def load_dotenv_values(dotenv_path: Path) -> dict[str, str]:
    contents = _read_regular_file(dotenv_path)
    return parse_dotenv_values(contents) if contents is not None else {}


def write_dotenv_values(
    dotenv_path: Path,
    values: Mapping[str, str],
) -> None:
    import shlex
    import tempfile
    from pathlib import Path

    if not values:
        dotenv_path.unlink(missing_ok=True)
        return

    dotenv_path.parent.mkdir(parents=True, exist_ok=True)
    contents = "".join(
        f"{key}={shlex.quote(str(value))}\n" for key, value in sorted(values.items())
    )
    file_mode = dotenv_path.stat().st_mode & 0o755 if dotenv_path.exists() else 0o600
    temp_fd, temp_name = tempfile.mkstemp(
        dir=dotenv_path.parent,
        prefix=f".{dotenv_path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(temp_fd, file_mode)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(contents)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, dotenv_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def load_derived_cache_text(contents: str) -> dict[str, dict[str, object]]:
    raw_value = parse_dotenv_values(contents).get(DERIVED_CACHE_KEY, "").strip()
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def load_derived_cache(dotenv_path: Path) -> dict[str, dict[str, object]]:
    contents = _read_regular_file(dotenv_path)
    return load_derived_cache_text(contents) if contents is not None else {}


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
    install_roots: dict[str, str] | None = None,
    *,
    require_executable: bool = True,
):
    for provider_name in provider_names:
        default_root = os.path.join(lib_dir, provider_name)
        provider_roots = dict.fromkeys(
            filter(None, ((install_roots or {}).get(provider_name), default_root)),
        )
        for provider_root in provider_roots:
            derived_env_path = os.path.join(provider_root, "derived.env")
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
                cache = load_derived_cache_text(contents)
            except OSError:
                continue
            finally:
                if fd >= 0:
                    os.close(fd)
            for record in cache.values():
                raw_fingerprints = (
                    record.get("fingerprint") if isinstance(record, dict) else None
                )
                record_abspath = (
                    record.get("abspath") if isinstance(record, dict) else None
                )
                primary_fingerprint = (
                    raw_fingerprints[0]
                    if isinstance(raw_fingerprints, list) and raw_fingerprints
                    else None
                )
                if (
                    isinstance(record, dict)
                    and record.get("provider_name") == provider_name
                    and record.get("bin_name") == binary_name
                    and isinstance(record_abspath, str)
                    and os.path.isabs(record_abspath)
                    and (not require_executable or os.access(record_abspath, os.X_OK))
                    and isinstance(primary_fingerprint, dict)
                    and os.path.realpath(record_abspath)
                    == cast(dict[str, object], primary_fingerprint).get("path")
                    and _fingerprints_match(raw_fingerprints)
                ):
                    yield record


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
    *,
    base_env: Mapping[str, str] | None = None,
    ignored_env_base_keys: Iterable[str] = (),
    require_executable: bool = True,
) -> tuple[str, dict[str, str]] | None:
    if os.getuid() != os.geteuid() or not isinstance(raw_plan, dict):
        return None
    exec_plan = cast(dict[str, object], raw_plan)
    if (
        exec_plan.get("version") != 6
        or exec_plan.get("run_context") != run_context
        or exec_plan.get("euid") != os.geteuid()
        or not _fingerprints_match(exec_plan.get("fingerprint"))
    ):
        return None
    exec_abspath = exec_plan.get("abspath")
    is_script = exec_plan.get("script")
    env = exec_plan.get("env")
    env_base = exec_plan.get("env_base")
    cache_context_env = exec_plan.get("cache_context_env")
    resolutions = exec_plan.get("resolutions")
    if (
        not isinstance(exec_abspath, str)
        or not os.path.isabs(exec_abspath)
        or (require_executable and not os.access(exec_abspath, os.X_OK))
        or not isinstance(is_script, bool)
        or not isinstance(env, dict)
        or not isinstance(env_base, dict)
        or not isinstance(cache_context_env, dict)
        or not isinstance(resolutions, list)
        or not resolutions
        or any(not isinstance(key, str) for key in env)
        or any(not isinstance(value, str) for value in env.values())
        or any(not isinstance(key, str) for key in env_base)
        or any(
            value is not None and not isinstance(value, str)
            for value in env_base.values()
        )
        or any(not isinstance(key, str) for key in cache_context_env)
        or any(
            value is not None and not isinstance(value, str)
            for value in cache_context_env.values()
        )
    ):
        return None
    typed_env = cast(dict[str, str], env)
    typed_env_base = cast(dict[str, str | None], env_base)
    typed_cache_context_env = cast(dict[str, str | None], cache_context_env)
    ignored_env_keys = frozenset(ignored_env_base_keys)
    current_env = os.environ if base_env is None else base_env
    if any(
        current_env.get(key) != value
        for key, value in typed_cache_context_env.items()
        if not (is_script and key in typed_env)
    ):
        return None
    if any(
        current_env.get(key) != value
        for key, value in typed_env_base.items()
        if key not in ignored_env_keys and not (is_script and key in typed_env)
    ):
        return None
    final_env = dict(current_env)
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
        current_ambient = _find_executable(name, current_env.get("PATH", ""))
        resolved_ambient = (
            os.path.realpath(current_ambient) if current_ambient is not None else None
        )
        projected_ambient = is_script and resolved_ambient == os.path.realpath(abspath)
        if resolved_ambient != ambient_abspath and not projected_ambient:
            return None
    return exec_abspath, final_env


def _load_cached_request_projection(
    lib_dir: str | os.PathLike[str],
    request: dict[str, object],
    provider_names: list[str],
    *,
    base_env: Mapping[str, str] | None = None,
    ignored_env_base_keys: Iterable[str] = (),
) -> tuple[dict[str, object], dict[str, object], str, dict[str, str]] | None:
    current_env = os.environ if base_env is None else base_env
    request_key = binary_request_cache_key(
        request,
        default_provider_names=provider_names,
        env=current_env,
    )
    install_roots: dict[str, str] = {}
    top_level_install_root = request.get("install_root")
    if isinstance(top_level_install_root, (str, os.PathLike)):
        install_roots.update(
            dict.fromkeys(provider_names, os.fspath(top_level_install_root)),
        )
    raw_overrides = request.get("overrides")
    if isinstance(raw_overrides, dict):
        typed_overrides = cast(dict[str, object], raw_overrides)
        for provider_name in provider_names:
            provider_overrides = typed_overrides.get(provider_name)
            if not isinstance(provider_overrides, dict):
                continue
            install_root = cast(dict[str, object], provider_overrides).get(
                "install_root",
            )
            if isinstance(install_root, (str, os.PathLike)):
                install_roots[provider_name] = os.fspath(install_root)

    for provider_name in provider_names:
        matches = []
        # Dependency requests may resolve non-executable artifacts such as
        # browser extension metadata or importable module entrypoints.
        for record in _cached_records(
            lib_dir,
            [provider_name],
            str(request["name"]),
            install_roots,
            require_executable=False,
        ):
            raw_projections = record.get("request_exec_projections")
            projection = (
                raw_projections.get(request_key)
                if isinstance(raw_projections, dict)
                else None
            )
            if not isinstance(projection, dict):
                continue
            typed_projection = cast(dict[str, object], projection)
            if typed_projection.get("version") != 1 or any(
                not isinstance(typed_projection.get(key), list)
                or any(
                    not isinstance(layer, dict)
                    for layer in cast(list[object], typed_projection.get(key))
                )
                for key in ("provider_layers", "target_layers")
            ):
                continue
            if not typed_projection.get("provider_layers"):
                continue
            validated = _validated_cached_plan(
                typed_projection.get("validation"),
                request_key,
                base_env=current_env,
                ignored_env_base_keys=ignored_env_base_keys,
                require_executable=False,
            )
            if validated is not None:
                matches.append((record, typed_projection, *validated))
        if len(matches) > 1:
            return None
        if not matches:
            continue
        return matches[0]
    return None


def save_derived_cache(
    dotenv_path: Path,
    cache: Mapping[str, object],
) -> None:
    values = load_dotenv_values(dotenv_path)
    if cache:
        values[DERIVED_CACHE_KEY] = json.dumps(
            cache,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        values.pop(DERIVED_CACHE_KEY, None)
    write_dotenv_values(dotenv_path, values)

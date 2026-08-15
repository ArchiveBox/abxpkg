from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Mapping, MutableMapping
from functools import lru_cache
from pathlib import Path

# Keep typing-only imports off cache reads.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import ClassVar, Protocol

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
    min_release_age = payload["min_release_age"]
    if isinstance(min_release_age, (int, float)):
        payload["min_release_age"] = float(min_release_age)
    payload["dry_run"] = bool(payload["dry_run"])
    payload["no_cache"] = bool(payload["no_cache"])
    payload["binproviders"] = provider_names
    payload["abxpkg_env"] = {
        key: value
        for key, value in sorted(
            (env if env is not None else os.environ).items(),
        )
        if key.startswith("ABXPKG_")
        and key
        not in {
            "ABXPKG_BINPROVIDERS",
            "ABXPKG_LIB_DIR",
            "ABXPKG_TMP_CACHE_DIR",
        }
    }
    canonical = json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def default_abxpkg_lib_dir() -> Path:
    from platformdirs import user_config_path

    return user_config_path("abx") / "lib"


@lru_cache(maxsize=32)
def _forbidden_convenience_lib_bins(abxpkg_lib_dir: str | None) -> frozenset[Path]:
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

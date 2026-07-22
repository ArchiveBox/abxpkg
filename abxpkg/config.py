from __future__ import annotations

import ast
import json
import os
import shlex
import tempfile
from collections.abc import Iterable, Mapping, MutableMapping
from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable


DERIVED_CACHE_KEY = "ABXPKG_DERIVED_CACHE"
_SHELL_SINGLE_QUOTE_ESCAPE = "'\"'\"'"
_FIRST_WRITER_ENV_KEYS = frozenset(
    {
        # These are convenience aliases for JS tooling with a single value,
        # while NODE_PATH is the complete ordered search path. Provider envs are
        # merged in precedence order, so keep the first alias value and let
        # later providers contribute only through NODE_PATH. Otherwise a lower
        # priority provider can overwrite the alias with an unused workspace.
        "NODE_MODULES_DIR",
        "NODE_MODULE_DIR",
    },
)


@runtime_checkable
class SupportsExecEnv(Protocol):
    PATH: str
    EXEC_ONLY_ENV_KEYS: ClassVar[frozenset[str]]

    def setup_PATH(self) -> None: ...

    @property
    def ENV(self) -> dict[str, str]: ...


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
    providers: Iterable[SupportsExecEnv] = (),
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

        provider.setup_PATH()
        provider_env = dict(provider.ENV)
        if not include_exec_only_env:
            for key in getattr(provider, "EXEC_ONLY_ENV_KEYS", ()):
                provider_env.pop(key, None)
        for key in _FIRST_WRITER_ENV_KEYS:
            if key in first_writer_provider_keys:
                provider_env.pop(key, None)
            elif provider_env.get(key):
                first_writer_provider_keys.add(key)
        consume_PATH_env(
            provider_env,
            prepend_layers=provider_path_prepend_layers,
            append_layers=provider_path_append_layers,
        )
        provider_path = provider.PATH
        provider_bin_dir = getattr(provider, "bin_dir", None)
        # EnvProvider uses its full ambient PATH for host discovery, but only
        # binaries that it has accepted and projected into env/bin may outrank
        # managed provider fallbacks at execution time. Keep the untouched
        # ambient PATH as the final base layer so undeclared host tools remain
        # available without allowing a rejected host candidate to shadow a
        # managed binary.
        if getattr(provider, "name", None) == "env" and provider_bin_dir is not None:
            provider_path = str(provider_bin_dir)
        if provider_path:
            provider_path_prepend_layers.append(provider_path)
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


def load_dotenv_values(dotenv_path: Path) -> dict[str, str]:
    if not dotenv_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
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
                values[key] = str(ast.literal_eval(value))
                continue
            except Exception:
                pass
            try:
                values[key] = shlex.split(value)[0]
                continue
            except Exception:
                pass
        values[key] = value

    return values


def write_dotenv_values(
    dotenv_path: Path,
    values: Mapping[str, str],
) -> None:
    if not values:
        dotenv_path.unlink(missing_ok=True)
        return

    dotenv_path.parent.mkdir(parents=True, exist_ok=True)
    contents = "".join(
        f"{key}={shlex.quote(str(value))}\n" for key, value in sorted(values.items())
    )
    file_mode = dotenv_path.stat().st_mode & 0o777 if dotenv_path.exists() else 0o600
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


def load_derived_cache(dotenv_path: Path) -> dict[str, dict[str, object]]:
    raw_value = load_dotenv_values(dotenv_path).get(DERIVED_CACHE_KEY, "").strip()
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

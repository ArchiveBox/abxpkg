from __future__ import annotations

import ast
import json
import os
import shlex
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Protocol, runtime_checkable


DERIVED_CACHE_KEY = "ABXPKG_DERIVED_CACHE"


@runtime_checkable
class SupportsExecEnv(Protocol):
    PATH: str

    def setup_PATH(self) -> None: ...

    @property
    def ENV(self) -> dict[str, str]: ...


def _split_path(path_value: str | None) -> list[str]:
    return [entry for entry in str(path_value or "").split(os.pathsep) if entry]


def apply_exec_env(
    exec_env: Mapping[str, str],
    env: MutableMapping[str, str],
) -> None:
    """Apply one execution-time env layer to ``env`` in place.

    Value semantics (``SEP`` is :data:`os.pathsep` — ``:`` on Unix, ``;``
    on Windows — used as both the sentinel and the separator, so on each
    host the resulting path-list is natively well-formed):
    - ``"value"`` overwrites the existing value
    - ``"<SEP>value"`` appends to the existing value
    - ``"value<SEP>"`` prepends to the existing value
    """

    sep = os.pathsep
    for key, value in exec_env.items():
        if value.startswith(sep):
            existing = env.get(key, "")
            env[key] = f"{existing}{value}" if existing else value[1:]
        elif value.endswith(sep):
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
) -> dict[str, str]:
    """Build the final env used for runtime execution.

    This is intentionally execution-only. Provider resolution continues to use
    each provider's own ``PATH`` and lookup logic independently.
    """

    env = dict(os.environ if base_env is None else base_env)
    path_layers: list[str] = []

    if extra_env:
        extra_layer = dict(extra_env)
        extra_path = extra_layer.pop("PATH", None)
        if extra_path:
            path_layers.append(extra_path)
        apply_exec_env(extra_layer, env)

    seen_providers: set[int] = set()
    for provider in providers:
        provider_id = id(provider)
        if provider_id in seen_providers:
            continue
        seen_providers.add(provider_id)

        provider.setup_PATH()
        provider_env = dict(provider.ENV)
        provider_path = provider_env.pop("PATH", None)
        if provider_path:
            path_layers.append(provider_path)
        if provider.PATH:
            path_layers.append(provider.PATH)
        apply_exec_env(provider_env, env)

    merged_path = merge_exec_path(*path_layers, base_path=env.get("PATH", ""))
    if merged_path:
        env["PATH"] = merged_path

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
            try:
                values[key] = shlex.split(value)[0]
                continue
            except Exception:
                try:
                    parsed = ast.literal_eval(value)
                    values[key] = str(parsed)
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
    dotenv_path.write_text(
        "".join(
            f"{key}={shlex.quote(str(value))}\n"
            for key, value in sorted(values.items())
        ),
        encoding="utf-8",
    )


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

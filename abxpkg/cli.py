from __future__ import annotations

import json
import logging as py_logging
import os
import platform
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, replace
from importlib import metadata
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

import rich_click as click
from rich.highlighter import ReprHighlighter
from rich.logging import RichHandler
from rich.text import Text
from rich.theme import Theme

from . import ALL_PROVIDER_NAMES, DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME, Binary
from .base_types import DEFAULT_LIB_DIR
from .binprovider import DEFAULT_ENV_PATH, BinProvider, HandlerDict, env_flag_is_true
from .config import load_derived_cache
from .exceptions import ABXPkgError
from .logging import (
    RICH_INSTALLED,
    configure_logging,
    format_command,
    format_exception_with_output,
    format_loaded_binary_line,
    get_logger,
    summarize_value,
)

logger = get_logger(__name__)
_INITIAL_ENV = dict(os.environ)


@dataclass(slots=True)
class CliOptions:
    lib_dir: Path
    provider_names: list[str]
    dry_run: bool
    debug: bool
    no_cache: bool
    # Binary-level fields forwarded verbatim to the Binary constructor.
    # Binary's own model validators propagate them to each provider via
    # the existing install/update kwarg path, which is
    # also where the ``supports_postinstall_disable`` /
    # ``supports_min_release_age`` warning emitters live.
    min_version: str | None = None
    postinstall_scripts: bool | None = None
    min_release_age: float | None = None
    overrides: dict[str, Any] | None = None
    handler_overrides: HandlerDict | None = None
    # Provider-level fields forwarded to every provider constructor.
    install_root: Path | None = None
    bin_dir: Path | None = None
    euid: int | None = None
    install_timeout: int | None = None
    version_timeout: int | None = None


_NONE_STRINGS = frozenset({"", "none", "null"})
_ACTIVATE_SHELL_NAMES = frozenset({"bash", "zsh", "fish"})


def _none_or_stripped(raw: str | None) -> str | None:
    """Return ``raw.strip()`` unless the value is the ``None`` /
    ``'None'`` / ``'null'`` / empty-string sentinel.

    Called from every CLI parser below as a single short-circuit so
    pyright can narrow ``raw`` past the ``None`` branch and each parser
    stays focused on its one-value-type conversion logic.
    """

    if raw is None:
        return None
    stripped = raw.strip()
    return None if stripped.lower() in _NONE_STRINGS else stripped


def _parse_min_version(raw: str | None) -> str | None:
    return _none_or_stripped(raw)


def _parse_cli_bool(raw: str | None) -> bool | None:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    lowered = stripped.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise click.BadParameter(f"expected a bool or 'None', got {raw!r}")


def _parse_cli_float(raw: str | None) -> float | None:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    try:
        return float(stripped)
    except ValueError as err:
        raise click.BadParameter(f"expected a float or 'None', got {raw!r}") from err


def _parse_cli_int(raw: str | None) -> int | None:
    """Parse an integer from a CLI flag, accepting ``"10"`` and ``"10.0"``.

    Rejects ``"10.5"`` (non-integer float) so typos don't silently
    truncate. Returns None for the ``None``/``null``/empty sentinels.
    """

    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        as_float = float(stripped)
    except ValueError as err:
        raise click.BadParameter(
            f"expected an int or 'None', got {raw!r}",
        ) from err
    as_int = int(as_float)
    if as_float != as_int:
        raise click.BadParameter(f"expected an int or 'None', got {raw!r}")
    return as_int


def _parse_overrides(raw: str | None) -> dict[str, Any] | None:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as err:
        raise click.BadParameter(f"--overrides must be valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise click.BadParameter("--overrides must be a JSON object")
    return data


def _parse_handler_override(raw: str | None) -> Any:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _parse_cli_path(raw: str | None) -> Path | None:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    return Path(stripped).expanduser().resolve()


# Click ``callback=`` adapter: run the supplied parser over every raw
# click value so each option's final value is already typed (bool / int
# / float / Path / dict) by the time it reaches any command callback.
# build_cli_options, build_binary, and build_providers downstream only
# ever see typed values — no string parsing below this layer.
def _click_parse(parser: Callable[[str | None], Any]) -> Callable[..., Any]:
    return lambda _ctx, _param, value: parser(value)


def parse_script_metadata(
    script_path: Path,
    max_lines: int = 50,
) -> dict[str, Any] | None:
    """Extract inline ``/// script`` metadata from a script file.

    Scans the first *max_lines* lines for a ``/// script`` opening marker
    and a closing ``///``.  Content lines between the markers are stripped
    of their leading comment prefix (everything up to and including the
    first whitespace) and the resulting text is parsed as TOML.

    Works with any single-token comment prefix (``#``, ``//``, ``--``,
    ``;``, ``/*``, …) — the prefix is never hard-coded; we simply discard
    the first whitespace-delimited token on each content line.
    """

    try:
        text = script_path.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        raise click.ClickException(f"cannot read script {script_path}: {err}") from err

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
        stripped = lines[i].strip()
        parts = stripped.split(None, 1)
        if len(parts) < 2:
            toml_lines.append("")
        else:
            toml_lines.append(parts[1])

    try:
        return tomllib.loads("\n".join(toml_lines))
    except Exception as err:
        raise click.ClickException(
            f"invalid TOML in /// script block of {script_path}: {err}",
        ) from err


def get_package_version() -> str:
    try:
        return metadata.version("abxpkg")
    except metadata.PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with pyproject_path.open("rb") as pyproject_file:
            project = tomllib.load(pyproject_file)
        return str(project["project"]["version"])


def resolve_lib_dir(raw_value: str | Path | None) -> Path:
    env_value = os.environ.get("ABXPKG_LIB_DIR")
    raw_str = raw_value if isinstance(raw_value, str) else None
    if raw_str is not None and _none_or_stripped(raw_str) is None:
        os.environ.pop("ABXPKG_LIB_DIR", None)
        return DEFAULT_LIB_DIR.expanduser().resolve()
    if env_value is not None and _none_or_stripped(env_value) is None:
        os.environ.pop("ABXPKG_LIB_DIR", None)
        return DEFAULT_LIB_DIR.expanduser().resolve()

    lib_dir = Path(raw_value or _none_or_stripped(env_value) or DEFAULT_LIB_DIR)
    resolved = lib_dir.expanduser().resolve()
    os.environ["ABXPKG_LIB_DIR"] = str(resolved)
    return resolved


def parse_provider_names(raw_value: str | None) -> list[str]:
    if raw_value is None:
        env_value = os.environ.get("ABXPKG_BINPROVIDERS")
        if env_value is None:
            return list(DEFAULT_PROVIDER_NAMES)
        raw_value = env_value

    provider_names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_value.split(","):
        name = raw_name.strip()
        if not name or name in seen:
            continue
        provider_names.append(name)
        seen.add(name)

    if not provider_names:
        raise click.BadParameter("expected at least one provider name")

    invalid = [name for name in provider_names if name not in PROVIDER_CLASS_BY_NAME]
    if invalid:
        valid = ", ".join(ALL_PROVIDER_NAMES)
        invalid_names = ", ".join(invalid)
        raise click.BadParameter(
            f"unknown provider name(s): {invalid_names}. Valid providers: {valid}",
        )

    return provider_names


def resolve_dry_run(flag_value: bool | None) -> bool:
    if flag_value is not None:
        return flag_value
    return env_flag_is_true("ABXPKG_DRY_RUN") or env_flag_is_true("DRY_RUN")


def resolve_debug(flag_value: bool | None) -> bool:
    if flag_value is not None:
        return flag_value
    return env_flag_is_true("ABXPKG_DEBUG")


def resolve_no_cache(flag_value: bool | None) -> bool:
    if flag_value is not None:
        return flag_value
    return env_flag_is_true("ABXPKG_NO_CACHE")


def build_providers(
    provider_names: list[str],
    *,
    dry_run: bool = False,
    install_root: Path | None = None,
    bin_dir: Path | None = None,
    euid: int | None = None,
    install_timeout: int | None = None,
    version_timeout: int | None = None,
) -> list[BinProvider]:
    providers: list[BinProvider] = []
    for provider_name in provider_names:
        provider_class = PROVIDER_CLASS_BY_NAME[provider_name]
        provider_kwargs: dict[str, Any] = {"dry_run": dry_run}
        if euid is not None:
            provider_kwargs["euid"] = euid
        if install_timeout is not None:
            provider_kwargs["install_timeout"] = install_timeout
        if version_timeout is not None:
            provider_kwargs["version_timeout"] = version_timeout
        # User-supplied --install-root overrides the provider's default.
        # Otherwise each provider resolves its own install_root from
        # ABXPKG_LIB_DIR (set by resolve_lib_dir) via default_factory.
        if install_root is not None:
            provider_kwargs["install_root"] = install_root
        if bin_dir is not None:
            provider_kwargs["bin_dir"] = bin_dir
        providers.append(provider_class(**provider_kwargs))
    return providers


def build_binary(binary_name: str, options: CliOptions, *, dry_run: bool) -> Binary:
    merged_overrides = options.overrides
    if options.handler_overrides:
        merged_overrides = {
            provider_name: dict(options.handler_overrides)
            for provider_name in options.provider_names
        }
        if options.overrides:

            def merge_dicts(
                base: dict[str, Any],
                override: dict[str, Any],
            ) -> dict[str, Any]:
                merged: dict[str, Any] = dict(base)
                for key, value in override.items():
                    existing = merged.get(key)
                    if isinstance(existing, dict) and isinstance(value, dict):
                        merged[key] = merge_dicts(existing, value)
                    else:
                        merged[key] = value
                return merged

            for provider_name, provider_overrides in options.overrides.items():
                existing = merged_overrides.get(provider_name, {})
                if isinstance(existing, dict) and isinstance(provider_overrides, dict):
                    merged_overrides[provider_name] = merge_dicts(
                        existing,
                        provider_overrides,
                    )
                else:
                    merged_overrides[provider_name] = provider_overrides

    binary_kwargs: dict[str, Any] = {
        "name": binary_name,
        "binproviders": build_providers(
            options.provider_names,
            dry_run=dry_run,
            install_root=options.install_root,
            bin_dir=options.bin_dir,
            euid=options.euid,
            install_timeout=options.install_timeout,
            version_timeout=options.version_timeout,
        ),
    }
    # Binary's field validators coerce str → SemVer, dict → BinaryOverrides,
    # etc., so just forward the parsed values verbatim. Binary.install /
    # update then propagate postinstall_scripts /
    # min_release_age to each provider's install() kwarg, where the
    # existing ``supports_postinstall_disable`` / ``supports_min_release_age``
    # warn-and-ignore path fires for providers that can't enforce them.
    for key, value in (
        ("min_version", options.min_version),
        ("postinstall_scripts", options.postinstall_scripts),
        ("min_release_age", options.min_release_age),
        ("overrides", merged_overrides),
    ):
        if value is not None:
            binary_kwargs[key] = value
    return Binary(**binary_kwargs)


def build_cli_options(
    ctx: click.Context | None,
    *,
    lib_dir: str | None,
    global_mode: bool | None,
    binproviders: str | None,
    dry_run: bool | None,
    debug: bool | None,
    no_cache: bool | None,
    min_version: str | None,
    abspath_override: Any = None,
    version_override: Any = None,
    install_args_override: Any = None,
    packages_override: Any = None,
    postinstall_scripts: bool | None,
    min_release_age: float | None,
    overrides: dict[str, Any] | None,
    install_root: Path | None,
    bin_dir: Path | None,
    euid: int | None,
    install_timeout: int | None,
    version_timeout: int | None,
) -> CliOptions:
    """Single entry-point used by the group callback and every subcommand.

    All CLI flag values arrive here already typed — click's per-option
    ``callback=`` parsers run first, so there's no string-to-bool /
    string-to-int / JSON-decode work left at this layer. Every field is
    forwarded verbatim into the returned ``CliOptions``; Binary /
    BinProvider constructors downstream honor them via the existing
    kwarg paths, and the warn-and-ignore machinery in
    ``BinProvider.__init__`` / ``BinProvider.install`` handles providers
    that can't enforce a given option.

    Subcommand-level values override the group-level values on
    ``ctx.obj['group_options']`` field-by-field; if ``ctx`` is ``None``
    (the group callback itself), values are taken as-is.
    """

    group: CliOptions | None = (
        cast(CliOptions, ctx.obj["group_options"])
        if ctx is not None and ctx.obj and "group_options" in ctx.obj
        else None
    )

    def _override(value: Any, group_value: Any) -> Any:
        """Inherit from group unless the subcommand supplied a value."""
        return group_value if value is None else value

    if global_mode is True:
        lib_dir = "None"

    handler_overrides: HandlerDict | None = None
    for key, value in (
        ("abspath", abspath_override),
        ("version", version_override),
        ("install_args", install_args_override),
        ("packages", packages_override),
    ):
        if value is None:
            continue
        if handler_overrides is None:
            handler_overrides = {}
        handler_overrides[key] = value

    if group is None:
        provider_names = parse_provider_names(binproviders)
        os.environ["ABXPKG_BINPROVIDERS"] = ",".join(provider_names)
        return CliOptions(
            lib_dir=resolve_lib_dir(lib_dir),
            provider_names=provider_names,
            dry_run=resolve_dry_run(dry_run),
            debug=resolve_debug(debug),
            no_cache=resolve_no_cache(no_cache),
            min_version=min_version,
            handler_overrides=handler_overrides,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            overrides=overrides,
            install_root=install_root,
            bin_dir=bin_dir,
            euid=euid,
            install_timeout=install_timeout,
            version_timeout=version_timeout,
        )
    provider_names = (
        group.provider_names
        if binproviders is None
        else parse_provider_names(binproviders)
    )
    os.environ["ABXPKG_BINPROVIDERS"] = ",".join(provider_names)
    return CliOptions(
        lib_dir=(
            group.lib_dir
            if lib_dir is None
            else resolve_lib_dir("None" if global_mode is True else lib_dir)
        ),
        provider_names=provider_names,
        dry_run=_override(dry_run, group.dry_run),
        debug=_override(debug, group.debug),
        no_cache=_override(no_cache, group.no_cache),
        min_version=_override(min_version, group.min_version),
        handler_overrides=(
            group.handler_overrides
            if handler_overrides is None
            else {
                **(group.handler_overrides or {}),
                **handler_overrides,
            }
        ),
        postinstall_scripts=_override(postinstall_scripts, group.postinstall_scripts),
        min_release_age=_override(min_release_age, group.min_release_age),
        overrides=_override(overrides, group.overrides),
        install_root=_override(install_root, group.install_root),
        bin_dir=_override(bin_dir, group.bin_dir),
        euid=_override(euid, group.euid),
        install_timeout=_override(install_timeout, group.install_timeout),
        version_timeout=_override(version_timeout, group.version_timeout),
    )


def _stream_is_tty(stream: Any) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def is_interactive_tty() -> bool:
    return _stream_is_tty(sys.stdin) and _stream_is_tty(sys.stderr)


def _console_for_stream(*, err: bool):
    stream = sys.stderr if err else sys.stdout
    if not (RICH_INSTALLED and _stream_is_tty(stream)):
        return None
    from rich.console import Console
    from rich.highlighter import ReprHighlighter

    return Console(
        file=stream,
        highlighter=ReprHighlighter(),
        theme=Theme(
            {
                "repr.str": "#87af87",
                "repr.path": "#87af87",
                "repr.filename": "#87af87",
                "repr.url": "#87af87",
            },
        ),
    )


def _echo(message: Any, *, err: bool = False) -> None:
    console = _console_for_stream(err=err)
    if console is not None:
        if isinstance(message, str):
            console.print(ReprHighlighter()(Text.from_ansi(message)), highlight=False)
        else:
            console.print(message, highlight=False)
        return
    click.echo(str(message), err=err)


class _CliRichHandler(RichHandler):
    _STDERR_PAYLOAD_PREFIX = "  \x1b[2;31m>\x1b[0m "
    _STDOUT_PAYLOAD_PREFIX = "  > "

    def render_message(
        self,
        record: py_logging.LogRecord,
        message: str,
    ) -> Text:
        contains_ansi = "\x1b[" in message
        lines = message.splitlines()
        highlighter = (
            getattr(record, "highlighter", None)
            or self.highlighter
            or getattr(self.console, "highlighter", None)
        )
        has_completed_process_payload = any(
            line.lstrip(" ").startswith("> ")
            or line.lstrip(" ").startswith(self._STDERR_PAYLOAD_PREFIX.strip())
            for line in lines
        )
        if contains_ansi or has_completed_process_payload:
            message_text = Text()
            for idx, line in enumerate(lines):
                indent = line[: len(line) - len(line.lstrip(" "))]
                stripped = line[len(indent) :]
                if stripped.startswith(self._STDERR_PAYLOAD_PREFIX.strip()):
                    message_text.append(indent)
                    message_text.append(">", style="red")
                    message_text.append(
                        " " + stripped[len(self._STDERR_PAYLOAD_PREFIX.strip()) :],
                        style="grey42",
                    )
                elif stripped.startswith(self._STDOUT_PAYLOAD_PREFIX.strip()):
                    message_text.append(indent + stripped, style="grey42")
                elif "\x1b[" in line:
                    line_text = Text.from_ansi(line)
                    if highlighter:
                        line_text = highlighter(line_text)
                    plain = line_text.plain
                    for match in re.finditer(
                        r"(?<![\w.])([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)(?=\()",
                        plain,
                    ):
                        method_name = match.group(2)
                        method_start = match.start(2)
                        method_end = match.end(2)
                        if "cache" in method_name:
                            line_text.stylize("#4e4e4e", method_start, method_end)
                        elif method_name in {
                            "install",
                            "update",
                            "uninstall",
                            "exec",
                        }:
                            line_text.stylize("#d78700", method_start, method_end)
                        elif method_name == "load" or method_name.startswith("get_"):
                            line_text.stylize("#2e8b57", method_start, method_end)
                    message_text.append_text(line_text)
                else:
                    line_text = Text(line)
                    if highlighter:
                        line_text = highlighter(line_text)
                    plain = line_text.plain
                    for match in re.finditer(
                        r"(?<![\w.])([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)(?=\()",
                        plain,
                    ):
                        method_name = match.group(2)
                        method_start = match.start(2)
                        method_end = match.end(2)
                        if "cache" in method_name:
                            line_text.stylize("#4e4e4e", method_start, method_end)
                        elif method_name in {
                            "install",
                            "update",
                            "uninstall",
                            "exec",
                        }:
                            line_text.stylize("#d78700", method_start, method_end)
                        elif method_name == "load" or method_name.startswith("get_"):
                            line_text.stylize("#2e8b57", method_start, method_end)
                    message_text.append_text(line_text)
                if idx < len(lines) - 1:
                    message_text.append("\n")
        else:
            message_text = Text.from_ansi(message)
            if highlighter:
                message_text = highlighter(message_text)
            plain = message_text.plain
            for match in re.finditer(
                r"(?<![\w.])([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)(?=\()",
                plain,
            ):
                method_name = match.group(2)
                method_start = match.start(2)
                method_end = match.end(2)
                if "cache" in method_name:
                    message_text.stylize("#4e4e4e", method_start, method_end)
                elif method_name in {"install", "update", "uninstall", "exec"}:
                    message_text.stylize("#d78700", method_start, method_end)
                elif method_name == "load" or method_name.startswith("get_"):
                    message_text.stylize("#2e8b57", method_start, method_end)
        if self.keywords is None:
            self.keywords = self.KEYWORDS
        if self.keywords:
            message_text.highlight_words(self.keywords, "logging.keyword")
        message_text.stylize("dim")
        return message_text


class _CliDuplicateStdoutFilter(py_logging.Filter):
    def filter(self, record: py_logging.LogRecord) -> bool:
        return not bool(getattr(record, "abx_cli_duplicate_stdout", False))


def configure_cli_logging(*, debug: bool) -> None:
    level = "DEBUG" if debug else "INFO"
    console = _console_for_stream(err=True)
    duplicate_stdout_filter = _CliDuplicateStdoutFilter()
    if console is not None:
        handler = _CliRichHandler(
            console=console,
            markup=False,
            show_time=False,
            show_level=False,
            show_path=False,
            highlighter=ReprHighlighter(),
        )
        handler.addFilter(duplicate_stdout_filter)
        configure_logging(
            level=level,
            handler=handler,
            fmt="%(message)s",
            replace_handlers=True,
        )
        return
    handler = py_logging.StreamHandler(sys.stderr)
    handler.addFilter(duplicate_stdout_filter)
    configure_logging(
        level=level,
        handler=handler,
        fmt="%(message)s",
        replace_handlers=True,
    )


def format_error(err: Exception) -> str:
    return format_exception_with_output(err)


def cached_binaries(
    options: CliOptions,
    names: tuple[str, ...] = (),
    providers: Iterable[BinProvider] | None = None,
) -> list[tuple[str, str, str, Path, str, str]]:
    records: list[tuple[str, str, str, Path, str, str]] = []
    selected_names = set(names)
    provider_instances = tuple(
        providers
        if providers is not None
        else build_providers(
            options.provider_names,
            dry_run=False,
            install_root=options.install_root,
            bin_dir=options.bin_dir,
            euid=options.euid,
            install_timeout=options.install_timeout,
            version_timeout=options.version_timeout,
        ),
    )
    for provider in provider_instances:
        provider_name = provider.name
        derived_env_path = provider.derived_env_path
        if derived_env_path is None or not derived_env_path.is_file():
            continue
        cache = load_derived_cache(derived_env_path)
        installer_bin = cast(
            str,
            type(provider).model_fields["INSTALLER_BIN"].default,
        )
        for cache_key, record in sorted(cache.items()):
            if not isinstance(record, dict):
                continue

            cached_provider_name = record.get("provider_name")
            cached_bin_name = record.get("bin_name")
            cached_abspath = record.get("abspath")
            if (
                not isinstance(cached_provider_name, str)
                or not isinstance(cached_bin_name, str)
                or not isinstance(cached_abspath, str)
            ):
                try:
                    cached_provider_name, cached_bin_name, cached_abspath = json.loads(
                        cache_key,
                    )
                except Exception:
                    continue
            if (
                cached_provider_name != provider_name
                or not isinstance(cached_bin_name, str)
                or not isinstance(cached_abspath, str)
            ):
                continue
            if names:
                if provider_name in selected_names:
                    pass
                elif cached_bin_name == installer_bin:
                    continue
                elif cached_bin_name not in selected_names:
                    continue

            version = record.get("loaded_version")
            resolved_provider_name = record.get("resolved_provider_name")
            cache_kind = record.get("cache_kind")
            if not isinstance(version, str):
                continue
            if not isinstance(resolved_provider_name, str):
                resolved_provider_name = provider_name
            if not isinstance(cache_kind, str):
                cache_kind = (
                    "dependency" if cached_bin_name == installer_bin else "binary"
                )

            abspath_path = Path(cached_abspath)
            if not abspath_path.exists():
                continue

            records.append(
                (
                    provider_name,
                    resolved_provider_name,
                    cached_bin_name,
                    abspath_path,
                    version,
                    cache_kind,
                ),
            )

    return records


def list_cached_binaries(
    options: CliOptions,
    names: tuple[str, ...] = (),
) -> list[str]:
    providers = tuple(
        build_providers(
            options.provider_names,
            dry_run=False,
            install_root=options.install_root,
            bin_dir=options.bin_dir,
            euid=options.euid,
            install_timeout=options.install_timeout,
            version_timeout=options.version_timeout,
        ),
    )
    installer_lines: list[str] = []
    binary_lines: list[str] = []
    seen_installer_lines: set[str] = set()
    seen_binary_lines: set[str] = set()
    for (
        _cache_owner,
        resolved_provider_name,
        _bin_name,
        abspath,
        version,
        _cache_kind,
    ) in cached_binaries(options, names, providers=providers):
        line = format_loaded_binary_line(
            version,
            abspath,
            resolved_provider_name,
            _bin_name,
        )
        installer_bin = cast(
            str,
            PROVIDER_CLASS_BY_NAME[_cache_owner].model_fields["INSTALLER_BIN"].default,
        )
        if _bin_name == installer_bin:
            if line in seen_installer_lines:
                continue
            seen_installer_lines.add(line)
            installer_lines.append(line)
        else:
            if line in seen_binary_lines or line in seen_installer_lines:
                continue
            seen_binary_lines.add(line)
            binary_lines.append(line)
    if installer_lines and binary_lines:
        return [*installer_lines, "", *binary_lines]
    return installer_lines or binary_lines


def version_report(options: CliOptions):
    highlighter = ReprHighlighter()
    all_providers = build_providers(
        list(ALL_PROVIDER_NAMES),
        dry_run=False,
        install_root=options.install_root,
        bin_dir=options.bin_dir,
        euid=options.euid,
        install_timeout=options.install_timeout,
        version_timeout=options.version_timeout,
    )
    install_timeout = all_providers[0].install_timeout if all_providers else 120
    version_timeout = all_providers[0].version_timeout if all_providers else 10

    def render_env_value(value: Any) -> str:
        if isinstance(value, Path):
            return summarize_value(value, 10_000)
        if isinstance(value, str):
            return (
                summarize_value(value, 10_000)
                if os.sep in value or value.startswith(("~", ".", "/"))
                else value
            )
        return str(value)

    yield get_package_version()
    summary_line = Text()

    def append_part(
        text: str,
        *,
        style: str | None = None,
    ) -> None:
        if summary_line:
            summary_line.append(" ")
        summary_line.append(text, style=style)

    def append_env_var(name: str, value: Any, prefix: str = " ") -> None:
        if summary_line:
            summary_line.append(prefix)
        summary_line.append(
            f"{name}=",
            style="green" if name in _INITIAL_ENV else "grey42",
        )
        summary_line.append(render_env_value(value))

    append_part(f"ARCH={platform.machine()}")
    append_part(f"OS={platform.system()}")
    append_part(f"PLATFORM={platform.platform()}")
    append_part(
        f"PYTHON={sys.implementation.name.title()}-{'.'.join(map(str, sys.version_info[:3]))}",
    )
    append_env_var("ABXPKG_DRY_RUN", options.dry_run, prefix="\n")
    append_env_var("ABXPKG_DEBUG", options.debug)
    append_env_var("ABXPKG_NO_CACHE", options.no_cache)
    append_env_var("ABXPKG_INSTALL_TIMEOUT", install_timeout)
    append_env_var("ABXPKG_VERSION_TIMEOUT", version_timeout)
    append_env_var("ABXPKG_POSTINSTALL_SCRIPTS", options.postinstall_scripts)
    append_env_var("ABXPKG_MIN_RELEASE_AGE", options.min_release_age)
    append_env_var("ABXPKG_BINPROVIDERS", ",".join(options.provider_names))
    append_env_var("ABXPKG_LIB_DIR", options.lib_dir)
    yield summary_line
    for provider in build_providers(
        options.provider_names,
        dry_run=False,
        install_root=options.install_root,
        bin_dir=options.bin_dir,
        euid=options.euid,
        install_timeout=options.install_timeout,
        version_timeout=options.version_timeout,
    ):
        try:
            provider.setup_PATH(no_cache=options.no_cache)
        except Exception:
            pass
        emoji = (
            type(provider).__private_attributes__["_log_emoji"].default
            or BinProvider.__private_attributes__["_log_emoji"].default
        )
        installer_binary = None
        try:
            installer_binary = provider.INSTALLER_BINARY(no_cache=options.no_cache)
        except Exception:
            installer_binary = None

        status = "✅" if provider.is_valid else "❌"
        yield ""
        heading = Text()
        heading.append(f"{emoji} ", style="bold")
        heading.append(
            f"{provider.__class__.__name__} ({provider.name})",
            style="bold bright_white",
        )
        heading.append(f" {status}", style="green" if status == "✅" else "red")
        yield heading

        installer_line = Text("   ")
        installer_line.append("INSTALLER_BINARY=", style="bold cyan")
        if installer_binary and installer_binary.loaded_abspath:
            resolved_by = (
                installer_binary.loaded_binprovider.name
                if installer_binary.loaded_binprovider is not None
                else provider.name
            )
            installer_line.append(
                " ".join(
                    (
                        str(installer_binary.loaded_version or "unknown"),
                        summarize_value(installer_binary.loaded_abspath, 10_000),
                        f"({installer_binary.name})",
                        resolved_by,
                        f"euid={provider.EUID}",
                    ),
                ),
            )
        else:
            installer_line.append(f"None euid={provider.EUID}")
        yield installer_line

        path_line = Text("   ")
        path_line.append("PATH=", style="bold cyan")
        path_line.append(
            summarize_value(
                str(provider.PATH).replace(DEFAULT_ENV_PATH, "$PATH"),
                10_000,
            ),
        )
        yield path_line

        provider_env = provider.ENV
        env_line = Text("   ")
        env_line.append("ENV=", style="bold cyan")
        env_line.append(
            "{"
            + ", ".join(
                f"{key}: {summarize_value(str(value).replace(DEFAULT_ENV_PATH, '$PATH'), 10_000) if isinstance(value, str) else summarize_value(value, 10_000)}"
                for key, value in provider_env.items()
                if value is not None
            )
            + "}",
        )
        yield env_line
        install_root_line = Text("   ")
        install_root_line.append("install_root=", style="bold cyan")
        install_root_line.append(summarize_value(provider.install_root, 10_000))
        yield install_root_line
        bin_dir_line = Text("   ")
        bin_dir_line.append("bin_dir=", style="bold cyan")
        bin_dir_line.append(summarize_value(provider.bin_dir, 10_000))
        yield bin_dir_line

        upstream_binary_lines = [
            format_loaded_binary_line(
                binary.loaded_version,
                binary.loaded_abspath,
                (
                    binary.loaded_binprovider.name
                    if binary.loaded_binprovider is not None
                    else provider.name
                ),
                binary.name,
            )
            for binary in provider.depends_on_binaries()
            if binary.loaded_abspath is not None and binary.loaded_version is not None
        ]
        installed_binary_lines = [
            format_loaded_binary_line(
                binary.loaded_version,
                binary.loaded_abspath,
                (
                    binary.loaded_binprovider.name
                    if binary.loaded_binprovider is not None
                    else provider.name
                ),
                binary.name,
            )
            for binary in provider.installed_binaries()
            if binary.loaded_abspath is not None and binary.loaded_version is not None
        ]

        if upstream_binary_lines:
            upstream_line = Text("   ")
            upstream_line.append("depends_on_binaries=", style="bold cyan")
            yield upstream_line
            for line in upstream_binary_lines:
                yield highlighter(Text("      " + line))

        if installed_binary_lines:
            installed_line = Text("   ")
            installed_line.append("installed_binaries=", style="bold cyan")
            yield installed_line
            for line in installed_binary_lines:
                yield highlighter(Text("      " + line))


def shared_options(command):
    # Options apply innermost-first; the --help listing order is the
    # reverse of the decoration order, so --lib / --binproviders /
    # --dry-run stay last here to preserve the pre-existing --help layout.
    # Every non-trivial option gets a ``callback=`` that runs its raw
    # string through a parser so the command receives a typed value
    # (bool / int / float / Path / dict) instead of a string.
    for decorator in (
        click.option(
            "--version-timeout",
            metavar="SECONDS",
            default=None,
            callback=_click_parse(_parse_cli_int),
            help="Seconds to wait for version/metadata probes. 'None' restores default.",
        ),
        click.option(
            "--install-timeout",
            metavar="SECONDS",
            default=None,
            callback=_click_parse(_parse_cli_int),
            help="Seconds to wait for install/update/uninstall subprocesses. 'None' restores default.",
        ),
        click.option(
            "--euid",
            metavar="UID",
            default=None,
            callback=_click_parse(_parse_cli_int),
            help="Pin the UID used when providers shell out. 'None' auto-detects.",
        ),
        click.option(
            "--bin-dir",
            metavar="PATH",
            default=None,
            callback=_click_parse(_parse_cli_path),
            help="Override the per-provider bin directory. Set 'None' to install globally.",
        ),
        click.option(
            "--install-root",
            metavar="PATH",
            default=None,
            callback=_click_parse(_parse_cli_path),
            help="Override the per-provider install directory. Set 'None' to install globally.",
        ),
        click.option(
            "--overrides",
            metavar="JSON",
            default=None,
            callback=_click_parse(_parse_overrides),
            help='JSON-encoded Binary.overrides dict, e.g. \'{"pip":{"install_args":["pkg"]}}\'. \'None\' restores defaults.',
        ),
        click.option(
            "--min-release-age",
            metavar="DAYS",
            default=None,
            callback=_click_parse(_parse_cli_float),
            help="Minimum days since publication. Providers that can't enforce it warn and ignore. 'None' restores defaults.",
        ),
        click.option(
            "--postinstall-scripts",
            metavar="BOOL",
            default=None,
            callback=_click_parse(_parse_cli_bool),
            help="Allow post-install scripts ('True'/'False'/'1'/'0'/'None' or bare `--postinstall-scripts` for implicit True). Providers that can't disable them warn and ignore.",
        ),
        click.option(
            "--min-version",
            metavar="SEMVER",
            default=None,
            callback=_click_parse(_parse_min_version),
            help="Minimum acceptable version floor for the binary. 'None' means any version is acceptable.",
        ),
        click.option(
            "--dry-run",
            metavar="BOOL",
            default=None,
            callback=_click_parse(_parse_cli_bool),
            help="Show installer commands without executing them ('True'/'False'/'None' or bare `--dry-run` for implicit True).",
        ),
        click.option(
            "--debug",
            metavar="BOOL",
            default=None,
            callback=_click_parse(_parse_cli_bool),
            help="Emit DEBUG logs to stderr ('True'/'False'/'None' or bare `--debug` for implicit True). Defaults to ABXPKG_DEBUG or False.",
        ),
        click.option(
            "--no-cache",
            metavar="BOOL",
            default=None,
            callback=_click_parse(_parse_cli_bool),
            help="Bypass cached/current-state checks and force install/update probes ('True'/'False'/'None' or bare `--no-cache` for implicit True). Defaults to ABXPKG_NO_CACHE or False.",
        ),
        click.option(
            "--binproviders",
            metavar="LIST",
            default=None,
            help="Comma-separated provider order. Defaults to ABXPKG_BINPROVIDERS or all providers.",
        ),
        click.option(
            "--global",
            "global_mode",
            default=None,
            flag_value=True,
            help="Thin alias for --lib=None. Bare --global = True.",
        ),
        click.option(
            "--lib",
            "lib_dir",
            metavar="PATH",
            default=None,
            help="Base library directory. Defaults to ABXPKG_LIB_DIR or $XDG_CONFIG_HOME/abx/lib.",
        ),
    ):
        command = decorator(command)
    return command


def binary_override_options(command):
    for decorator in (
        click.option(
            "--packages",
            "packages_override",
            metavar="JSON_OR_STR",
            default=None,
            callback=_click_parse(_parse_handler_override),
            help="Default packages override applied to all selected providers unless --overrides specifies a provider-specific value.",
        ),
        click.option(
            "--install-args",
            "install_args_override",
            metavar="JSON_OR_STR",
            default=None,
            callback=_click_parse(_parse_handler_override),
            help="Default install_args override applied to all selected providers unless --overrides specifies a provider-specific value.",
        ),
        click.option(
            "--version",
            "version_override",
            metavar="JSON_OR_STR",
            default=None,
            callback=_click_parse(_parse_handler_override),
            help="Default version override applied to all selected providers unless --overrides specifies a provider-specific value.",
        ),
        click.option(
            "--abspath",
            "abspath_override",
            metavar="JSON_OR_STR",
            default=None,
            callback=_click_parse(_parse_handler_override),
            help="Default abspath override applied to all selected providers unless --overrides specifies a provider-specific value.",
        ),
    ):
        command = decorator(command)
    return command


# Single canonical list of kwargs carried by every CLI callback that
# uses @shared_options. Defined once so command callbacks don't have to
# enumerate all of them, and so ``get_command_options`` has a single
# source of truth for what gets forwarded to ``build_cli_options``.
_SHARED_OPTION_NAMES: tuple[str, ...] = (
    "lib_dir",
    "global_mode",
    "binproviders",
    "dry_run",
    "debug",
    "no_cache",
    "min_version",
    "abspath_override",
    "version_override",
    "install_args_override",
    "packages_override",
    "postinstall_scripts",
    "min_release_age",
    "overrides",
    "install_root",
    "bin_dir",
    "euid",
    "install_timeout",
    "version_timeout",
)


def get_command_options(
    ctx: click.Context,
    **shared_kwargs: Any,
) -> CliOptions:
    return build_cli_options(ctx, **shared_kwargs)


def run_binary_command(
    binary_name: str,
    *,
    action: str,
    options: CliOptions,
) -> None:
    binary = build_binary(binary_name, options, dry_run=options.dry_run)
    method = getattr(binary, action)
    configure_cli_logging(debug=options.debug)

    try:
        if action == "load":
            result = method(no_cache=options.no_cache)
        else:
            result = method(dry_run=options.dry_run, no_cache=options.no_cache)
    except ABXPkgError as err:
        raise click.ClickException(format_error(err)) from err

    if options.dry_run and action != "load":
        return

    if action == "uninstall":
        _echo(binary_name)
        return

    provider = result.loaded_binprovider
    provider_name = provider.name if provider is not None else "unknown"
    _echo(
        format_loaded_binary_line(
            result.loaded_version,
            result.loaded_abspath,
            provider_name,
            result.name,
        ),
    )


def resolve_runtime_binary(
    binary_name: str,
    *,
    options: CliOptions,
    install_before_run: bool = False,
    update_before_run: bool = False,
) -> tuple[Binary, list[BinProvider]]:
    runtime_binproviders: list[BinProvider] = []
    binary = build_binary(binary_name, options, dry_run=options.dry_run)
    runtime_binproviders.extend(binary.binproviders)
    update_provider_names = [
        provider_name
        for provider_name in options.provider_names
        if provider_name != "env"
    ]

    try:
        if update_before_run:
            loaded_for_update = None
            try:
                loaded_for_update = binary.load(no_cache=options.no_cache)
            except ABXPkgError:
                loaded_for_update = None
            if loaded_for_update is not None and update_provider_names:
                binary = loaded_for_update.update(
                    binproviders=update_provider_names,
                    dry_run=options.dry_run,
                    no_cache=options.no_cache,
                )
            else:
                binary = binary.update(
                    binproviders=update_provider_names or None,
                    dry_run=options.dry_run,
                    no_cache=options.no_cache,
                )
                if not binary.is_valid:
                    binary = binary.install(
                        dry_run=options.dry_run,
                        no_cache=options.no_cache,
                    )
        elif install_before_run:
            binary = binary.install(
                dry_run=options.dry_run,
                no_cache=options.no_cache,
            )
        else:
            binary = binary.load(no_cache=options.no_cache)
    except ABXPkgError as err:
        raise click.ClickException(format_error(err)) from err

    return binary, runtime_binproviders


def get_runtime_exec_providers(
    binary: Binary,
    runtime_binproviders: Iterable[BinProvider] = (),
) -> list[BinProvider]:
    if not binary.is_valid:
        raise click.ClickException(
            f"abxpkg: {binary.name}: binary could not be loaded",
        )

    assert binary.loaded_binprovider is not None
    return [
        provider
        for provider in runtime_binproviders
        if provider.name != binary.loaded_binprovider.name
        or provider.install_root != binary.loaded_binprovider.install_root
        or provider.bin_dir != binary.loaded_binprovider.bin_dir
    ]


def build_runtime_exec_env(
    binary: Binary,
    runtime_binproviders: Iterable[BinProvider] = (),
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    assert binary.loaded_binprovider is not None
    env = dict(os.environ if base_env is None else base_env)
    other_runtime_binproviders = get_runtime_exec_providers(
        binary,
        runtime_binproviders,
    )
    if other_runtime_binproviders:
        env = BinProvider.build_exec_env(
            providers=other_runtime_binproviders,
            base_env=env,
        )
    return binary.loaded_binprovider.build_exec_env(
        providers=[binary.loaded_binprovider],
        base_env=env,
    )


def _render_env_delta_value(
    before: str | None,
    after: str,
) -> str | None:
    if before == after:
        return None
    if before:
        if after.endswith(before):
            prefix = after[: -len(before)]
            if prefix:
                return prefix
        if after.startswith(before):
            suffix = after[len(before) :]
            if suffix:
                return f":{suffix}"
    return after


def render_env_assignment_lines(
    base_env: dict[str, str],
    final_env: dict[str, str],
    *,
    prefix: str = "",
) -> list[str]:
    def render_assignment_value(value: str) -> str:
        if value and re.fullmatch(r"[A-Za-z0-9_./,:@%+=-]+", value):
            return value
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace("`", "\\`")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'

    changed_values: dict[str, str] = {}
    for key, value in final_env.items():
        rendered = _render_env_delta_value(base_env.get(key), value)
        if rendered is not None:
            changed_values[key] = rendered

    ordered_keys = [
        *(["VIRTUAL_ENV"] if "VIRTUAL_ENV" in changed_values else []),
        *(["PATH"] if "PATH" in changed_values else []),
        *sorted(key for key in changed_values if key not in {"VIRTUAL_ENV", "PATH"}),
    ]
    return [
        f"{prefix}{key}={render_assignment_value(changed_values[key])}"
        for key in ordered_keys
    ]


def render_activate_lines(
    base_env: dict[str, str],
    final_env: dict[str, str],
    *,
    shell: str,
) -> list[str]:
    assert shell in _ACTIVATE_SHELL_NAMES

    if shell in {"bash", "zsh"}:
        return render_env_assignment_lines(base_env, final_env, prefix="export ")

    def render_fish_value(value: str) -> str:
        if value and re.fullmatch(r"[A-Za-z0-9_./,:@%+=-]+", value):
            return value
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'

    changed_values: dict[str, str] = {}
    for key, value in final_env.items():
        rendered = _render_env_delta_value(base_env.get(key), value)
        if rendered is not None:
            changed_values[key] = rendered

    ordered_keys = [
        *(["VIRTUAL_ENV"] if "VIRTUAL_ENV" in changed_values else []),
        *(["PATH"] if "PATH" in changed_values else []),
        *sorted(key for key in changed_values if key not in {"VIRTUAL_ENV", "PATH"}),
    ]
    return [
        f"set -x {key} {render_fish_value(changed_values[key])}" for key in ordered_keys
    ]


def parse_activate_shell(
    *,
    bash: bool,
    zsh: bool,
    fish: bool,
) -> str:
    selected = [
        shell_name
        for shell_name, enabled in (("bash", bash), ("zsh", zsh), ("fish", fish))
        if enabled
    ]
    if len(selected) > 1:
        raise click.BadParameter("choose only one of --bash, --zsh, or --fish")
    return selected[0] if selected else "bash"


def render_activate_comment(
    *,
    shell: str,
    binary_names: Iterable[str],
) -> str:
    command = " ".join(
        [
            "abxpkg",
            "activate",
            *([f"--{shell}"] if shell != "bash" else []),
            *(str(binary_name) for binary_name in binary_names),
        ],
    )
    if shell == "fish":
        return f"# {command} | source"
    return f'# eval "$({command})"'


def build_command_exec_env(
    binary_names: Iterable[str],
    *,
    options: CliOptions,
    install_before_run: bool = False,
    update_before_run: bool = False,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    names = tuple(binary_names)
    if not names:
        return BinProvider.build_exec_env(
            providers=build_providers(
                options.provider_names,
                dry_run=options.dry_run,
                install_root=options.install_root,
                bin_dir=options.bin_dir,
                euid=options.euid,
                install_timeout=options.install_timeout,
                version_timeout=options.version_timeout,
            ),
            base_env=env,
        )

    for binary_name in names:
        binary, runtime_binproviders = resolve_runtime_binary(
            binary_name,
            options=options,
            install_before_run=install_before_run,
            update_before_run=update_before_run,
        )
        env = build_runtime_exec_env(
            binary,
            runtime_binproviders,
            base_env=env,
        )
    return env


def clear_lib_dir(lib_dir: Path) -> None:
    if lib_dir.is_symlink() or lib_dir.is_file():
        lib_dir.unlink(missing_ok=True)
        return
    if lib_dir.exists():
        logger.info("$ %s", format_command(["rm", "-rf", str(lib_dir)]))
    shutil.rmtree(lib_dir, ignore_errors=True)


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.pass_context
@shared_options
@click.option(
    "--install",
    "install_before_run",
    is_flag=True,
    default=False,
    help="Used by `run`, `env`, and `activate`: load the binary first, and install it if missing.",
)
@click.option(
    "--update",
    "update_before_run",
    is_flag=True,
    default=False,
    help="Used by `run`, `env`, and `activate`: ensure the binary is available, then update it first.",
)
@click.option(
    "--version",
    "show_version",
    is_flag=True,
    default=False,
    help="Show the abxpkg version and available installer binaries.",
)
def cli(
    ctx: click.Context,
    install_before_run: bool,
    update_before_run: bool,
    show_version: bool,
    **shared_kwargs: Any,
) -> None:
    """Manage binaries via abxpkg binproviders."""

    ctx.ensure_object(dict)
    options = build_cli_options(None, **shared_kwargs)
    ctx.obj["group_options"] = options
    ctx.obj["install_before_run"] = install_before_run
    ctx.obj["update_before_run"] = update_before_run

    if show_version:
        for line in version_report(options):
            _echo(line)
        ctx.exit()

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command("version")
@click.argument("binary_name", required=False)
@click.pass_context
@shared_options
def version_command(
    ctx: click.Context,
    binary_name: str | None,
    **shared_kwargs: Any,
) -> None:
    """Show the package version report, or load a named binary."""

    options = get_command_options(ctx, **shared_kwargs)
    if binary_name is not None:
        options = replace(options, dry_run=False)
        run_binary_command(binary_name, action="load", options=options)
        return
    for line in version_report(options):
        _echo(line)


@cli.command("list")
@click.argument("names", nargs=-1)
@click.pass_context
@shared_options
def list_command(
    ctx: click.Context,
    names: tuple[str, ...],
    **shared_kwargs: Any,
) -> None:
    """List binaries installed under the configured library directory."""

    options = get_command_options(ctx, **shared_kwargs)
    lines = list_cached_binaries(options, names)
    if lines:
        _echo("\n".join(lines))


@cli.command("install")
@click.argument("binary_name")
@click.pass_context
@binary_override_options
@shared_options
def install_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    """Install a binary via the selected providers in order."""

    options = get_command_options(ctx, **shared_kwargs)
    run_binary_command(binary_name, action="install", options=options)


@cli.command("add", hidden=True)
@click.argument("binary_name")
@click.pass_context
@shared_options
def add_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    options = get_command_options(ctx, **shared_kwargs)
    run_binary_command(binary_name, action="install", options=options)


@cli.command("help", hidden=True)
@click.pass_context
def help_command(ctx: click.Context) -> None:
    parent = ctx.parent
    assert parent is not None
    _echo(parent.get_help())


@cli.command("clear")
@click.pass_context
@shared_options
def clear_command(ctx: click.Context, **shared_kwargs: Any) -> None:
    """Delete the configured library directory immediately."""

    options = get_command_options(ctx, **shared_kwargs)
    clear_lib_dir(options.lib_dir)


@cli.command("update")
@click.argument("binary_name")
@click.pass_context
@binary_override_options
@shared_options
def update_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    """Update a binary via the selected providers in order."""

    options = get_command_options(ctx, **shared_kwargs)
    run_binary_command(binary_name, action="update", options=options)


@cli.command("upgrade", hidden=True)
@click.argument("binary_name")
@click.pass_context
@binary_override_options
@shared_options
def upgrade_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    options = get_command_options(ctx, **shared_kwargs)
    run_binary_command(binary_name, action="update", options=options)


@cli.command("uninstall")
@click.argument("binary_name")
@click.pass_context
@binary_override_options
@shared_options
def uninstall_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    """Uninstall a binary via the selected providers in order."""

    options = get_command_options(ctx, **shared_kwargs)
    run_binary_command(binary_name, action="uninstall", options=options)


@cli.command("remove", hidden=True)
@click.argument("binary_name")
@click.pass_context
@shared_options
def remove_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    options = get_command_options(ctx, **shared_kwargs)
    run_binary_command(binary_name, action="uninstall", options=options)


@cli.command("load")
@click.argument("binary_name")
@click.pass_context
@binary_override_options
@shared_options
def load_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    """Load an already-installed binary via the selected providers in order."""

    options = get_command_options(ctx, **shared_kwargs)
    # Load never installs, so force dry_run off regardless of what the
    # user passed; the other option fields are preserved so min_version
    # etc. still apply.
    options = replace(options, dry_run=False)
    run_binary_command(binary_name, action="load", options=options)


def _run_command_impl(
    ctx: click.Context,
    **shared_kwargs: Any,
) -> None:
    command_install_before_run = bool(shared_kwargs.pop("command_install_before_run"))
    command_update_before_run = bool(shared_kwargs.pop("command_update_before_run"))
    script_mode = bool(shared_kwargs.pop("script_mode"))
    binary_name = cast(str, shared_kwargs.pop("binary_name"))
    binary_args = cast(tuple[str, ...], shared_kwargs.pop("binary_args"))

    run_options = get_command_options(ctx, **shared_kwargs)
    install_before_run = bool(ctx.obj.get("install_before_run", False)) or bool(
        command_install_before_run,
    )
    update_before_run = bool(ctx.obj.get("update_before_run", False)) or bool(
        command_update_before_run,
    )

    configure_cli_logging(debug=run_options.debug)

    runtime_binproviders: list[BinProvider] = []
    binary_options = run_options

    # --script: the OS appends the script path as binary_args[0] via the
    # shebang.  Parse its /// metadata and resolve deps before normal run.
    if script_mode:
        if not binary_args:
            _echo("abxpkg: --script requires a script path", err=True)
            ctx.exit(1)
            return

        script_path = Path(binary_args[0])
        if not script_path.is_file():
            _echo(f"abxpkg: script not found: {script_path}", err=True)
            ctx.exit(1)
            return

        meta = parse_script_metadata(script_path)
        if meta is None:
            _echo(
                f"abxpkg: no /// script metadata found in {script_path}",
                err=True,
            )
            ctx.exit(1)
            return

        # Apply [tool.abxpkg] as env vars before resolving deps.
        tool_section = meta.get("tool")
        tool_config = (
            tool_section.get("abxpkg", {}) if isinstance(tool_section, dict) else {}
        )
        for key, value in tool_config.items():
            os.environ.setdefault(key, str(value))

        # Resolve all declared dependencies and collect their runtime ENV for
        # the final script execution. Provider resolution remains hermetic:
        # only the subprocess env gets the merged dependency PATH / ENV.
        explicit_provider_selection = shared_kwargs.get(
            "binproviders",
        ) is not None or os.environ.get("ABXPKG_BINPROVIDERS") not in (None, "")
        for dep in meta.get("dependencies", []):
            if isinstance(dep, str):
                dep_name = dep
                dep_options = run_options
            elif isinstance(dep, dict):
                if "name" not in dep:
                    continue
                dep_name = dep["name"]
                dep_options = run_options
                if "binproviders" in dep:
                    dep_options = replace(
                        dep_options,
                        provider_names=dep["binproviders"],
                    )
                if "min_version" in dep:
                    dep_options = replace(dep_options, min_version=dep["min_version"])
            else:
                continue

            if dep_name == binary_name:
                if not isinstance(dep, dict):
                    continue
                if (
                    not explicit_provider_selection
                    and "binproviders" in dep
                    and binary_options.provider_names == run_options.provider_names
                ):
                    binary_options = replace(
                        binary_options,
                        provider_names=dep["binproviders"],
                    )
                replacement_kwargs: dict[str, Any] = {}
                for field_name in (
                    "min_version",
                    "postinstall_scripts",
                    "min_release_age",
                    "euid",
                    "install_timeout",
                    "version_timeout",
                ):
                    if (
                        field_name in dep
                        and getattr(binary_options, field_name) is None
                    ):
                        replacement_kwargs[field_name] = dep[field_name]
                for field_name in ("install_root", "bin_dir"):
                    if (
                        field_name in dep
                        and getattr(binary_options, field_name) is None
                        and dep[field_name] is not None
                    ):
                        replacement_kwargs[field_name] = (
                            Path(
                                dep[field_name],
                            )
                            .expanduser()
                            .resolve()
                        )
                if "overrides" in dep and dep["overrides"] is not None:
                    merged_overrides = json.loads(
                        json.dumps(dep["overrides"]),
                    )
                    if binary_options.overrides:
                        stack: list[tuple[dict[str, Any], dict[str, Any]]] = [
                            (merged_overrides, binary_options.overrides),
                        ]
                        while stack:
                            base_dict, override_dict = stack.pop()
                            for key, value in override_dict.items():
                                existing = base_dict.get(key)
                                if isinstance(existing, dict) and isinstance(
                                    value,
                                    dict,
                                ):
                                    stack.append((existing, value))
                                else:
                                    base_dict[key] = value
                    replacement_kwargs["overrides"] = merged_overrides
                dep_handler_overrides = {
                    key: dep[key]
                    for key in ("abspath", "version", "install_args", "packages")
                    if key in dep and dep[key] is not None
                }
                if dep_handler_overrides:
                    replacement_kwargs["handler_overrides"] = {
                        **dep_handler_overrides,
                        **(binary_options.handler_overrides or {}),
                    }
                if replacement_kwargs:
                    binary_options = replace(binary_options, **replacement_kwargs)
                continue

            try:
                dep_binary = build_binary(
                    dep_name,
                    dep_options,
                    dry_run=run_options.dry_run,
                )
                dep_binary = dep_binary.install(
                    dry_run=run_options.dry_run,
                    no_cache=run_options.no_cache,
                )
                if dep_binary.loaded_binprovider:
                    runtime_binproviders.append(dep_binary.loaded_binprovider)
            except ABXPkgError as err:
                _echo(
                    f"abxpkg: failed to resolve dependency {dep_name}: "
                    f"{format_error(err)}",
                    err=True,
                )
                ctx.exit(1)
                return

        # --script implies --install
        install_before_run = True

    try:
        binary, resolved_runtime_binproviders = resolve_runtime_binary(
            binary_name,
            options=binary_options,
            install_before_run=install_before_run,
            update_before_run=update_before_run,
        )
    except click.ClickException as err:
        _echo(str(err), err=True)
        ctx.exit(1)
        return
    if not script_mode:
        runtime_binproviders = resolved_runtime_binproviders

    if run_options.dry_run:
        # Provider exec honors dry_run and returns a no-op CompletedProcess;
        # keep the behavior consistent here so nothing is actually run.
        ctx.exit(0)
        return

    if not binary.is_valid:
        _echo(
            f"abxpkg: {binary_name}: binary could not be loaded",
            err=True,
        )
        ctx.exit(1)
        return

    # binary.is_valid guarantees both fields are set; narrow for pyright.
    assert binary.loaded_binprovider is not None
    assert binary.loaded_abspath is not None
    exec_kwargs: dict[str, Any] = {"capture_output": False}
    other_runtime_binproviders = get_runtime_exec_providers(
        binary,
        runtime_binproviders,
    )
    if other_runtime_binproviders:
        exec_kwargs["env"] = BinProvider.build_exec_env(
            providers=other_runtime_binproviders,
            base_env=os.environ.copy(),
        )
    proc = binary.loaded_binprovider.exec(
        bin_name=binary.loaded_abspath,
        cmd=list(binary_args),
        **exec_kwargs,
    )
    ctx.exit(proc.returncode)


@cli.command(
    "run",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "allow_interspersed_args": False,
        "help_option_names": [],
    },
)
@binary_override_options
@shared_options
@click.option(
    "--install",
    "command_install_before_run",
    is_flag=True,
    default=False,
    help="Load the binary first, and install it if missing.",
)
@click.option(
    "--update",
    "command_update_before_run",
    is_flag=True,
    default=False,
    help="Ensure the binary is available, then update it before executing it.",
)
@click.option(
    "--script",
    "script_mode",
    is_flag=True,
    default=False,
    help="Parse inline /// script metadata from the script file and resolve dependencies before running.",
)
@click.argument("binary_name")
@click.argument("binary_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def run_command(
    ctx: click.Context,
    **shared_kwargs: Any,
) -> None:
    """Run an installed binary, passing all remaining arguments through to it.

    The full shared option surface is accepted on ``run`` itself before the
    binary name. Everything after the binary name is forwarded verbatim to
    the underlying binary's argv.
    """

    _run_command_impl(ctx, **shared_kwargs)


@cli.command(
    "exec",
    hidden=True,
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "allow_interspersed_args": False,
        "help_option_names": [],
    },
)
@binary_override_options
@shared_options
@click.option(
    "--install",
    "command_install_before_run",
    is_flag=True,
    default=False,
)
@click.option(
    "--update",
    "command_update_before_run",
    is_flag=True,
    default=False,
)
@click.option(
    "--script",
    "script_mode",
    is_flag=True,
    default=False,
)
@click.argument("binary_name")
@click.argument("binary_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def exec_command(
    ctx: click.Context,
    **shared_kwargs: Any,
) -> None:
    _run_command_impl(ctx, **shared_kwargs)


@cli.command("env")
@shared_options
@click.option(
    "--install",
    "command_install_before_run",
    is_flag=True,
    default=False,
    help="Load each binary first, and install it if missing.",
)
@click.option(
    "--update",
    "command_update_before_run",
    is_flag=True,
    default=False,
    help="Ensure each binary is available, then update it before emitting the env.",
)
@click.argument("binary_names", nargs=-1)
@click.pass_context
def env_command(
    ctx: click.Context,
    binary_names: tuple[str, ...],
    **shared_kwargs: Any,
) -> None:
    """Emit dotenv-style KEY=value lines for the selected binaries or providers."""

    command_install_before_run = bool(shared_kwargs.pop("command_install_before_run"))
    command_update_before_run = bool(shared_kwargs.pop("command_update_before_run"))
    options = get_command_options(ctx, **shared_kwargs)
    install_before_run = bool(ctx.obj.get("install_before_run", False)) or bool(
        command_install_before_run,
    )
    update_before_run = bool(ctx.obj.get("update_before_run", False)) or bool(
        command_update_before_run,
    )
    configure_cli_logging(debug=options.debug)

    base_env = os.environ.copy()
    final_env = build_command_exec_env(
        binary_names,
        options=options,
        install_before_run=install_before_run,
        update_before_run=update_before_run,
        base_env=base_env,
    )
    lines = render_env_assignment_lines(base_env, final_env)
    if lines:
        _echo("\n".join(lines))


@cli.command("activate")
@shared_options
@click.option(
    "--install",
    "command_install_before_run",
    is_flag=True,
    default=False,
    help="Load each binary first, and install it if missing.",
)
@click.option(
    "--update",
    "command_update_before_run",
    is_flag=True,
    default=False,
    help="Ensure each binary is available, then update it before emitting the activation script.",
)
@click.option(
    "--bash",
    "activate_bash",
    is_flag=True,
    default=False,
    help="Emit bash-compatible export lines (default).",
)
@click.option(
    "--zsh",
    "activate_zsh",
    is_flag=True,
    default=False,
    help="Emit zsh-compatible export lines.",
)
@click.option(
    "--fish",
    "activate_fish",
    is_flag=True,
    default=False,
    help="Emit fish-compatible set -x lines.",
)
@click.argument("binary_names", nargs=-1)
@click.pass_context
def activate_command(
    ctx: click.Context,
    binary_names: tuple[str, ...],
    **shared_kwargs: Any,
) -> None:
    """Emit shell commands that activate the selected abxpkg runtime env."""

    command_install_before_run = bool(shared_kwargs.pop("command_install_before_run"))
    command_update_before_run = bool(shared_kwargs.pop("command_update_before_run"))
    activate_bash = bool(shared_kwargs.pop("activate_bash"))
    activate_zsh = bool(shared_kwargs.pop("activate_zsh"))
    activate_fish = bool(shared_kwargs.pop("activate_fish"))
    options = get_command_options(ctx, **shared_kwargs)
    install_before_run = bool(ctx.obj.get("install_before_run", False)) or bool(
        command_install_before_run,
    )
    update_before_run = bool(ctx.obj.get("update_before_run", False)) or bool(
        command_update_before_run,
    )
    configure_cli_logging(debug=options.debug)
    shell = parse_activate_shell(
        bash=activate_bash,
        zsh=activate_zsh,
        fish=activate_fish,
    )

    base_env = os.environ.copy()
    final_env = build_command_exec_env(
        binary_names,
        options=options,
        install_before_run=install_before_run,
        update_before_run=update_before_run,
        base_env=base_env,
    )
    lines = [
        render_activate_comment(shell=shell, binary_names=binary_names),
        *render_activate_lines(base_env, final_env, shell=shell),
    ]
    _echo("\n".join(lines))


# Bool flags that should auto-set to True when passed bare (e.g. `--dry-run`
# with no ``=VALUE``). Pre-processing in main() / abx_main() rewrites bare
# occurrences to ``--flag=True`` so a single click string option can handle
# both the bare and the value form. Callers pass ``--dry-run=False`` or
# ``--dry-run=None`` to override the auto-True semantics.
_BARE_TRUE_BOOL_FLAGS = frozenset(
    {"--dry-run", "--debug", "--postinstall-scripts", "--no-cache"},
)

_RUN_LIKE_COMMANDS = frozenset({"run", "exec"})

_BINARY_OVERRIDE_FLAGS = frozenset({"--abspath", "--install-args", "--packages"})
_BINARY_OVERRIDE_COMMANDS = frozenset(
    {"install", "update", "upgrade", "uninstall", "load", *tuple(_RUN_LIKE_COMMANDS)},
)


def _normalize_binary_override_option_order(argv: list[str]) -> list[str]:
    """Allow binary override flags before or after the subcommand.

    Click only knows ``--abspath`` / ``--install-args`` / ``--packages`` on
    the concrete binary subcommands, so a call like ``abxpkg --install-args
    '["black"]' upgrade black`` would normally fail at the group level before
    ``upgrade`` gets a chance to parse it. Hoist those flags across the first
    binary-managing subcommand so both placements normalize to the same argv.
    """

    group_opts_with_values = frozenset(
        opt
        for param in cli.params
        if isinstance(param, click.Option) and not param.is_flag
        for opt in param.opts
        if opt.startswith("--") and opt not in _BARE_TRUE_BOOL_FLAGS
    )

    prefix: list[str] = []
    hoisted: list[str] = []
    idx = 0
    while idx < len(argv):
        tok = argv[idx]
        if tok == "--":
            return argv
        if tok.startswith("--") and "=" in tok:
            opt_name = tok.split("=", 1)[0]
            if opt_name in _BINARY_OVERRIDE_FLAGS:
                hoisted.append(tok)
            else:
                prefix.append(tok)
            idx += 1
            continue
        if tok in _BINARY_OVERRIDE_FLAGS:
            hoisted.append(tok)
            if idx + 1 < len(argv):
                hoisted.append(argv[idx + 1])
                idx += 2
            else:
                idx += 1
            continue
        if tok in group_opts_with_values:
            prefix.append(tok)
            if idx + 1 < len(argv):
                prefix.append(argv[idx + 1])
                idx += 2
            else:
                idx += 1
            continue
        if tok.startswith("-") and tok != "-":
            prefix.append(tok)
            idx += 1
            continue
        if tok not in _BINARY_OVERRIDE_COMMANDS or not hoisted:
            return argv
        return [*prefix, tok, *hoisted, *argv[idx + 1 :]]
    return argv


def _expand_bare_bool_flags(argv: list[str]) -> list[str]:
    """Translate bare bool flags (``--dry-run``) into their value form
    (``--dry-run=True``) so a single click string option can handle both.

    Crucially, the rewrite stops at the run-like subcommands: every token
    after ``run`` / ``exec`` is a child binary arg that must be forwarded verbatim
    (a bare ``rsync --dry-run /src /dst`` must stay ``--dry-run``, not
    ``--dry-run=True``, because many tools reject the value form).
    """

    out: list[str] = []
    past_run = False
    skip_next = False
    for tok in argv:
        if past_run:
            out.append(tok)
        elif skip_next:
            # This token is the value of the preceding option (e.g.
            # `--lib run`), not the `run` subcommand itself.
            skip_next = False
            out.append(tok)
        elif tok in _ABXPKG_GROUP_OPTS_WITH_VALUES:
            skip_next = True
            out.append(tok)
        elif tok in _RUN_LIKE_COMMANDS:
            past_run = True
            out.append(tok)
        elif tok in _BARE_TRUE_BOOL_FLAGS:
            out.append(f"{tok}=True")
        else:
            out.append(tok)
    return out


def main() -> None:
    cli(_expand_bare_bool_flags(_normalize_binary_override_option_order(sys.argv[1:])))


# ---------------------------------------------------------------------------
# `abx` — thin alias for `abxpkg --install run ...`
# ---------------------------------------------------------------------------

# Group-level options that consume a following value (e.g. `--lib PATH`,
# `--binproviders LIST`). Derived at import time by introspecting the
# click group's own option definitions — no hardcoding — so any option
# added later via @shared_options automatically joins this set. Used by
# _split_abx_argv to know when to pull an extra token into the
# "pre-package-name" prefix; options written as `--name=value` never hit
# this code path (they're handled by the `"=" in tok` branch).
# Bare-bool-flags are click string options under the hood, but they're
# used as bare flags (``--dry-run`` = True). Exclude them so the splitter
# doesn't try to consume the next token as a value.
_ABXPKG_GROUP_OPTS_WITH_VALUES = frozenset(
    opt
    for param in cli.params
    if isinstance(param, click.Option) and not param.is_flag
    for opt in param.opts
    if opt.startswith("--") and opt not in _BARE_TRUE_BOOL_FLAGS
) | frozenset({"--abspath", "--install-args", "--packages"})

_ABX_USAGE = (
    "Usage: abx [OPTIONS] BINARY_NAME [BINARY_ARGS]...\n"
    "\n"
    "Install (if needed) and run a package-installed binary.\n"
    "Equivalent to `abxpkg run --install [OPTIONS] BINARY_NAME [BINARY_ARGS]`.\n"
    "\n"
    "Options (forwarded to abxpkg): --lib, --binproviders, --dry-run,\n"
    "--debug, "
    "--no-cache, "
    "--update, --version, --help, --abspath, --install-args, --packages.\n"
)


def _split_abx_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split ``argv`` around the first positional (binary name) token.

    Everything up to (and not including) the first non-option token is
    treated as `abxpkg` group options and returned as ``pre``. The binary
    name and all following tokens are returned verbatim as ``rest``.

    Options that take a separate value (`--lib PATH`, `--binproviders LIST`)
    are handled so the value token is kept with its option instead of being
    mistaken for the binary name.
    """
    pre: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            # POSIX option terminator: everything after `--` is the binary
            # name and its arguments, never abxpkg options. Consume the
            # `--` so we don't end up injecting `--install run` *after* a
            # stray `--` that would otherwise force click to treat every
            # following token as a positional group argument.
            return pre, argv[i + 1 :]
        if tok.startswith("--") and "=" in tok:
            pre.append(tok)
            i += 1
            continue
        if tok in _ABXPKG_GROUP_OPTS_WITH_VALUES:
            pre.append(tok)
            if i + 1 < len(argv):
                pre.append(argv[i + 1])
                i += 2
            else:
                i += 1
            continue
        if tok.startswith("-") and tok != "-":
            pre.append(tok)
            i += 1
            continue
        # First non-option token: this is the binary name.
        return pre, argv[i:]
    return pre, []


def abx_main() -> None:
    """Console-script entrypoint for the thin ``abx`` alias.

    Rewrites ``abx [OPTS] BINARY [ARGS]`` into
    ``abxpkg run --install [OPTS] BINARY [ARGS]`` and hands it off to the
    existing click group. Keeps us from redefining any of the rich-click
    surface area — every option is still documented and parsed exactly
    once, by ``abxpkg`` itself.
    """
    argv = list(sys.argv[1:])
    pre, rest = _split_abx_argv(argv)
    # Expand bare bool flags only in the pre-binary-name slice; rest is
    # the child binary's argv and must be forwarded verbatim.
    pre = _expand_bare_bool_flags(pre)
    pre = ["--update" if tok == "--upgrade" else tok for tok in pre]

    if not rest:
        # No binary name given. Forward info-only flags so `abx --version`
        # and `abx --help` still do something useful; otherwise print our
        # own usage to stderr and exit 2 like click would.
        if "--version" in pre:
            _echo(get_package_version())
            return
        if any(flag in pre for flag in ("--help", "-h")):
            cli(pre)
            return
        _echo(_ABX_USAGE, err=True)
        sys.exit(2)

    # --update already implies "install if missing", so adding --install alongside
    # it is a no-op; always injecting --install keeps this wrapper stateless.
    cli(["run", "--install", *pre, *rest])


__all__ = [
    "CliOptions",
    "abx_main",
    "build_binary",
    "build_providers",
    "cli",
    "get_package_version",
    "is_interactive_tty",
    "main",
    "parse_provider_names",
    "parse_script_metadata",
    "resolve_debug",
    "resolve_dry_run",
    "resolve_no_cache",
    "resolve_lib_dir",
    "version_report",
]

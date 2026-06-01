from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ConfigDict, Field

from . import DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME, Binary, BinProvider
from .exceptions import BinaryLoadError
from .semver import SemVer

try:
    from abxbus import (
        BaseEvent,
        EventBus,
        EventConcurrencyMode,
        EventHandlerConcurrencyMode,
    )
    from abxbus.retry import retry
except (
    ModuleNotFoundError
) as err:  # pragma: no cover - exercised only without optional peer dependency
    raise ImportError(
        "abxpkg.binary_service requires the optional peer dependency abxbus. "
        "Install abxbus alongside abxpkg to use BinaryService.",
    ) from err


class BinaryRequestEvent(BaseEvent):
    """Request that abxpkg resolve or install one binary."""

    model_config = ConfigDict(extra="forbid")

    event_concurrency: EventConcurrencyMode | None = EventConcurrencyMode.PARALLEL
    event_handler_concurrency: EventHandlerConcurrencyMode | None = (
        EventHandlerConcurrencyMode.SERIAL
    )
    name: str = Field(min_length=1)
    description: str = ""
    min_version: str | None = None
    postinstall_scripts: bool | None = None
    min_release_age: float | None = None
    binproviders: str | list[str] = "env"
    overrides: dict[str, Any] | None = None
    auto_install: bool | None = None
    lib_dir: Path | None = None
    install_root: Path | None = None
    bin_dir: Path | None = None
    euid: int | None = None
    dry_run: bool | None = None
    no_cache: bool | None = None
    install_timeout: int | None = None
    version_timeout: int | None = None
    base_env: dict[str, str] | None = None
    extra_env: dict[str, str] | None = None
    event_timeout: float | None = 300.0


class BinaryEvent(BaseEvent):
    """Resolved binary metadata emitted after a successful request."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    description: str = ""
    abspath: str = Field(min_length=1)
    version: str = ""
    sha256: str = ""
    mtime: int | None = None
    euid: int | None = None
    binproviders: str = ""
    binprovider: str = ""
    overrides: dict[str, Any] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    event_timeout: float | None = 10.0


class BinaryService:
    """abxbus service that resolves BinaryRequestEvent using native abxpkg providers."""

    LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [BinaryRequestEvent]
    EMITS: ClassVar[list[type[BaseEvent]]] = [BinaryEvent]

    def __init__(
        self,
        bus: EventBus,
        *,
        auto_install: bool = True,
        provider_names: str | list[str] | None = None,
        description: str = "",
        min_version: str | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        overrides: Mapping[str, Any] | None = None,
        lib_dir: Path | None = None,
        install_root: Path | None = None,
        bin_dir: Path | None = None,
        euid: int | None = None,
        dry_run: bool = False,
        no_cache: bool = False,
        install_timeout: int | None = None,
        version_timeout: int | None = None,
        base_env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
    ):
        self.bus = bus
        self.auto_install = auto_install
        self.provider_names = provider_names
        self.description = description
        self.min_version = min_version
        self.postinstall_scripts = postinstall_scripts
        self.min_release_age = min_release_age
        self.overrides = dict(overrides or {})
        self.lib_dir = lib_dir
        self.install_root = install_root
        self.bin_dir = bin_dir
        self.euid = euid
        self.dry_run = dry_run
        self.no_cache = no_cache
        self.install_timeout = install_timeout
        self.version_timeout = version_timeout
        self.base_env = base_env
        self.extra_env = extra_env
        self.bus.on(BinaryRequestEvent, self.on_BinaryRequestEvent)

    async def on_BinaryRequestEvent(self, event: BinaryRequestEvent) -> str | None:
        existing = await self._find_binary_event(event)
        if existing is not None:
            return existing.abspath

        try:
            loaded = await asyncio.to_thread(self._load, event)
        except BinaryLoadError:
            loaded = None
        if loaded is not None and loaded.loaded_abspath:
            return await self._emit_binary_event(event, loaded)

        auto_install = (
            self.auto_install if event.auto_install is None else event.auto_install
        )
        if not auto_install:
            return None

        existing = await self._find_binary_event(event)
        if existing is not None:
            return existing.abspath
        installed = await self._install_or_find(event)
        if isinstance(installed, BinaryEvent):
            return installed.abspath
        return await self._emit_binary_event(event, installed)

    async def _find_binary_event(
        self,
        event: BinaryRequestEvent,
    ) -> BinaryEvent | None:
        existing = await self.bus.find(
            BinaryEvent,
            child_of=event,
            past=True,
            future=False,
            name=event.name,
            where=lambda candidate: bool(candidate.abspath),
        )
        return existing if isinstance(existing, BinaryEvent) else None

    def _load(self, event: BinaryRequestEvent) -> Binary:
        return self._binary_for_event(event).load(
            no_cache=self._no_cache_for_event(event),
        )

    @retry(
        max_attempts=1,
        semaphore_limit=1,
        semaphore_scope="multiprocess",
        semaphore_name=lambda self, event: self._install_semaphore_name(event),
        semaphore_timeout=300,
        semaphore_lax=False,
    )
    async def _install_or_find(self, event: BinaryRequestEvent) -> Binary | BinaryEvent:
        existing = await self._find_binary_event(event)
        if existing is not None:
            return existing
        return await asyncio.to_thread(
            self._binary_for_event(event).install,
            no_cache=self._no_cache_for_event(event),
            dry_run=self._dry_run_for_event(event),
            postinstall_scripts=self._postinstall_scripts_for_event(event),
            min_release_age=self._min_release_age_for_event(event),
        )

    def _install_semaphore_name(self, event: BinaryRequestEvent) -> str:
        roots: list[str] = []
        for provider in self._providers_for_event(event):
            root = provider.install_root or provider.bin_dir
            roots.append(
                str(Path(root).expanduser().resolve())
                if root is not None
                else provider.name,
            )
        return "abxpkg:install:" + "|".join(roots)

    def _binary_for_event(self, event: BinaryRequestEvent) -> Binary:
        return Binary(
            name=event.name,
            description=self._description_for_event(event),
            min_version=(
                SemVer(min_version)
                if (min_version := self._min_version_for_event(event))
                else None
            ),
            postinstall_scripts=self._postinstall_scripts_for_event(event),
            min_release_age=self._min_release_age_for_event(event),
            binproviders=self._providers_for_event(event),
            overrides=self._overrides_for_event(event),
        )

    def _providers_for_event(self, event: BinaryRequestEvent) -> list[BinProvider]:
        names = self._provider_names(event.binproviders)
        providers: list[BinProvider] = []
        for name in names:
            install_root = (
                self.install_root if event.install_root is None else event.install_root
            )
            lib_dir = self.lib_dir if event.lib_dir is None else event.lib_dir
            kwargs: dict[str, Any] = {"dry_run": self._dry_run_for_event(event)}
            for key, value in (
                ("install_root", install_root),
                ("bin_dir", self.bin_dir if event.bin_dir is None else event.bin_dir),
                ("euid", self.euid if event.euid is None else event.euid),
                (
                    "install_timeout",
                    self.install_timeout
                    if event.install_timeout is None
                    else event.install_timeout,
                ),
                (
                    "version_timeout",
                    self.version_timeout
                    if event.version_timeout is None
                    else event.version_timeout,
                ),
            ):
                if value is not None:
                    kwargs[key] = value
            if install_root is None and lib_dir is not None:
                kwargs["install_root"] = lib_dir / name
            providers.append(PROVIDER_CLASS_BY_NAME[name](**kwargs))
        return providers

    def _dry_run_for_event(self, event: BinaryRequestEvent) -> bool:
        return self.dry_run if event.dry_run is None else event.dry_run

    def _no_cache_for_event(self, event: BinaryRequestEvent) -> bool:
        return self.no_cache if event.no_cache is None else event.no_cache

    def _description_for_event(self, event: BinaryRequestEvent) -> str:
        return event.description or self.description

    def _min_version_for_event(self, event: BinaryRequestEvent) -> str | None:
        return self.min_version if event.min_version is None else event.min_version

    def _postinstall_scripts_for_event(
        self,
        event: BinaryRequestEvent,
    ) -> bool | None:
        return (
            self.postinstall_scripts
            if event.postinstall_scripts is None
            else event.postinstall_scripts
        )

    def _min_release_age_for_event(self, event: BinaryRequestEvent) -> float | None:
        return (
            self.min_release_age
            if event.min_release_age is None
            else event.min_release_age
        )

    def _overrides_for_event(self, event: BinaryRequestEvent) -> dict[str, Any]:
        return dict(self.overrides if event.overrides is None else event.overrides)

    def _base_env_for_event(
        self,
        event: BinaryRequestEvent,
    ) -> Mapping[str, str] | None:
        return self.base_env if event.base_env is None else event.base_env

    def _extra_env_for_event(self, event: BinaryRequestEvent) -> dict[str, str]:
        return {
            **dict(self.extra_env or {}),
            **dict(event.extra_env or {}),
        }

    def _provider_names(self, requested: str | list[str] | None) -> list[str]:
        names: list[str] = []
        raw_requested = (
            requested if requested not in (None, "", []) else self.provider_names
        )
        if isinstance(raw_requested, str):
            if raw_requested.strip() == "*":
                raw_names = list(DEFAULT_PROVIDER_NAMES)
            else:
                raw_names = [part.strip() for part in raw_requested.split(",")]
        elif raw_requested:
            raw_names = [str(part).strip() for part in raw_requested]
        else:
            raw_names = list(DEFAULT_PROVIDER_NAMES)

        for name in raw_names:
            if not name or name in names:
                continue
            if name not in PROVIDER_CLASS_BY_NAME:
                valid = ", ".join(PROVIDER_CLASS_BY_NAME)
                raise ValueError(
                    f"Unknown abxpkg provider {name!r}. Valid providers: {valid}",
                )
            names.append(name)
        if not names:
            raise ValueError(
                "BinaryRequestEvent.binproviders did not include any providers",
            )
        return names

    async def _emit_binary_event(
        self,
        request: BinaryRequestEvent,
        binary: Binary,
    ) -> str:
        if not binary.loaded_abspath:
            raise ValueError(f"{request.name} did not resolve to an abspath")
        provider = binary.loaded_binprovider
        provider_name = provider.name if provider is not None else ""
        env = (
            BinProvider.build_exec_env(
                providers=[provider],
                base_env=self._base_env_for_event(request),
                extra_env=self._extra_env_for_event(request),
            )
            if provider is not None
            else BinProvider.build_exec_env(
                providers=[],
                base_env=self._base_env_for_event(request),
                extra_env=self._extra_env_for_event(request),
            )
        )
        event = BinaryEvent(
            name=request.name,
            description=self._description_for_event(request),
            abspath=str(binary.loaded_abspath),
            version=str(binary.loaded_version or ""),
            sha256=str(binary.loaded_sha256 or ""),
            mtime=binary.loaded_mtime,
            euid=binary.loaded_euid,
            binproviders=",".join(self._provider_names(request.binproviders)),
            binprovider=provider_name,
            overrides=self._overrides_for_event(request),
            env=env,
        )
        await request.emit(event).now()  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore[unresolved-attribute]
        return event.abspath

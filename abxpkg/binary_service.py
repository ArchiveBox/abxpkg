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
    from abxbus import BaseEvent, EventBus, EventConcurrencyMode
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

    model_config = ConfigDict(extra="allow")

    event_concurrency: EventConcurrencyMode | None = EventConcurrencyMode.PARALLEL
    name: str = Field(min_length=1)
    plugin_name: str = ""
    hook_name: str = ""
    output_dir: str = ""
    min_version: str | None = None
    postinstall_scripts: bool | None = None
    min_release_age: float | None = None
    binary_id: str = ""
    machine_id: str = ""
    binproviders: str | list[str] = "env"
    overrides: dict[str, Any] | None = None
    install_cache_key: str = ""
    install_cache_hit: bool = False
    event_timeout: float | None = 300.0


class BinaryEvent(BaseEvent):
    """Resolved binary metadata emitted after a successful request."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    plugin_name: str = ""
    hook_name: str = ""
    abspath: str = Field(min_length=1)
    version: str = ""
    sha256: str = ""
    binproviders: str = ""
    binprovider: str = ""
    overrides: dict[str, Any] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    binary_id: str = ""
    machine_id: str = ""
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
        install_root: Path | None = None,
        bin_dir: Path | None = None,
        euid: int | None = None,
        dry_run: bool = False,
        no_cache: bool = False,
        install_timeout: int | None = None,
        version_timeout: int | None = None,
        base_env: Mapping[str, str] | None = None,
    ):
        self.bus = bus
        self.auto_install = auto_install
        self.provider_names = provider_names
        self.install_root = install_root
        self.bin_dir = bin_dir
        self.euid = euid
        self.dry_run = dry_run
        self.no_cache = no_cache
        self.install_timeout = install_timeout
        self.version_timeout = version_timeout
        self.base_env = base_env
        self.bus.on(BinaryRequestEvent, self.on_BinaryRequestEvent)

    async def on_BinaryRequestEvent(self, event: BinaryRequestEvent) -> str | None:
        existing = await self.bus.find(
            BinaryEvent,
            child_of=event,
            past=True,
            future=False,
            name=event.name,
            where=lambda candidate: bool(candidate.abspath),
        )
        if isinstance(existing, BinaryEvent):
            return existing.abspath

        try:
            loaded = await asyncio.to_thread(self._load, event)
        except BinaryLoadError:
            loaded = None
        if loaded is not None and loaded.loaded_abspath:
            return await self._emit_binary_event(event, loaded)

        if not self.auto_install:
            return None

        existing = await self.bus.find(
            BinaryEvent,
            child_of=event,
            past=True,
            future=False,
            name=event.name,
            where=lambda candidate: bool(candidate.abspath),
        )
        if isinstance(existing, BinaryEvent):
            return existing.abspath
        installed = await asyncio.to_thread(self._install, event)
        return await self._emit_binary_event(event, installed)

    def _load(self, event: BinaryRequestEvent) -> Binary:
        return self._binary_for_event(event).load(no_cache=self.no_cache)

    @retry(
        max_attempts=1,
        semaphore_limit=1,
        semaphore_scope="multiprocess",
        semaphore_name=lambda self, event: self._install_semaphore_name(event),
        semaphore_timeout=300,
        semaphore_lax=False,
    )
    def _install(self, event: BinaryRequestEvent) -> Binary:
        return self._binary_for_event(event).install(
            no_cache=self.no_cache,
            dry_run=self.dry_run,
            postinstall_scripts=event.postinstall_scripts,
            min_release_age=event.min_release_age,
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
            min_version=SemVer(event.min_version) if event.min_version else None,
            postinstall_scripts=event.postinstall_scripts,
            min_release_age=event.min_release_age,
            binproviders=self._providers_for_event(event),
            overrides=event.overrides or {},
        )

    def _providers_for_event(self, event: BinaryRequestEvent) -> list[BinProvider]:
        names = self._provider_names(event.binproviders)
        kwargs: dict[str, Any] = {"dry_run": self.dry_run}
        for key, value in (
            ("install_root", self.install_root),
            ("bin_dir", self.bin_dir),
            ("euid", self.euid),
            ("install_timeout", self.install_timeout),
            ("version_timeout", self.version_timeout),
        ):
            if value is not None:
                kwargs[key] = value
        return [PROVIDER_CLASS_BY_NAME[name](**kwargs) for name in names]

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
            BinProvider.build_exec_env(providers=[provider], base_env=self.base_env)
            if provider is not None
            else dict(self.base_env or {})
        )
        event = BinaryEvent(
            name=request.name,
            plugin_name=request.plugin_name,
            hook_name=request.hook_name,
            abspath=str(binary.loaded_abspath),
            version=str(binary.loaded_version or ""),
            sha256=str(binary.loaded_sha256 or ""),
            binproviders=",".join(self._provider_names(request.binproviders)),
            binprovider=provider_name,
            overrides=request.overrides,
            env=env,
            binary_id=request.binary_id,
            machine_id=request.machine_id,
        )
        await request.emit(event).now()
        return event.abspath

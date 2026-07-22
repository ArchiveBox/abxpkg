import asyncio
from pathlib import Path
from typing import Any

import pytest
import abxbus

from abxpkg import Binary, EnvProvider
from abxpkg.semver import SemVer


def test_binary_request_events_allow_parallel_scheduling_by_default(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import BinaryRequestEvent, BinaryService

    event = BinaryRequestEvent(name="python")
    service = BinaryService(
        abxbus.EventBus(name="test_binary_request_events_allow_parallel_scheduling"),
        install_root=tmp_path / "shared-root",
    )
    other_service = BinaryService(
        abxbus.EventBus(
            name="test_binary_request_events_allow_parallel_scheduling_other",
        ),
        install_root=tmp_path / "other-root",
    )

    assert event.event_concurrency == abxbus.EventConcurrencyMode.PARALLEL
    assert event.event_handler_concurrency == abxbus.EventHandlerConcurrencyMode.SERIAL
    assert "install_args" not in BinaryRequestEvent.model_fields
    with pytest.raises(Exception, match="Extra inputs are not permitted"):
        BinaryRequestEvent.model_validate(
            {"name": "tool", "install_args": ["tool-package"]},
        )
    for app_field in (
        "plugin_name",
        "hook_name",
        "output_dir",
        "binary_id",
        "machine_id",
        "install_cache_key",
        "install_cache_hit",
    ):
        assert app_field not in BinaryRequestEvent.model_fields
        with pytest.raises(Exception, match="Extra inputs are not permitted"):
            BinaryRequestEvent.model_validate(
                {"name": "tool", app_field: "app-value"},
            )
    assert service._install_semaphore_name(event) == service._install_semaphore_name(
        event,
    )
    assert service._install_semaphore_name(
        event,
    ) != other_service._install_semaphore_name(
        event,
    )
    assert service._provider_names(["pip", "npm", "pip"]) == ["pip", "npm"]

    lib_service = BinaryService(
        abxbus.EventBus(name="test_binary_request_events_lib_dir"),
        lib_dir=tmp_path / "lib",
    )
    lib_event = BinaryRequestEvent(name="tool", binproviders="pip,npm")
    lib_roots = [
        provider.install_root
        for provider in lib_service._providers_for_event(lib_event)
    ]
    assert lib_roots == [tmp_path / "lib" / "pip", tmp_path / "lib" / "npm"]

    override_event = BinaryRequestEvent(
        name="tool",
        description="Tool binary",
        binproviders=["pip"],
        lib_dir=tmp_path / "event-lib",
        bin_dir=tmp_path / "event-bin",
        euid=123,
        dry_run=True,
        no_cache=True,
        install_timeout=3,
        version_timeout=4,
    )
    override_provider = lib_service._providers_for_event(override_event)[0]
    assert override_provider.install_root == tmp_path / "event-lib" / "pip"
    assert override_provider.bin_dir == tmp_path / "event-bin"
    assert override_provider.euid == 123
    assert override_provider.dry_run is True
    assert override_provider.install_timeout == 3
    assert override_provider.version_timeout == 4
    assert lib_service._no_cache_for_event(override_event) is True
    assert lib_service._binary_for_event(override_event).description == "Tool binary"

    explicit_root_event = BinaryRequestEvent(
        name="tool",
        binproviders="pip",
        lib_dir=tmp_path / "ignored-lib",
        install_root=tmp_path / "explicit-root",
    )
    explicit_provider = lib_service._providers_for_event(explicit_root_event)[0]
    assert explicit_provider.install_root == tmp_path / "explicit-root"

    defaulted_service = BinaryService(
        abxbus.EventBus(name="test_binary_request_events_binary_defaults"),
        description="Default description",
        min_version="1.2.3",
        postinstall_scripts=False,
        min_release_age=7,
        overrides={"pip": {"install_args": ["default-package"]}},
        extra_env={"DEFAULT_EXTRA_ENV": "default"},
    )
    defaulted_event = BinaryRequestEvent(name="tool", binproviders="pip")
    defaulted_binary = defaulted_service._binary_for_event(defaulted_event)
    assert defaulted_binary.description == "Default description"
    assert defaulted_binary.min_version == SemVer("1.2.3")
    assert defaulted_binary.postinstall_scripts is False
    assert defaulted_binary.min_release_age == 7
    assert defaulted_binary.overrides == {
        "pip": {"install_args": ["default-package"]},
    }
    assert defaulted_service._extra_env_for_event(defaulted_event) == {
        "DEFAULT_EXTRA_ENV": "default",
    }

    event_overrides = BinaryRequestEvent(
        name="tool",
        description="Event description",
        binproviders="pip",
        min_version="2.0.0",
        postinstall_scripts=True,
        min_release_age=3,
        overrides={"pip": {"install_args": ["event-package"]}},
        extra_env={"DEFAULT_EXTRA_ENV": "event", "EVENT_EXTRA_ENV": "event"},
    )
    event_binary = defaulted_service._binary_for_event(event_overrides)
    assert event_binary.description == "Event description"
    assert event_binary.min_version == SemVer("2.0.0")
    assert event_binary.postinstall_scripts is True
    assert event_binary.min_release_age == 3
    assert event_binary.overrides == {
        "pip": {"install_args": ["event-package"]},
    }
    assert defaulted_service._extra_env_for_event(event_overrides) == {
        "DEFAULT_EXTRA_ENV": "event",
        "EVENT_EXTRA_ENV": "event",
    }

    context_event = BinaryRequestEvent(
        name="tool",
        extra_context={
            "plugin_name": "example",
            "binary_id": "binary-123",
            "nested": {"key": "value"},
        },
    )
    assert context_event.extra_context == {
        "plugin_name": "example",
        "binary_id": "binary-123",
        "nested": {"key": "value"},
    }


class _MemoryBinaryCacheBackend:
    def __init__(self, binary: Binary | None = None):
        self.binary = binary
        self.gets: list[Any] = []
        self.sets: list[tuple[Any, Binary]] = []
        self.invalidations: list[tuple[Any, Binary, str]] = []

    async def get(self, request: Any) -> Binary | None:
        self.gets.append(request)
        return self.binary

    async def set(self, request: Any, binary: Binary) -> None:
        self.sets.append((request, binary.model_copy(deep=True)))
        self.binary = binary.model_copy(deep=True)

    async def invalidate(self, request: Any, binary: Binary, reason: str) -> None:
        self.invalidations.append((request, binary, reason))
        self.binary = None


def _real_python_binary(lib_dir: Path) -> Binary:
    provider = EnvProvider(install_root=lib_dir / "env")
    binary = Binary(name="python", binproviders=[provider]).load(no_cache=True)
    assert binary.loaded_abspath is not None
    return binary


def test_binary_cache_service_emits_cached_binary_before_resolver(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import (
        BinaryCacheService,
        BinaryEvent,
        BinaryRequestEvent,
    )

    cached_binary = _real_python_binary(tmp_path)
    cached_path = cached_binary.loaded_abspath
    assert cached_path is not None
    cached_binary.env = {"CACHED_ENV": "1"}
    backend = _MemoryBinaryCacheBackend(cached_binary)

    async def run() -> tuple[Any, BinaryEvent, list[Any]]:
        from abxpkg.binary_service import BinaryService

        bus = abxbus.EventBus(name="test_binary_cache_service_hit")
        BinaryCacheService(bus, backend=backend)
        BinaryService(bus, auto_install=False)

        request = await bus.emit(
            BinaryRequestEvent(
                name="python",
                binproviders="env",
                extra_context={
                    "plugin_name": "cache-plugin",
                    "nested": {"key": "value"},
                },
            ),
        ).now()
        event = await bus.find(
            BinaryEvent,
            child_of=request,
            past=True,
            future=False,
            name="python",
        )
        assert isinstance(event, BinaryEvent)
        return request, event, await request.event_results_list()

    request, event, results = asyncio.run(run())

    assert results == [str(cached_path), str(cached_path)]
    assert event.abspath == str(cached_path)
    assert event.version == str(cached_binary.loaded_version)
    assert event.sha256 == cached_binary.loaded_sha256
    assert event.binproviders == "env"
    assert event.binprovider == "env"
    assert event.env == {"CACHED_ENV": "1"}
    assert event.extra_context == request.extra_context
    assert event.extra_context is not request.extra_context
    assert backend.invalidations == []
    assert len(backend.gets) == 1
    assert len(backend.sets) == 1
    assert backend.sets[0][0] is request
    assert backend.sets[0][1].loaded_abspath == cached_path


def test_binary_cache_service_stores_resolved_binary_event() -> None:
    from abxpkg.binary_service import (
        BinaryCacheService,
        BinaryRequestEvent,
        BinaryService,
    )

    backend = _MemoryBinaryCacheBackend()

    async def run() -> tuple[Any, Binary]:
        bus = abxbus.EventBus(name="test_binary_cache_service_stores_event")
        BinaryCacheService(bus, backend=backend)
        BinaryService(bus, auto_install=False)
        request = await bus.emit(
            BinaryRequestEvent(
                name="python",
                binproviders="env",
                extra_context={"binary_id": "python-cache"},
            ),
        ).now()
        await request.event_results_list()
        assert backend.binary is not None
        return request, backend.binary

    request, cached = asyncio.run(run())

    assert len(backend.sets) == 1
    assert backend.sets[0][0] is request
    assert cached.name == "python"
    assert cached.loaded_abspath is not None
    assert cached.loaded_version is not None
    assert cached.loaded_binprovider is not None
    assert cached.loaded_binprovider.name == "env"
    assert cached.model_extra
    assert "env" in cached.model_extra
    assert "extra_context" not in cached.model_extra
    assert request.extra_context == {"binary_id": "python-cache"}


def test_binary_cache_service_invalidates_stale_cached_binary(tmp_path: Path) -> None:
    from abxpkg.binary_service import BinaryCacheService, BinaryRequestEvent

    stale_binary = _real_python_binary(tmp_path)
    missing_path = stale_binary.loaded_abspath
    assert missing_path is not None
    backend = _MemoryBinaryCacheBackend(stale_binary)
    missing_path.unlink()

    async def run() -> list[Any]:
        bus = abxbus.EventBus(name="test_binary_cache_service_invalidates")
        BinaryCacheService(bus, backend=backend)
        request = await bus.emit(
            BinaryRequestEvent(
                name="python",
                binproviders="env",
                auto_install=False,
            ),
        ).now()
        return await request.event_results_list(raise_if_none=False)

    results = asyncio.run(run())

    assert results == []
    assert len(backend.invalidations) == 1
    assert "does not exist" in backend.invalidations[0][2]


def test_binary_service_trusts_injected_binary_event_for_same_request(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent, BinaryService

    injected_binary = _real_python_binary(tmp_path / "injected")
    injected_path = injected_binary.loaded_abspath
    assert injected_path is not None

    async def run() -> tuple[BinaryRequestEvent, BinaryEvent, list[Any]]:
        bus = abxbus.EventBus(name="test_binary_service_trusts_injected_event")

        async def inject_binary(event: BinaryRequestEvent) -> None:
            await event.emit(
                BinaryEvent(
                    name=event.name,
                    abspath=str(injected_path),
                    version=str(injected_binary.loaded_version),
                    sha256=injected_binary.loaded_sha256,
                    binproviders="env",
                    binprovider="env",
                ),
            ).now()

        bus.on(BinaryRequestEvent, inject_binary)
        BinaryService(bus, auto_install=False, lib_dir=tmp_path / "service")

        request = await bus.emit(
            BinaryRequestEvent(
                name="python",
                binproviders="env",
            ),
        ).now()
        event = await bus.find(
            BinaryEvent,
            child_of=request,
            past=True,
            future=False,
            name="python",
        )
        assert isinstance(event, BinaryEvent)
        return request, event, await request.event_results_list()

    request, event, results = asyncio.run(run())

    assert event.abspath == str(injected_path)
    assert event.event_parent_id == request.event_id
    assert results == [str(injected_path)]


def test_binary_service_ignores_binary_events_from_other_requests(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent, BinaryService

    injected_binary = _real_python_binary(tmp_path / "stale")
    stale_path = injected_binary.loaded_abspath
    assert stale_path is not None

    async def run() -> tuple[BinaryEvent, BinaryEvent]:
        bus = abxbus.EventBus(name="test_binary_service_ignores_other_requests")
        seeded = False

        async def seed_first_request(event: BinaryRequestEvent) -> None:
            nonlocal seeded
            if seeded:
                return
            seeded = True
            await event.emit(
                BinaryEvent(
                    name=event.name,
                    abspath=str(stale_path),
                    version=str(injected_binary.loaded_version),
                    sha256=injected_binary.loaded_sha256,
                    binproviders="env",
                    binprovider="env",
                ),
            ).now()

        bus.on(BinaryRequestEvent, seed_first_request)

        seed_request = await bus.emit(
            BinaryRequestEvent(
                name="python",
                binproviders="env",
            ),
        ).now()
        stale_event = await bus.find(
            BinaryEvent,
            child_of=seed_request,
            past=True,
            future=False,
            name="python",
        )
        assert isinstance(stale_event, BinaryEvent)

        BinaryService(bus, auto_install=False, lib_dir=tmp_path / "resolved")
        request = await bus.emit(
            BinaryRequestEvent(
                name="python",
                binproviders="env",
            ),
        ).now()
        event = await bus.find(
            BinaryEvent,
            child_of=request,
            past=True,
            future=False,
            name="python",
        )
        assert isinstance(event, BinaryEvent)
        return stale_event, event

    stale_event, event = asyncio.run(run())

    assert stale_event.abspath == str(stale_path)
    assert event.abspath != stale_event.abspath
    assert Path(event.abspath).exists()


def test_binary_service_allows_parallel_installs_for_different_provider_roots(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import BinaryRequestEvent, BinaryService

    async def run() -> tuple[list[Any], list[Any]]:
        bus = abxbus.EventBus(name="test_binary_service_parallel_installs")
        service = BinaryService(bus)
        requests = [
            bus.emit(
                BinaryRequestEvent(
                    name="black",
                    binproviders="pip",
                    install_root=tmp_path / "black-root",
                    postinstall_scripts=True,
                    min_release_age=3,
                    overrides={"pip": {"install_args": ["black"]}},
                ),
            ),
            bus.emit(
                BinaryRequestEvent(
                    name="isort",
                    binproviders="pip",
                    install_root=tmp_path / "isort-root",
                    postinstall_scripts=True,
                    min_release_age=3,
                    overrides={"pip": {"install_args": ["isort"]}},
                ),
            ),
        ]
        assert service._install_semaphore_name(
            requests[0],
        ) != service._install_semaphore_name(
            requests[1],
        )
        await asyncio.gather(*(request.now() for request in requests))
        return (
            await requests[0].event_results_list(),
            await requests[1].event_results_list(),
        )

    black_results, isort_results = asyncio.run(run())
    assert len(black_results) == 1 and Path(black_results[0]).exists()
    assert len(isort_results) == 1 and Path(isort_results[0]).exists()


def test_binary_service_serializes_installs_for_same_provider_root(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import BinaryRequestEvent, BinaryService

    async def run() -> tuple[list[Any], list[Any]]:
        bus = abxbus.EventBus(name="test_binary_service_serial_installs")
        BinaryService(bus, install_root=tmp_path / "shared-pip-root")
        requests = [
            bus.emit(
                BinaryRequestEvent(
                    name="black",
                    binproviders="pip",
                    postinstall_scripts=True,
                    min_release_age=3,
                    overrides={"pip": {"install_args": ["black"]}},
                ),
            ),
            bus.emit(
                BinaryRequestEvent(
                    name="isort",
                    binproviders="pip",
                    postinstall_scripts=True,
                    min_release_age=3,
                    overrides={"pip": {"install_args": ["isort"]}},
                ),
            ),
        ]

        await asyncio.gather(*(request.now() for request in requests))
        return (
            await requests[0].event_results_list(),
            await requests[1].event_results_list(),
        )

    black_results, isort_results = asyncio.run(run())
    assert len(black_results) == 1 and Path(black_results[0]).exists()
    assert len(isort_results) == 1 and Path(isort_results[0]).exists()
    assert Path(black_results[0]).is_relative_to(tmp_path / "shared-pip-root")
    assert Path(isort_results[0]).is_relative_to(tmp_path / "shared-pip-root")


def test_binary_service_rechecks_same_request_after_install_semaphore(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import BinaryRequestEvent, BinaryService

    async def run() -> tuple[list[Any], list[Any]]:
        bus = abxbus.EventBus(name="test_binary_service_same_root_race")
        BinaryService(bus, install_root=tmp_path / "shared-root")
        first = bus.emit(
            BinaryRequestEvent(
                name="black",
                binproviders="pip",
                postinstall_scripts=True,
                min_release_age=3,
                overrides={"pip": {"install_args": ["black"]}},
            ),
        )
        second = bus.emit(
            BinaryRequestEvent(
                name="black",
                binproviders="pip",
                postinstall_scripts=True,
                min_release_age=3,
                overrides={"pip": {"install_args": ["black"]}},
            ),
        )

        await asyncio.gather(first.now(), second.now())
        return await first.event_results_list(), await second.event_results_list()

    first_results, second_results = asyncio.run(run())

    assert first_results == second_results
    assert len(first_results) == 1
    assert Path(first_results[0]).exists()


def test_binary_service_failed_install_raises_from_handler(tmp_path: Path) -> None:
    from abxpkg.binary_service import BinaryRequestEvent, BinaryService
    from abxpkg.exceptions import BinaryInstallError

    async def run() -> None:
        bus = abxbus.EventBus(name="test_binary_service_failed_install")
        BinaryService(bus, install_root=tmp_path / "pip-root")
        request = await bus.emit(
            BinaryRequestEvent(
                name="abxpkg-package-that-does-not-exist",
                binproviders="pip",
                overrides={
                    "pip": {
                        "install_args": ["abxpkg-package-that-does-not-exist"],
                    },
                },
            ),
        ).now()

        errors = [
            result.error
            for result in request.event_results.values()
            if isinstance(result.error, BinaryInstallError)
        ]
        assert len(errors) == 1
        with pytest.raises(BinaryInstallError):
            await request.event_results_list()

    asyncio.run(run())


def test_binary_service_loads_env_binary_from_request() -> None:
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent, BinaryService

    async def run() -> tuple[BinaryRequestEvent, BinaryEvent]:
        bus = abxbus.EventBus(name="test_binary_service_loads_env_binary_from_request")
        BinaryService(bus, auto_install=False)

        request = await bus.emit(
            BinaryRequestEvent(
                name="python",
                binproviders="env",
                description="Python interpreter",
                base_env={"ABXPKG_BINARY_SERVICE_TEST": "base"},
                extra_env={"ABXPKG_BINARY_SERVICE_TEST_EXTRA": "extra"},
                extra_context={
                    "plugin_name": "python-plugin",
                    "binary_id": "python-binary",
                    "machine_id": "machine-123",
                },
            ),
        ).now()

        event = await bus.find(BinaryEvent, past=True, future=False, name="python")
        assert isinstance(event, BinaryEvent)
        assert await request.event_results_list() == [event.abspath]
        return request, event

    request, event = asyncio.run(run())

    assert Path(event.abspath).exists()
    assert event.version
    assert event.binproviders == "env"
    assert event.binprovider == "env"
    assert event.description == "Python interpreter"
    assert event.env["ABXPKG_BINARY_SERVICE_TEST"] == "base"
    assert event.env["ABXPKG_BINARY_SERVICE_TEST_EXTRA"] == "extra"
    assert event.extra_context == {
        "plugin_name": "python-plugin",
        "binary_id": "python-binary",
        "machine_id": "machine-123",
    }
    assert event.extra_context == request.extra_context
    assert event.extra_context is not request.extra_context
    assert event.extra_context["machine_id"] == request.extra_context["machine_id"]


def test_binary_service_installs_real_pip_binary_from_request(tmp_path: Path) -> None:
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent, BinaryService

    async def run() -> BinaryEvent:
        bus = abxbus.EventBus(
            name="test_binary_service_installs_real_pip_binary_from_request",
        )
        BinaryService(bus, install_root=tmp_path / "pip-root")

        request = await bus.emit(
            BinaryRequestEvent(
                name="black",
                binproviders="pip",
                postinstall_scripts=True,
                min_release_age=3,
                overrides={
                    "pip": {
                        "install_args": ["black"],
                    },
                },
            ),
        ).now()

        event = await bus.find(BinaryEvent, past=True, future=False, name="black")
        assert isinstance(event, BinaryEvent)
        assert await request.event_results_list() == [event.abspath]
        return event

    event = asyncio.run(run())

    assert Path(event.abspath).exists()
    assert event.version
    assert event.binproviders == "pip"
    assert event.binprovider == "pip"
    assert event.env.get("VIRTUAL_ENV")
    assert "PYTHONPATH" in event.env

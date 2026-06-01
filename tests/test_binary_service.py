import asyncio
import stat
import threading
import time
from pathlib import Path
from typing import Any, Self

import pytest

from abxpkg import Binary, BinProviderName
from abxpkg.exceptions import BinaryLoadError
from abxpkg.semver import SemVer


def test_binary_request_events_allow_parallel_scheduling_by_default(
    tmp_path: Path,
) -> None:
    abxbus = pytest.importorskip("abxbus")
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
        min_release_age=0,
        overrides={"pip": {"install_args": ["event-package"]}},
        extra_env={"DEFAULT_EXTRA_ENV": "event", "EVENT_EXTRA_ENV": "event"},
    )
    event_binary = defaulted_service._binary_for_event(event_overrides)
    assert event_binary.description == "Event description"
    assert event_binary.min_version == SemVer("2.0.0")
    assert event_binary.postinstall_scripts is True
    assert event_binary.min_release_age == 0
    assert event_binary.overrides == {
        "pip": {"install_args": ["event-package"]},
    }
    assert defaulted_service._extra_env_for_event(event_overrides) == {
        "DEFAULT_EXTRA_ENV": "event",
        "EVENT_EXTRA_ENV": "event",
    }


class _InstallProbe:
    def __init__(
        self,
        *,
        sleep_seconds: float = 0.2,
        barrier: threading.Barrier | None = None,
    ):
        self.sleep_seconds = sleep_seconds
        self.barrier = barrier
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.starts: list[tuple[str, float]] = []
        self.ends: list[tuple[str, float]] = []

    def run(self, name: str) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.starts.append((name, time.monotonic()))
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            time.sleep(self.sleep_seconds)
        finally:
            with self.lock:
                self.ends.append((name, time.monotonic()))
                self.active -= 1


class _ProbeBinary(Binary):
    def __init__(self, service: Any, event: Any):
        super().__init__(
            name=event.name,
            binproviders=service._providers_for_event(event),
        )
        object.__setattr__(self, "service", service)
        object.__setattr__(self, "event", event)

    def install(
        self,
        binproviders: list[BinProviderName] | None = None,
        no_cache: bool = False,
        dry_run: bool | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        **extra_overrides: Any,
    ) -> Self:
        del binproviders, no_cache, dry_run, postinstall_scripts, min_release_age
        del extra_overrides
        provider = self.binproviders[0]
        self.service.probe.run(self.event.name)
        path = self.service.output_dir / f"{self.event.name}-{provider.name}"
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.loaded_binprovider = provider
        self.loaded_abspath = path
        self.loaded_version = SemVer("1.0.0")
        self.loaded_sha256 = "0" * 64
        return self


def _probe_service_class():
    from abxpkg.binary_service import BinaryService

    class ProbeBinaryService(BinaryService):
        def __init__(
            self,
            *args: Any,
            probe: _InstallProbe,
            output_dir: Path,
            **kwargs: Any,
        ):
            self.probe = probe
            self.output_dir = output_dir
            super().__init__(*args, **kwargs)

        def _load(self, event: Any) -> Binary:
            raise BinaryLoadError(event.name, str(event.binproviders), {})

        def _binary_for_event(self, event: Any) -> Binary:
            return _ProbeBinary(self, event)

    return ProbeBinaryService


def test_binary_service_trusts_injected_binary_event_for_same_request(
    tmp_path: Path,
) -> None:
    abxbus = pytest.importorskip("abxbus")
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent

    injected_path = tmp_path / "injected-tool"
    injected_path.write_text("#!/bin/sh\nexit 0\n")
    injected_path.chmod(injected_path.stat().st_mode | stat.S_IXUSR)

    class NoResolutionService(_probe_service_class()):
        def _load(self, event: Any) -> Binary:
            raise AssertionError("load should not run after an injected event")

        async def _install_or_find(self, event: Any) -> Binary | BinaryEvent:
            raise AssertionError("install should not run after an injected event")

    async def run() -> tuple[_InstallProbe, BinaryRequestEvent, BinaryEvent, list[str]]:
        probe = _InstallProbe()
        bus = abxbus.EventBus(name="test_binary_service_trusts_injected_event")

        async def inject_binary(event: BinaryRequestEvent) -> None:
            await event.emit(
                BinaryEvent(
                    name=event.name,
                    abspath=str(injected_path),
                    version="9.9.9",
                    sha256="1" * 64,
                    binproviders="pip",
                    binprovider="pip",
                ),
            ).now()  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore[unresolved-attribute]

        bus.on(BinaryRequestEvent, inject_binary)
        NoResolutionService(bus, probe=probe, output_dir=tmp_path)

        request = await bus.emit(
            BinaryRequestEvent(
                name="tool",
                binproviders="pip",
            ),
        ).now()
        event = await bus.find(
            BinaryEvent,
            child_of=request,
            past=True,
            future=False,
            name="tool",
        )
        assert isinstance(event, BinaryEvent)
        return probe, request, event, await request.event_results_list()

    probe, request, event, results = asyncio.run(run())

    assert probe.starts == []
    assert event.abspath == str(injected_path)
    assert event.event_parent_id == request.event_id
    assert results == [str(injected_path)]


def test_binary_service_ignores_binary_events_from_other_requests(
    tmp_path: Path,
) -> None:
    abxbus = pytest.importorskip("abxbus")
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent

    stale_path = tmp_path / "stale-tool"
    stale_path.write_text("#!/bin/sh\nexit 0\n")
    stale_path.chmod(stale_path.stat().st_mode | stat.S_IXUSR)

    async def run() -> tuple[_InstallProbe, BinaryEvent, BinaryEvent]:
        probe = _InstallProbe()
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
                    binproviders="pip",
                    binprovider="pip",
                ),
            ).now()  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore[unresolved-attribute]

        bus.on(BinaryRequestEvent, seed_first_request)

        seed_request = await bus.emit(
            BinaryRequestEvent(
                name="tool",
                binproviders="pip",
            ),
        ).now()
        stale_event = await bus.find(
            BinaryEvent,
            child_of=seed_request,
            past=True,
            future=False,
            name="tool",
        )
        assert isinstance(stale_event, BinaryEvent)

        _probe_service_class()(bus, probe=probe, output_dir=tmp_path)
        request = await bus.emit(
            BinaryRequestEvent(
                name="tool",
                binproviders="pip",
            ),
        ).now()
        event = await bus.find(
            BinaryEvent,
            child_of=request,
            past=True,
            future=False,
            name="tool",
        )
        assert isinstance(event, BinaryEvent)
        return probe, stale_event, event

    probe, stale_event, event = asyncio.run(run())

    assert [name for name, _ in probe.starts] == ["tool"]
    assert stale_event.abspath == str(stale_path)
    assert event.abspath != stale_event.abspath
    assert Path(event.abspath).exists()


def test_binary_service_allows_parallel_installs_for_different_provider_roots(
    tmp_path: Path,
) -> None:
    abxbus = pytest.importorskip("abxbus")
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent

    async def run() -> _InstallProbe:
        probe = _InstallProbe(barrier=threading.Barrier(2))
        bus = abxbus.EventBus(name="test_binary_service_parallel_installs")
        service = _probe_service_class()(bus, probe=probe, output_dir=tmp_path)
        requests = [
            bus.emit(BinaryRequestEvent(name="probe-pip", binproviders="pip")),
            bus.emit(BinaryRequestEvent(name="probe-npm", binproviders="npm")),
        ]
        assert service._install_semaphore_name(
            requests[0],
        ) != service._install_semaphore_name(
            requests[1],
        )

        await asyncio.gather(*(request.now() for request in requests))
        pip_event = await bus.find(
            BinaryEvent,
            past=True,
            future=False,
            name="probe-pip",
        )
        npm_event = await bus.find(
            BinaryEvent,
            past=True,
            future=False,
            name="probe-npm",
        )
        assert isinstance(pip_event, BinaryEvent)
        assert isinstance(npm_event, BinaryEvent)
        return probe

    probe = asyncio.run(run())

    assert probe.max_active == 2
    assert {name for name, _ in probe.starts} == {"probe-pip", "probe-npm"}
    assert {name for name, _ in probe.ends} == {"probe-pip", "probe-npm"}


def test_binary_service_serializes_installs_for_same_provider_root(
    tmp_path: Path,
) -> None:
    abxbus = pytest.importorskip("abxbus")
    from abxpkg.binary_service import BinaryRequestEvent

    async def run() -> _InstallProbe:
        probe = _InstallProbe(sleep_seconds=0.25)
        bus = abxbus.EventBus(name="test_binary_service_serial_installs")
        _probe_service_class()(
            bus,
            probe=probe,
            output_dir=tmp_path,
            install_root=tmp_path / "shared-pip-root",
        )
        requests = [
            bus.emit(BinaryRequestEvent(name="probe-one", binproviders="pip")),
            bus.emit(BinaryRequestEvent(name="probe-two", binproviders="pip")),
        ]

        await asyncio.gather(*(request.now() for request in requests))
        return probe

    started_at = time.monotonic()
    probe = asyncio.run(run())
    elapsed = time.monotonic() - started_at

    assert probe.max_active == 1
    assert len(probe.starts) == 2
    assert len(probe.ends) == 2
    assert (
        sorted(probe.starts, key=lambda item: item[1])[1][1]
        >= sorted(
            probe.ends,
            key=lambda item: item[1],
        )[0][1]
    )
    assert elapsed >= 0.45


def test_binary_service_rechecks_same_request_after_install_semaphore(
    tmp_path: Path,
) -> None:
    abxbus = pytest.importorskip("abxbus")
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent

    injected_path = tmp_path / "race-target"
    injected_path.write_text("#!/bin/sh\nexit 0\n")
    injected_path.chmod(injected_path.stat().st_mode | stat.S_IXUSR)

    async def run() -> tuple[_InstallProbe, list[str], list[str]]:
        loop = asyncio.get_running_loop()
        second_load_seen = asyncio.Event()
        probe = _InstallProbe(sleep_seconds=0.35)
        bus = abxbus.EventBus(name="test_binary_service_same_root_race")
        background_tasks: list[asyncio.Task[None]] = []

        async def inject_after_service_is_waiting(
            event: BinaryRequestEvent,
        ) -> None:
            if event.name != "race-target":
                return

            async def emit_later() -> None:
                await second_load_seen.wait()
                await asyncio.sleep(0.05)
                await event.emit(
                    BinaryEvent(
                        name="race-target",
                        abspath=str(injected_path),
                        binproviders="pip",
                        binprovider="pip",
                    ),
                ).now()  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore[unresolved-attribute]

            background_tasks.append(asyncio.create_task(emit_later()))

        class RaceService(_probe_service_class()):
            def _load(self, event: Any) -> Binary:
                if event.name == "race-target":
                    loop.call_soon_threadsafe(second_load_seen.set)
                return super()._load(event)

        bus.on(BinaryRequestEvent, inject_after_service_is_waiting)
        RaceService(
            bus,
            probe=probe,
            output_dir=tmp_path,
            install_root=tmp_path / "shared-root",
        )

        first = bus.emit(BinaryRequestEvent(name="slow-holder", binproviders="pip"))
        while not probe.starts:
            await asyncio.sleep(0.01)

        second = bus.emit(BinaryRequestEvent(name="race-target", binproviders="pip"))
        await asyncio.wait_for(second_load_seen.wait(), timeout=2)
        if background_tasks:
            await asyncio.gather(*background_tasks)

        await asyncio.gather(first.now(), second.now())
        return (
            probe,
            await first.event_results_list(),
            await second.event_results_list(),
        )

    probe, first_results, second_results = asyncio.run(run())

    assert [name for name, _ in probe.starts] == ["slow-holder"]
    assert first_results == [str(tmp_path / "slow-holder-pip")]
    assert second_results == [str(injected_path)]


def test_binary_service_failed_install_raises_from_handler(tmp_path: Path) -> None:
    abxbus = pytest.importorskip("abxbus")
    from abxpkg.binary_service import BinaryRequestEvent
    from abxpkg.exceptions import BinaryInstallError

    class FailingBinary(Binary):
        def install(
            self,
            binproviders: list[BinProviderName] | None = None,
            no_cache: bool = False,
            dry_run: bool | None = None,
            postinstall_scripts: bool | None = None,
            min_release_age: float | None = None,
            **extra_overrides: Any,
        ) -> Self:
            del binproviders, no_cache, dry_run, postinstall_scripts, min_release_age
            del extra_overrides
            raise BinaryInstallError(self.name, "pip", {"pip": "boom"})

    class FailingService(_probe_service_class()):
        def _binary_for_event(self, event: Any) -> Binary:
            return FailingBinary(
                name=event.name,
                binproviders=self._providers_for_event(event),
            )

    async def run() -> None:
        bus = abxbus.EventBus(name="test_binary_service_failed_install")
        FailingService(bus, probe=_InstallProbe(), output_dir=tmp_path)
        request = await bus.emit(
            BinaryRequestEvent(name="fail-tool", binproviders="pip"),
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
    abxbus = pytest.importorskip("abxbus")
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent, BinaryService

    async def run() -> BinaryEvent:
        bus = abxbus.EventBus(name="test_binary_service_loads_env_binary_from_request")
        BinaryService(bus, auto_install=False)

        request = await bus.emit(
            BinaryRequestEvent(
                name="python",
                binproviders="env",
                description="Python interpreter",
                base_env={"ABXPKG_BINARY_SERVICE_TEST": "base"},
                extra_env={"ABXPKG_BINARY_SERVICE_TEST_EXTRA": "extra"},
            ),
        ).now()

        event = await bus.find(BinaryEvent, past=True, future=False, name="python")
        assert isinstance(event, BinaryEvent)
        assert await request.event_results_list() == [event.abspath]
        return event

    event = asyncio.run(run())

    assert Path(event.abspath).exists()
    assert event.version
    assert event.binproviders == "env"
    assert event.binprovider == "env"
    assert event.description == "Python interpreter"
    assert event.env["ABXPKG_BINARY_SERVICE_TEST"] == "base"
    assert event.env["ABXPKG_BINARY_SERVICE_TEST_EXTRA"] == "extra"


def test_binary_service_installs_real_pip_binary_from_request(tmp_path: Path) -> None:
    abxbus = pytest.importorskip("abxbus")
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
                min_release_age=0,
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

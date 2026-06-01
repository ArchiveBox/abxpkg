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
    assert event.event_handler_concurrency is None
    assert service._install_semaphore_name(event) == service._install_semaphore_name(
        event,
    )
    assert service._install_semaphore_name(
        event,
    ) != other_service._install_semaphore_name(
        event,
    )


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
                binary_id="test-python",
                machine_id="test-machine",
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
    assert event.binary_id == "test-python"
    assert event.machine_id == "test-machine"


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

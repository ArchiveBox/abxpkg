import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

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

    real_lib = tmp_path / "real-lib"
    real_lib.mkdir()
    linked_lib = tmp_path / "linked-lib"
    linked_lib.symlink_to(real_lib, target_is_directory=True)
    linked_service = BinaryService(
        abxbus.EventBus(name="test_binary_request_events_linked_lib_dir"),
        lib_dir=linked_lib,
    )
    linked_provider = linked_service._providers_for_event(
        BinaryRequestEvent(name="tool", binproviders="env"),
    )[0]
    assert linked_provider.install_root == real_lib.resolve() / "env"
    linked_override_event = BinaryRequestEvent(
        name="tool",
        binproviders="env",
        overrides={"env": {"install_root": linked_lib}},
    )
    linked_override_provider = linked_service._binary_for_event(
        linked_override_event,
    ).get_binprovider("env")
    assert linked_override_provider.install_root == real_lib.resolve()
    cached_provider = linked_provider._resolved_provider_from_cache_record(
        {
            "resolved_provider_name": "env",
            "resolved_provider_install_root": str(linked_lib),
            "resolved_provider_bin_dir": str(linked_lib / "bin"),
        },
    )
    assert cached_provider.install_root == real_lib.resolve()
    assert cached_provider.bin_dir == real_lib.resolve() / "bin"

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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux provider lock ordering")
def test_parallel_cross_provider_cache_checks_do_not_deadlock(
    tmp_path: Path,
) -> None:
    script = """
import asyncio
import sys
import abxbus
from abxpkg.binary_service import BinaryRequestEvent, BinaryService

async def main():
    bus = abxbus.EventBus(name="parallel_cross_provider_cache_checks")
    BinaryService(bus, auto_install=False, lib_dir=sys.argv[1])
    requests = [
        BinaryRequestEvent(name=name, binproviders=providers, auto_install=False)
        for _ in range(8)
        for name, providers in (
            ("node", "env,node,brew,apt"),
            ("postlight-parser", "env,npm"),
        )
    ]
    await asyncio.gather(*(bus.emit(request).now() for request in requests))

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "lib")],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _real_python_binary(lib_dir: Path) -> Binary:
    provider = EnvProvider(install_root=lib_dir / "env")
    binary = Binary(name="python", binproviders=[provider]).load(no_cache=True)
    assert binary.loaded_abspath is not None
    return binary


def test_binary_request_cache_key_normalizes_inputs(tmp_path: Path) -> None:
    from abxpkg.config import binary_request_cache_key

    request = {"name": "python3", "binproviders": "env"}
    base_key = binary_request_cache_key(
        request,
        default_provider_names=["env"],
        env={"ABXPKG_TMP_CACHE_DIR": "/tmp/first"},
    )
    assert (
        binary_request_cache_key(
            request,
            default_provider_names=["env"],
            env={"ABXPKG_TMP_CACHE_DIR": "/tmp/second"},
        )
        == base_key
    )
    for name in ("ABXPKG_DRY_RUN", "ABXPKG_DEBUG", "ABXPKG_NO_CACHE"):
        assert (
            binary_request_cache_key(
                request,
                default_provider_names=["env"],
                env={name: "False"},
            )
            == base_key
        )
    assert (
        binary_request_cache_key(
            request,
            default_provider_names=["env"],
            env={"ABXPKG_ENV_ROOT": "/opt/alternate-env"},
        )
        != base_key
    )
    assert (
        binary_request_cache_key(
            {**request, "overrides": {}},
            default_provider_names=["env"],
            env={},
        )
        == base_key
    )
    assert binary_request_cache_key(
        {
            **request,
            "overrides": {"env": {"min_release_age": 0}},
        },
        default_provider_names=["env"],
        env={},
    ) == binary_request_cache_key(
        {
            **request,
            "overrides": {"env": {"min_release_age": 0.0}},
        },
        default_provider_names=["env"],
        env={},
    )

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)

    def path_request(root: Path) -> dict[str, object]:
        return {
            "name": str(root / "python"),
            "binproviders": "env",
            "install_root": root / "install",
            "bin_dir": root / "bin",
            "overrides": {
                "env": {
                    "install_root": str(root / "provider"),
                    "abspath": str(root / "python"),
                },
            },
        }

    assert binary_request_cache_key(
        path_request(linked_root),
        default_provider_names=["env"],
        env={},
    ) == binary_request_cache_key(
        path_request(real_root),
        default_provider_names=["env"],
        env={},
    )


def test_binary_service_request_projection_uses_effective_service_options(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import BinaryRequestEvent, BinaryService
    from abxpkg.config import (
        binary_request_cache_key,
        load_derived_cache,
        save_derived_cache,
    )

    lib_dir = tmp_path / "lib"
    run_id = 0

    async def run(*, dry_run: bool = False) -> None:
        nonlocal run_id
        run_id += 1
        bus = abxbus.EventBus(name=f"test_effective_service_projection_{run_id}")
        BinaryService(
            bus,
            auto_install=False,
            lib_dir=lib_dir,
            min_version="3.0.0",
        )
        await bus.emit(
            BinaryRequestEvent(name="python3", binproviders="env", dry_run=dry_run),
        ).now()
        await bus.wait_until_idle()
        await bus.destroy(clear=False)

    asyncio.run(run())

    projection_keys = {
        key
        for record in load_derived_cache(lib_dir / "env" / "derived.env").values()
        for key in cast(
            dict[str, object],
            record.get("request_exec_projections", {}),
        )
    }
    raw_key = binary_request_cache_key(
        {"name": "python3", "binproviders": "env"},
        default_provider_names=["env"],
    )
    effective_key = binary_request_cache_key(
        {
            "name": "python3",
            "binproviders": "env",
            "min_version": "3.0.0",
        },
        default_provider_names=["env"],
    )

    assert effective_key in projection_keys
    assert raw_key not in projection_keys

    binary_load_calls = 0

    def count_binary_loads(frame: Any, event: str, arg: Any) -> None:
        del arg
        nonlocal binary_load_calls
        if event == "call" and frame.f_code is BinaryService._load.__code__:
            binary_load_calls += 1

    sys.setprofile(count_binary_loads)
    threading.setprofile(count_binary_loads)
    try:
        asyncio.run(run())
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
    assert binary_load_calls == 0

    cache_before_dry_run = load_derived_cache(lib_dir / "env" / "derived.env")
    asyncio.run(run(dry_run=True))
    assert load_derived_cache(lib_dir / "env" / "derived.env") == cache_before_dry_run

    def request_projections(record: dict[str, Any]) -> dict[str, Any]:
        projections = record.get("request_exec_projections")
        return (
            cast(dict[str, Any], projections) if isinstance(projections, dict) else {}
        )

    cache_path = lib_dir / "env" / "derived.env"
    cache = load_derived_cache(cache_path)
    record = next(
        record
        for record in cache.values()
        if effective_key in request_projections(record)
    )
    projections = request_projections(record)
    projections[effective_key]["validation"]["fingerprint"][0]["mtime_ns"] = 0
    save_derived_cache(cache_path, cache)
    asyncio.run(run())

    refreshed = load_derived_cache(cache_path)
    refreshed_projections = next(
        request_projections(record)
        for record in refreshed.values()
        if effective_key in request_projections(record)
    )
    assert effective_key in refreshed_projections
    assert (
        refreshed_projections[effective_key]["validation"]["fingerprint"][0]["mtime_ns"]
        > 0
    )


def test_binary_event_env_does_not_prepend_shared_host_projections(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent, BinaryService

    runtime_bin = str(Path(sys.executable).parent)

    async def run() -> BinaryEvent:
        bus = abxbus.EventBus(name="test_binary_event_env_keeps_caller_runtime")
        BinaryService(bus, lib_dir=tmp_path / "lib", auto_install=False)
        request = await bus.emit(
            BinaryRequestEvent(
                name="python3",
                binproviders="env",
                base_env={"PATH": f"{runtime_bin}{os.pathsep}{os.defpath}"},
            ),
        ).now()
        event = await bus.find(
            BinaryEvent,
            child_of=request,
            past=True,
            future=False,
            name="python3",
        )
        assert isinstance(event, BinaryEvent)
        return event

    event = asyncio.run(run())

    assert event.env["PATH"].split(os.pathsep)[0] == runtime_bin


def test_binary_cache_service_emits_cached_binary_before_resolver(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import (
        BinaryCacheService,
        BinaryEvent,
        JSONFileBinaryCacheBackend,
        BinaryRequestEvent,
    )

    cached_binary = _real_python_binary(tmp_path)
    cached_path = cached_binary.loaded_abspath
    assert cached_path is not None
    cached_binary.env = {"CACHED_ENV": "1"}
    backend = JSONFileBinaryCacheBackend(tmp_path / "binary-cache.json")
    backend.set(BinaryRequestEvent(name="python"), cached_binary)

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
    persisted = backend.get(request)
    assert persisted is not None
    assert persisted.loaded_abspath == cached_path
    assert backend.path.is_file()


def test_binary_cache_service_stores_resolved_binary_event(tmp_path: Path) -> None:
    from abxpkg.binary_service import (
        BinaryCacheService,
        JSONFileBinaryCacheBackend,
        BinaryRequestEvent,
        BinaryService,
    )

    backend = JSONFileBinaryCacheBackend(tmp_path / "binary-cache.json")

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
        cached = backend.get(request)
        assert cached is not None
        return request, cached

    request, cached = asyncio.run(run())

    assert cached.name == "python"
    assert cached.loaded_abspath is not None
    assert cached.loaded_version is not None
    assert cached.loaded_binprovider is not None
    assert cached.loaded_binprovider.name == "env"
    assert cached.model_extra
    assert "env" in cached.model_extra
    assert "extra_context" not in cached.model_extra
    assert request.extra_context == {"binary_id": "python-cache"}


def test_binary_service_projects_managed_uv_install_through_env_bin(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import (
        BinaryEvent,
        BinaryRequestEvent,
        BinaryService,
    )

    lib = tmp_path / "lib"
    package_root = lib / "uv" / "packages" / "forum-dl"
    projected_entrypoint = lib / "env" / "bin" / "forum-dl"
    managed_entrypoint = package_root / "venv" / "bin" / "forum-dl"

    async def run(bus_name: str, *, no_cache: bool = False) -> tuple[Any, BinaryEvent]:
        bus = abxbus.EventBus(name=bus_name)
        BinaryService(bus)
        request = await bus.emit(
            BinaryRequestEvent(
                name="forum-dl",
                binproviders="env,uv",
                lib_dir=lib,
                min_release_age=3,
                no_cache=no_cache,
                overrides={
                    "uv": {
                        "install_root": str(package_root),
                        "install_args": [
                            "--no-deps",
                            "forum-dl",
                            "chardet==5.2.0",
                            "pydantic==2.12.3",
                            "pydantic-core==2.41.4",
                            "typing-extensions>=4.14.1",
                            "annotated-types>=0.6.0",
                            "typing-inspection>=0.4.2",
                            "beautifulsoup4",
                            "soupsieve",
                            "lxml",
                            "requests",
                            "urllib3",
                            "certifi",
                            "idna",
                            "charset-normalizer",
                            "tenacity",
                            "python-dateutil",
                            "six",
                            "html2text",
                            "warcio",
                        ],
                        "postinstall_scripts": True,
                    },
                },
            ),
        ).now()
        await request.event_results_list()
        event = await bus.find(
            BinaryEvent,
            child_of=request,
            past=True,
            future=False,
            name="forum-dl",
        )
        assert isinstance(event, BinaryEvent)
        await bus.destroy(clear=False)
        return request, event

    _, event = asyncio.run(
        run("test_binary_service_projects_uv_via_env_first", no_cache=True),
    )

    assert event.abspath == str(projected_entrypoint)
    assert event.binprovider == "uv"
    assert projected_entrypoint.is_symlink()
    assert projected_entrypoint.resolve() == managed_entrypoint
    assert os.access(managed_entrypoint, os.X_OK)

    asyncio.run(run("test_binary_service_projects_uv_via_env_prime_cache"))

    binary_load_calls = 0

    def count_binary_loads(frame: Any, event: str, arg: Any) -> None:
        del arg
        nonlocal binary_load_calls
        if event == "call" and frame.f_code is BinaryService._load.__code__:
            binary_load_calls += 1

    sys.setprofile(count_binary_loads)
    threading.setprofile(count_binary_loads)
    started_at = time.perf_counter()
    try:
        _, cached_event = asyncio.run(
            run("test_binary_service_projects_uv_via_env_cached"),
        )
    finally:
        elapsed = time.perf_counter() - started_at
        sys.setprofile(None)
        threading.setprofile(None)

    assert cached_event.abspath == event.abspath
    assert binary_load_calls == 0
    assert elapsed < 0.1


def test_binary_cache_service_invalidates_stale_cached_binary(tmp_path: Path) -> None:
    from abxpkg.binary_service import (
        BinaryCacheService,
        JSONFileBinaryCacheBackend,
        BinaryRequestEvent,
    )

    stale_binary = _real_python_binary(tmp_path)
    missing_path = stale_binary.loaded_abspath
    assert missing_path is not None
    backend = JSONFileBinaryCacheBackend(tmp_path / "binary-cache.json")
    cache_request = BinaryRequestEvent(name="python")
    backend.set(cache_request, stale_binary)
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
    assert backend.get(cache_request) is None


def test_binary_service_emits_resolved_event_for_same_request(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent, BinaryService

    async def run() -> tuple[BinaryRequestEvent, BinaryEvent, list[Any]]:
        bus = abxbus.EventBus(name="test_binary_service_same_request_event")
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

    assert Path(event.abspath).exists()
    assert event.event_parent_id == request.event_id
    assert results == [event.abspath]


def test_binary_service_scopes_events_to_their_real_requests(
    tmp_path: Path,
) -> None:
    from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent, BinaryService

    async def run() -> tuple[
        BinaryRequestEvent,
        BinaryEvent,
        BinaryRequestEvent,
        BinaryEvent,
    ]:
        bus = abxbus.EventBus(name="test_binary_service_request_scoping")
        BinaryService(bus, auto_install=False, lib_dir=tmp_path / "resolved")
        first_request = await bus.emit(
            BinaryRequestEvent(
                name="python",
                binproviders="env",
            ),
        ).now()
        first_event = await bus.find(
            BinaryEvent,
            child_of=first_request,
            past=True,
            future=False,
            name="python",
        )
        assert isinstance(first_event, BinaryEvent)
        second_request = await bus.emit(
            BinaryRequestEvent(
                name="python3",
                binproviders="env",
            ),
        ).now()
        second_event = await bus.find(
            BinaryEvent,
            child_of=second_request,
            past=True,
            future=False,
            name="python3",
        )
        assert isinstance(second_event, BinaryEvent)
        return first_request, first_event, second_request, second_event

    first_request, first_event, second_request, second_event = asyncio.run(run())

    assert first_event.event_parent_id == first_request.event_id
    assert second_event.event_parent_id == second_request.event_id
    assert first_event.event_parent_id != second_event.event_parent_id
    assert Path(first_event.abspath).exists()
    assert Path(second_event.abspath).exists()


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

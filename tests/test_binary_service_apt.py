import asyncio
import os
from pathlib import Path

import abxbus
import pytest

from abxpkg import AptProvider
from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent, BinaryService


@pytest.mark.root_required
def test_binary_service_reloads_managed_install_through_env(
    tmp_path: Path,
    test_machine,
) -> None:
    test_machine.require_tool("apt-get")
    package = test_machine.pick_missing_apt_package()
    apt = AptProvider(postinstall_scripts=True, min_release_age=3)
    lib_dir = tmp_path / "lib"

    async def run() -> tuple[BinaryEvent, BinaryEvent]:
        bus = abxbus.EventBus(
            name="test_binary_service_reloads_managed_install_through_env",
        )
        BinaryService(bus, lib_dir=lib_dir)

        request = await bus.emit(
            BinaryRequestEvent(
                name=package,
                binproviders=["env", "apt"],
                postinstall_scripts=True,
                min_release_age=3,
                no_cache=True,
            ),
        ).now()
        event = await bus.find(
            BinaryEvent,
            child_of=request,
            past=True,
            future=False,
            name=package,
        )
        assert isinstance(event, BinaryEvent)
        assert await request.event_results_list() == [event.abspath]

        hot_request = await bus.emit(
            BinaryRequestEvent(
                name=package,
                binproviders=["env", "apt"],
                auto_install=False,
                lib_dir=lib_dir,
                no_cache=True,
            ),
        ).now()
        hot_event = await bus.find(
            BinaryEvent,
            child_of=hot_request,
            past=True,
            future=False,
            name=package,
        )
        assert isinstance(hot_event, BinaryEvent)
        assert await hot_request.event_results_list() == [hot_event.abspath]
        return event, hot_event

    try:
        event, hot_event = asyncio.run(run())
        projected_abspath = lib_dir / "env" / "bin" / package

        for resolved_event in (event, hot_event):
            assert resolved_event.abspath == str(projected_abspath)
            assert resolved_event.binproviders == "env,apt"
            assert resolved_event.binprovider == "env"
            assert resolved_event.env["PATH"].split(os.pathsep)[0] == str(
                projected_abspath.parent,
            )
        assert projected_abspath.is_symlink()
        assert projected_abspath.resolve().is_file()
        assert not projected_abspath.resolve().is_relative_to(lib_dir)
    finally:
        apt.uninstall(package, quiet=True, no_cache=True)

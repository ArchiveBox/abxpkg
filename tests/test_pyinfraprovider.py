import subprocess
import logging
import os
from pathlib import Path

import pytest

from abxpkg import Binary, EnvProvider, SemVer
from abxpkg.binprovider import BinProvider
from abxpkg.binprovider_pyinfra import (
    PyinfraProvider,
    _resolve_pyinfra_brew_abspath,
    pyinfra_package_install,
)
from abxpkg.config import merge_exec_path
from abxpkg.exceptions import BinaryInstallError
from typing import cast


def _pyinfra_provider_for_host(test_machine):
    test_machine.require_tool("pyinfra")
    apt = EnvProvider().load("apt-get", no_cache=True)
    if apt is None:
        test_machine.require_tool("brew")
    provider = PyinfraProvider(
        postinstall_scripts=True,
        min_release_age=3,
    )
    return provider, test_machine.pick_missing_provider_binary(
        provider,
        (
            "tree",
            "rename",
            "jq",
            "screen",
            "toilet",
            "btop",
            "ranger",
            "mc",
        )
        if apt is not None
        else (
            "hello",
            "jq",
            "watch",
            "fzy",
            "tree",
            "toilet",
            "btop",
            "ranger",
            "nnn",
        ),
    )


class TestPyinfraProvider:
    def test_brew_operations_put_resolved_launcher_before_env_projection(
        self,
        test_machine,
    ):
        test_machine.require_tool("brew")
        env_provider = EnvProvider()
        projected = env_provider.load("brew", no_cache=True)
        assert projected is not None
        assert projected.loaded_abspath is not None
        assert env_provider.bin_dir is not None
        assert projected.loaded_abspath.parent == env_provider.bin_dir
        assert projected.loaded_abspath.is_symlink()

        resolved_brew = _resolve_pyinfra_brew_abspath()
        pyinfra_path = merge_exec_path(
            str(resolved_brew.parent),
            base_path=str(env_provider.PATH),
        )

        assert Path(pyinfra_path.split(os.pathsep)[0]) == resolved_brew.parent
        assert resolved_brew != projected.loaded_abspath
        assert projected.loaded_abspath.is_symlink()
        result = subprocess.run(
            [str(resolved_brew), "--prefix"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert Path(result.stdout.strip()) == resolved_brew.parent.parent

    def test_install_timeout_is_enforced_for_custom_operation_runs(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        test_machine.require_tool("pyinfra")
        provider = PyinfraProvider(
            postinstall_scripts=True,
            min_release_age=3,
        )
        installer = provider.INSTALLER_BINARY().loaded_abspath
        assert installer is not None

        with pytest.raises(subprocess.TimeoutExpired):
            pyinfra_package_install(
                ["sleep 5"],
                pyinfra_abspath=str(installer),
                installer_module="operations.server.shell",
                timeout=2,
            )
        with pytest.raises(subprocess.TimeoutExpired):
            pyinfra_package_install(
                ["sleep 5"],
                pyinfra_abspath=str(installer),
                installer_module="operations.server.shell",
                timeout=2,
            )

    def test_provider_direct_methods_exercise_real_lifecycle(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        provider, package = _pyinfra_provider_for_host(test_machine)

        test_machine.exercise_provider_lifecycle(provider, bin_name=package)

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        test_machine_dependencies,
        caplog,
    ):
        del test_machine_dependencies
        provider, package = _pyinfra_provider_for_host(test_machine)

        cleanup_provider = PyinfraProvider(postinstall_scripts=True, min_release_age=3)
        try:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = PyinfraProvider().install(
                    package,
                    postinstall_scripts=False,
                    min_release_age=1,
                )
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

            caplog.clear()
            binary = Binary(
                name=package,
                binproviders=cast(list[BinProvider], [PyinfraProvider()]),
                postinstall_scripts=False,
                min_release_age=1,
            )
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text
        finally:
            cleanup_provider.uninstall(package, quiet=True, no_cache=True)

    def test_min_version_enforced_in_provider_and_binary_paths(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        provider, package = _pyinfra_provider_for_host(test_machine)
        cleanup_provider = PyinfraProvider(postinstall_scripts=True, min_release_age=3)
        try:
            installed = provider.install(
                package,
                postinstall_scripts=True,
                min_release_age=3,
                no_cache=True,
            )
            test_machine.assert_shallow_binary_loaded(installed)

            with pytest.raises(ValueError):
                provider.update(
                    package,
                    postinstall_scripts=True,
                    min_release_age=3,
                    min_version=SemVer("999.0.0"),
                    no_cache=True,
                )

            too_new = Binary(
                name=package,
                binproviders=[provider],
                postinstall_scripts=True,
                min_release_age=3,
                min_version=SemVer("999.0.0"),
            )
            with pytest.raises(BinaryInstallError):
                too_new.install(no_cache=True)
        finally:
            cleanup_provider.uninstall(package, quiet=True, no_cache=True)

    def test_binary_direct_methods_exercise_real_lifecycle(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        provider, package = _pyinfra_provider_for_host(test_machine)
        binary = Binary(
            name=package,
            binproviders=cast(list[BinProvider], [provider]),
            postinstall_scripts=True,
            min_release_age=3,
        )
        test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_package(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        provider, package = _pyinfra_provider_for_host(test_machine)
        test_machine.exercise_provider_dry_run(provider, bin_name=package)

    def test_search_returns_empty_for_pyinfra_provider(self):
        # PyinfraProvider delegates installs to pyinfra's own operations
        # (apt, brew, ...). It has no package index of its own to search,
        # so search is intentionally an empty list.
        assert PyinfraProvider().search("python") == []
        assert PyinfraProvider().search("nonexistent-binary-xyz") == []

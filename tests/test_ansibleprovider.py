import shutil
import subprocess
import logging

import pytest

from abxpkg import Binary, SemVer
from abxpkg.binprovider import BinProvider
from abxpkg.binprovider_ansible import (
    AnsibleProvider,
    ansible_package_install,
)
from abxpkg.exceptions import BinaryInstallError
from typing import cast


def _ansible_provider_for_host(test_machine):
    test_machine.require_tool("ansible")
    if not shutil.which("apt-get"):
        test_machine.require_tool("brew")
    provider = AnsibleProvider(
        postinstall_scripts=True,
        min_release_age=0,
    )
    return provider, test_machine.pick_missing_provider_binary(
        provider,
        (
            "tree",
            "rename",
            "jq",
            "tmux",
            "screen",
            "sl",
            "toilet",
            "btop",
            "ranger",
            "mc",
        )
        if shutil.which("apt-get")
        else (
            "hello",
            "jq",
            "watch",
            "fzy",
            "tree",
            "sl",
            "toilet",
            "btop",
            "ranger",
            "nnn",
        ),
    )


class TestAnsibleProvider:
    def test_install_timeout_is_enforced_for_custom_playbook_runs(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        test_machine.require_tool("ansible-playbook")
        provider = AnsibleProvider(
            postinstall_scripts=True,
            min_release_age=0,
        )
        installer = provider.INSTALLER_BINARY().loaded_abspath
        assert installer is not None

        playbook_template = """
---
- name: Run a local command
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Run a local command
      {installer_module}:
        cmd: "{{{{item}}}}"
{module_extra_yaml}
      loop: {pkg_names}
"""

        with pytest.raises(subprocess.TimeoutExpired):
            ansible_package_install(
                ["sleep 5"],
                ansible_playbook_abspath=str(installer),
                playbook_template=playbook_template,
                installer_module="ansible.builtin.command",
                timeout=2,
            )
        with pytest.raises(subprocess.TimeoutExpired):
            ansible_package_install(
                ["sleep 5"],
                ansible_playbook_abspath=str(installer),
                playbook_template=playbook_template,
                installer_module="ansible.builtin.command",
                state="latest",
                timeout=2,
            )

    def test_provider_direct_methods_exercise_real_lifecycle(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        provider, package = _ansible_provider_for_host(test_machine)

        test_machine.exercise_provider_lifecycle(provider, bin_name=package)

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        test_machine_dependencies,
        caplog,
    ):
        del test_machine_dependencies
        provider, package = _ansible_provider_for_host(test_machine)

        cleanup_provider = AnsibleProvider(postinstall_scripts=True, min_release_age=0)
        try:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = AnsibleProvider().install(
                    package,
                    postinstall_scripts=False,
                    min_release_age=1,
                    no_cache=True,
                )
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

            caplog.clear()
            binary = Binary(
                name=package,
                binproviders=cast(list[BinProvider], [AnsibleProvider()]),
                postinstall_scripts=False,
                min_release_age=1,
            )
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = binary.install(no_cache=True)
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
        provider, package = _ansible_provider_for_host(test_machine)
        cleanup_provider = AnsibleProvider(postinstall_scripts=True, min_release_age=0)
        try:
            installed = provider.install(
                package,
                postinstall_scripts=True,
                min_release_age=0,
                no_cache=True,
            )
            test_machine.assert_shallow_binary_loaded(installed)

            with pytest.raises(ValueError):
                provider.update(
                    package,
                    postinstall_scripts=True,
                    min_release_age=0,
                    min_version=SemVer("999.0.0"),
                    no_cache=True,
                )

            too_new = Binary(
                name=package,
                binproviders=[provider],
                postinstall_scripts=True,
                min_release_age=0,
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
        provider, package = _ansible_provider_for_host(test_machine)
        binary = Binary(
            name=package,
            binproviders=cast(list[BinProvider], [provider]),
            postinstall_scripts=True,
            min_release_age=0,
        )
        test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_package(
        self,
        test_machine,
        test_machine_dependencies,
    ):
        del test_machine_dependencies
        provider, package = _ansible_provider_for_host(test_machine)
        test_machine.exercise_provider_dry_run(provider, bin_name=package)

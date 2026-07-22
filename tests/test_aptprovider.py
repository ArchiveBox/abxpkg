import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from abxpkg import AptProvider, Binary, EnvProvider


@pytest.mark.root_required
class TestAptProvider:
    def test_env_command_projects_new_host_binary_into_env(
        self,
        test_machine,
        tmp_path,
    ):
        test_machine.require_tool("apt-get")
        package = test_machine.pick_missing_apt_package()
        projected = tmp_path / "env" / "bin" / package
        executable = Path(sys.executable).parent / "abxpkg"
        assert executable.is_file()
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ABXPKG_")
        }

        try:
            proc = subprocess.run(
                [
                    str(executable),
                    f"--lib={tmp_path}",
                    "--binproviders=env,apt",
                    "--no-cache=True",
                    "env",
                    "--install",
                    package,
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=600,
            )

            assert proc.returncode == 0, proc.stderr
            assert projected.is_symlink()
            assert projected.resolve().is_file()
            assert not projected.resolve().is_relative_to(tmp_path)
            path_line = next(
                line for line in proc.stdout.splitlines() if line.startswith("PATH=")
            )
            resolved_path = shlex.split(path_line.removeprefix("PATH="))[0]
            assert resolved_path.split(os.pathsep)[0] == str(projected.parent)

            reloaded = EnvProvider(install_root=tmp_path / "env").load(
                package,
                no_cache=True,
            )
            assert reloaded.loaded_binprovider is not None
            assert reloaded.loaded_binprovider.name == "env"
            assert reloaded.loaded_abspath == projected
        finally:
            AptProvider().uninstall(package, quiet=True, no_cache=True)

    def test_fresh_provider_loads_cached_installer_before_setting_up_path(
        self,
        test_machine,
        tmp_path,
    ):
        test_machine.require_tool("apt-get")

        install_root = tmp_path / "apt"
        seeded_provider = AptProvider(install_root=install_root)
        seeded_installer = seeded_provider.INSTALLER_BINARY()
        assert seeded_installer.is_valid
        assert seeded_provider.derived_env_path is not None
        assert seeded_provider.derived_env_path.is_file()

        fresh_provider = AptProvider(install_root=install_root)
        fresh_provider.setup_PATH()

        assert fresh_provider._INSTALLER_BINARY is not None
        loaded_installer = fresh_provider.INSTALLER_BINARY()
        assert loaded_installer.is_valid
        assert loaded_installer.loaded_abspath == seeded_installer.loaded_abspath
        assert str(loaded_installer.loaded_abspath.parent) in fresh_provider.PATH.split(
            ":",
        )
        loaded_bash = fresh_provider.load("bash")
        assert loaded_bash is not None
        assert loaded_bash.is_valid

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("apt-get")

        provider = AptProvider(postinstall_scripts=True, min_release_age=3)
        test_machine.exercise_provider_lifecycle(
            provider,
            bin_name=test_machine.pick_missing_apt_package(),
        )

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        caplog,
    ):
        test_machine.require_tool("apt-get")
        package = test_machine.pick_missing_apt_package()

        with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
            installed = AptProvider().install(
                package,
                postinstall_scripts=False,
                min_release_age=1,
            )
        test_machine.assert_shallow_binary_loaded(installed)
        assert "ignoring unsupported min_release_age=1" in caplog.text
        assert "ignoring unsupported postinstall_scripts=False" in caplog.text

        caplog.clear()
        binary = Binary(
            name=test_machine.pick_missing_apt_package(),
            binproviders=[AptProvider()],
            postinstall_scripts=False,
            min_release_age=1,
        )
        with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
            installed = binary.install()
        test_machine.assert_shallow_binary_loaded(installed)
        assert "ignoring unsupported min_release_age=1" in caplog.text
        assert "ignoring unsupported postinstall_scripts=False" in caplog.text

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("apt-get")

        binary = Binary(
            name=test_machine.pick_missing_apt_package(),
            binproviders=[
                AptProvider(postinstall_scripts=True, min_release_age=3),
            ],
            postinstall_scripts=True,
            min_release_age=3,
        )
        test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_package(self, test_machine):
        test_machine.require_tool("apt-get")
        provider = AptProvider(postinstall_scripts=True, min_release_age=3)
        test_machine.exercise_provider_dry_run(
            provider,
            bin_name=test_machine.pick_missing_apt_package(),
        )

    def test_search_finds_real_apt_package_and_install_works(self, test_machine):
        test_machine.require_tool("apt-get")
        provider = AptProvider(postinstall_scripts=True, min_release_age=3)
        results = provider.search("wget")
        assert results, "apt-cache search wget should return matches"
        names = [r.name for r in results]
        assert "wget" in names
        wget_match = next(r for r in results if r.name == "wget")
        assert wget_match.overrides == {"apt": {"install_args": ["wget"]}}
        # The returned Binary is non-loaded — it has no abspath/version yet.
        assert wget_match.loaded_abspath is None
        assert wget_match.loaded_version is None
        # ...but installing it must produce a real, valid binary on disk.
        provider.uninstall("wget", quiet=True, no_cache=True)
        installed = wget_match.install()
        test_machine.assert_shallow_binary_loaded(installed)
        assert installed.name == "wget"

    def test_helper_install_args_used_by_native_apt_backend(self, test_machine):
        test_machine.require_tool("apt-get")

        primary = test_machine.pick_missing_apt_package()
        extra = "jq" if primary != "jq" else "tree"

        provider = AptProvider(
            postinstall_scripts=True,
            min_release_age=3,
        ).get_provider_with_overrides(
            overrides={primary: {"install_args": [primary, extra]}},
        )

        for pkg in (primary, extra):
            provider.uninstall(pkg, quiet=True, no_cache=True)

        provider.install(primary, no_cache=True)
        assert provider.load(extra, quiet=True, no_cache=True) is not None

        provider.uninstall(primary, no_cache=True)
        provider.uninstall(extra, quiet=True, no_cache=True)

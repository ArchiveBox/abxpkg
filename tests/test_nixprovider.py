import json
import tempfile
from pathlib import Path
import logging
from typing import cast

import pytest

from abxpkg import Binary, NixProvider, SemVer
from abxpkg.binprovider import BinProvider


class TestNixProvider:
    def test_install_root_alias_installs_into_the_requested_profile(self, test_machine):
        assert NixProvider().INSTALLER_BINARY(), "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "nix-profile"
            provider = NixProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                    "install_timeout": 300,
                },
            )

            installed = provider.install("hello")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        assert NixProvider().INSTALLER_BINARY(), "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NixProvider(
                install_root=Path(temp_dir) / "nix-profile",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="hello")

    def test_repeated_install_does_not_duplicate_profile_entries(self, test_machine):
        assert NixProvider().INSTALLER_BINARY(), "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NixProvider(
                install_root=Path(temp_dir) / "nix-profile",
                postinstall_scripts=True,
                min_release_age=0,
            )

            first = provider.install("hello", no_cache=True)
            second = provider.install("hello", no_cache=True)

            test_machine.assert_shallow_binary_loaded(first)
            test_machine.assert_shallow_binary_loaded(second)

            installer_bin = provider.INSTALLER_BINARY(no_cache=True).loaded_abspath
            assert installer_bin is not None
            proc = provider.exec(
                bin_name=installer_bin,
                cmd=[
                    "profile",
                    "list",
                    "--json",
                    "--extra-experimental-features",
                    "nix-command",
                    "--extra-experimental-features",
                    "flakes",
                    "--profile",
                    str(provider.install_root),
                ],
                quiet=True,
            )
            assert proc.returncode == 0, proc.stderr or proc.stdout
            assert set(json.loads(proc.stdout).get("elements", {})) == {"hello"}

    def test_provider_direct_min_version_revalidates_final_installed_package(
        self,
        test_machine,
    ):
        assert NixProvider().INSTALLER_BINARY(), "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NixProvider(
                install_root=Path(temp_dir) / "nix-profile",
                postinstall_scripts=True,
                min_release_age=0,
            )
            with pytest.raises(ValueError):
                provider.install("hello", min_version=SemVer("999.0.0"))

            loaded = provider.load("hello", quiet=True, no_cache=True)
            test_machine.assert_shallow_binary_loaded(loaded)
            assert loaded is not None
            assert loaded.loaded_version is not None
            required_version = SemVer.parse("999.0.0")
            assert required_version is not None
            assert loaded.loaded_version < required_version

    def test_nix_profile_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        assert NixProvider().INSTALLER_BINARY(), "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = NixProvider(
                install_root=temp_dir_path / "ambient-profile",
                postinstall_scripts=True,
                min_release_age=0,
            )
            ambient_installed = ambient_provider.install("hello")
            assert ambient_installed is not None

            nix_profile = temp_dir_path / "nix-profile"
            provider = NixProvider(
                PATH=f"{ambient_provider.bin_dir}:{NixProvider().PATH}",
                install_root=nix_profile,
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("hello")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == nix_profile
            assert provider.bin_dir == nix_profile / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert ambient_installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath.parent == ambient_provider.bin_dir

    def test_uninstall_preserves_other_profile_entries(self, test_machine):
        assert NixProvider().INSTALLER_BINARY(), "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NixProvider(
                install_root=Path(temp_dir) / "nix-profile",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "hello": {"install_args": ["hello", "figlet"]},
                },
            )

            installed = provider.install("hello")
            test_machine.assert_shallow_binary_loaded(installed)
            assert provider.load("figlet", quiet=True, no_cache=True) is not None

            assert provider.uninstall("hello")
            assert provider.load("hello", quiet=True, no_cache=True) is None
            assert provider.load("figlet", quiet=True, no_cache=True) is not None

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        caplog,
    ):
        assert NixProvider().INSTALLER_BINARY(), "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = NixProvider(
                    install_root=Path(temp_dir) / "bad-profile",
                    postinstall_scripts=False,
                    min_release_age=1,
                ).install("hello")
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

            caplog.clear()
            binary = Binary(
                name="hello",
                binproviders=cast(
                    list[BinProvider],
                    [
                        NixProvider(
                            install_root=Path(temp_dir) / "ok-profile",
                            postinstall_scripts=False,
                            min_release_age=1,
                        ),
                    ],
                ),
                postinstall_scripts=False,
                min_release_age=1,
            )
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        assert NixProvider().INSTALLER_BINARY(), "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="hello",
                binproviders=cast(
                    list[BinProvider],
                    [
                        NixProvider(
                            install_root=Path(temp_dir) / "nix-profile",
                            postinstall_scripts=True,
                            min_release_age=0,
                        ),
                    ],
                ),
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_hello(self, test_machine):
        assert NixProvider().INSTALLER_BINARY(), "nix is required on this host"

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NixProvider(
                install_root=Path(temp_dir) / "nix-profile",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="hello")

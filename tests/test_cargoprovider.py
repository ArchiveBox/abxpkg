import tempfile
from pathlib import Path
import logging
from typing import cast


from abxpkg import Binary, CargoProvider, SemVer
from abxpkg.binprovider import BinProvider


class TestCargoProvider:
    def test_install_args_version_flag_wins_over_min_version(self, test_machine):
        test_machine.require_tool("cargo")
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = CargoProvider(
                install_root=Path(temp_dir) / "cargo-root",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "choose": {"install_args": ["choose", "--version", "1.3.6"]},
                },
            )

            installed = provider.install("choose", min_version=SemVer("1.0.0"))

            test_machine.assert_shallow_binary_loaded(
                installed,
                expected_version=SemVer("1.3.6"),
            )

    def test_install_root_alias_installs_into_the_requested_root(self, test_machine):
        test_machine.require_tool("cargo")
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "cargo-root"
            provider = CargoProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            installed = provider.install("choose")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("cargo")
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = CargoProvider(
                install_root=Path(temp_dir) / "cargo",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="choose")

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        test_machine.require_tool("cargo")
        with tempfile.TemporaryDirectory() as temp_dir:
            cargo_root = Path(temp_dir) / "cargo"
            old_provider = CargoProvider(
                install_root=cargo_root,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "choose": {"install_args": ["choose", "--version", "1.3.6"]},
                },
            )
            old_installed = old_provider.install("choose")
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("1.3.6")

            provider = CargoProvider(
                install_root=cargo_root,
                postinstall_scripts=True,
                min_release_age=0,
            )
            upgraded = provider.install("choose", min_version=SemVer("1.3.7"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("1.3.7"),
            )

            updated = provider.update("choose", min_version=SemVer("1.3.7"))
            test_machine.assert_shallow_binary_loaded(
                updated,
                expected_version=SemVer("1.3.7"),
            )

    def test_uninstall_handles_version_pinned_install_args(self, test_machine):
        test_machine.require_tool("cargo")
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = CargoProvider(
                install_root=Path(temp_dir) / "cargo",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "choose": {"install_args": ["choose", "--version", "1.3.6"]},
                },
            )

            installed = provider.install("choose")
            test_machine.assert_shallow_binary_loaded(
                installed,
                expected_version=SemVer("1.3.6"),
            )

            assert provider.uninstall("choose")
            assert provider.load("choose", quiet=True, no_cache=True) is None

    def test_cargo_root_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        test_machine.require_tool("cargo")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = CargoProvider(
                install_root=temp_dir_path / "ambient-cargo",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "choose": {"install_args": ["choose", "--version", "1.3.6"]},
                },
            )
            ambient_installed = ambient_provider.install(
                "choose",
                min_version=SemVer("1.0.0"),
            )
            assert ambient_installed is not None
            ambient_installer = ambient_provider.INSTALLER_BINARY(no_cache=True)
            assert ambient_installer.loaded_abspath is not None

            cargo_root = temp_dir_path / "cargo"
            cargo_bin_dir = str(ambient_installer.loaded_abspath.parent)
            provider = CargoProvider(
                PATH=f"{ambient_provider.bin_dir}:{cargo_bin_dir}",
                install_root=cargo_root,
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("choose", min_version=SemVer("1.3.7"))

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == cargo_root
            assert provider.bin_dir == cargo_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        caplog,
    ):
        test_machine.require_tool("cargo")
        with tempfile.TemporaryDirectory() as temp_dir:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = CargoProvider(
                    install_root=Path(temp_dir) / "bad-cargo",
                    postinstall_scripts=False,
                    min_release_age=1,
                ).install("choose")
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

            caplog.clear()
            binary = Binary(
                name="choose",
                binproviders=cast(
                    list[BinProvider],
                    [
                        CargoProvider(
                            install_root=Path(temp_dir) / "ok-cargo",
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
        test_machine.require_tool("cargo")
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="choose",
                binproviders=cast(
                    list[BinProvider],
                    [
                        CargoProvider(
                            install_root=Path(temp_dir) / "cargo",
                            postinstall_scripts=True,
                            min_release_age=0,
                        ),
                    ],
                ),
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_choose(self, test_machine):
        test_machine.require_tool("cargo")
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = CargoProvider(
                install_root=Path(temp_dir) / "cargo",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="choose")

import tempfile
from pathlib import Path
import logging

from abxpkg import Binary, GemProvider, SemVer


class TestGemProvider:
    def test_install_args_version_flag_wins_over_min_version(self, test_machine):
        test_machine.require_tool("gem")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GemProvider(
                install_root=Path(temp_dir) / "gem-home",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "lolcat": {"install_args": ["lolcat", "--version", "99.9.99"]},
                },
            )

            installed = provider.install("lolcat", min_version=SemVer("1.0.0"))

            test_machine.assert_shallow_binary_loaded(
                installed,
                expected_version=SemVer("99.9.99"),
            )

    def test_install_root_alias_without_explicit_bin_dir_uses_root_bin(
        self,
        test_machine,
    ):
        test_machine.require_tool("gem")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "gem-home"
            provider = GemProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            installed = provider.install("lolcat")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_install_root_and_bin_dir_aliases_install_into_the_requested_paths(
        self,
        test_machine,
    ):
        test_machine.require_tool("gem")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "gem-home"
            bin_dir = Path(temp_dir) / "custom-bin"
            provider = GemProvider.model_validate(
                {
                    "install_root": install_root,
                    "bin_dir": bin_dir,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            installed = provider.install("lolcat")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_explicit_gem_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        test_machine.require_tool("gem")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = GemProvider(
                install_root=temp_dir_path / "ambient-gem-home",
                bin_dir=temp_dir_path / "ambient-gem-home/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "lolcat": {"install_args": ["lolcat", "--version", "99.9.99"]},
                },
            )
            ambient_installed = ambient_provider.install(
                "lolcat",
                min_version=SemVer("1.0.0"),
            )
            assert ambient_installed is not None

            gem_home = temp_dir_path / "gem-home"
            gem_bindir = temp_dir_path / "custom-bin"
            provider = GemProvider(
                PATH=str(ambient_provider.bin_dir),
                install_root=gem_home,
                bin_dir=gem_bindir,
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("lolcat", min_version=SemVer("100.0.0"))

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == gem_home
            assert provider.bin_dir == gem_bindir
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("gem")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GemProvider(
                install_root=Path(temp_dir) / "gem-home",
                bin_dir=Path(temp_dir) / "gem-home/bin",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="lolcat")

    def test_provider_direct_min_version_enforcement_on_install_and_update(
        self,
        test_machine,
    ):
        test_machine.require_tool("gem")

        with tempfile.TemporaryDirectory() as temp_dir:
            gem_home = Path(temp_dir) / "gem-home"
            gem_bindir = gem_home / "bin"
            old_provider = GemProvider(
                install_root=gem_home,
                bin_dir=gem_bindir,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "lolcat": {"install_args": ["lolcat", "--version", "99.9.99"]},
                },
            )
            old_installed = old_provider.install("lolcat")
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("99.9.99")

            provider = GemProvider(
                install_root=gem_home,
                bin_dir=gem_bindir,
                postinstall_scripts=True,
                min_release_age=0,
            )
            upgraded = provider.install("lolcat", min_version=SemVer("100.0.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("100.0.0"),
            )

            updated = provider.update("lolcat")
            test_machine.assert_shallow_binary_loaded(
                updated,
                expected_version=SemVer("100.0.0"),
            )

            satisfied = provider.install(
                "lolcat",
                min_version=SemVer("100.0.0"),
            )
            test_machine.assert_shallow_binary_loaded(
                satisfied,
                expected_version=SemVer("100.0.0"),
            )

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        caplog,
    ):
        test_machine.require_tool("gem")

        with tempfile.TemporaryDirectory() as temp_dir:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = GemProvider(
                    install_root=Path(temp_dir) / "bad-home",
                    bin_dir=Path(temp_dir) / "bad-home/bin",
                    postinstall_scripts=False,
                    min_release_age=1,
                ).install("lolcat")
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

            caplog.clear()
            binary = Binary(
                name="lolcat",
                binproviders=[
                    GemProvider(
                        install_root=Path(temp_dir) / "ok-home",
                        bin_dir=Path(temp_dir) / "ok-home/bin",
                        postinstall_scripts=False,
                        min_release_age=1,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=1,
            )
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("gem")

        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="lolcat",
                binproviders=[
                    GemProvider(
                        install_root=Path(temp_dir) / "gem-home",
                        bin_dir=Path(temp_dir) / "gem-home/bin",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_cowsay(self, test_machine):
        test_machine.require_tool("gem")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GemProvider(
                install_root=Path(temp_dir) / "gem-home",
                bin_dir=Path(temp_dir) / "gem-home/bin",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="cowsay")

    def test_search_finds_real_rubygem_and_install_works(self, test_machine):
        test_machine.require_tool("gem")
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GemProvider(
                install_root=Path(temp_dir) / "gem-home",
                bin_dir=Path(temp_dir) / "gem-home/bin",
                postinstall_scripts=True,
                min_release_age=0,
            )
            results = provider.search("lolcat")
            assert results, "gem search lolcat should return rubygems matches"
            names = [r.name for r in results]
            assert "lolcat" in names
            match = next(r for r in results if r.name == "lolcat")
            assert match.overrides == {"gem": {"install_args": ["lolcat"]}}
            assert match.loaded_abspath is None
            assert match.loaded_version is None
            installed = match.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed.name == "lolcat"

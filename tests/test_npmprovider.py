import tempfile
from pathlib import Path

import pytest

from abxpkg import Binary, BrewProvider, EnvProvider, NpmProvider, SemVer


class TestNpmProvider:
    def test_installer_binary_respects_abxpkg_binproviders(self, monkeypatch):
        monkeypatch.setenv("ABXPKG_BINPROVIDERS", "brew,env")
        expected = Binary(
            name="npm",
            binproviders=[BrewProvider(), EnvProvider(install_root=None, bin_dir=None)],
        ).load(no_cache=True)
        installer = NpmProvider().INSTALLER_BINARY(no_cache=True)
        assert expected is not None
        assert expected.loaded_binprovider is not None
        assert installer.loaded_binprovider is not None
        assert installer.loaded_binprovider.name == expected.loaded_binprovider.name

        monkeypatch.setenv("ABXPKG_BINPROVIDERS", "env")
        installer = NpmProvider().INSTALLER_BINARY(no_cache=True)
        assert installer.loaded_binprovider is not None
        assert installer.loaded_binprovider.name == "env"

    def test_install_args_win_for_ignore_scripts_and_min_release_age(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            npm_prefix = Path(temp_dir) / "npm"
            provider = NpmProvider(
                install_root=npm_prefix,
                postinstall_scripts=True,
                min_release_age=36500,
            ).get_provider_with_overrides(
                overrides={
                    "gifsicle": {
                        "install_args": [
                            "gifsicle",
                            "--ignore-scripts",
                            "--min-release-age=0",
                        ],
                    },
                },
            )

            installed = provider.install("gifsicle")

            assert installed is not None
            proc = installed.exec(cmd=("--version",), quiet=True)
            assert proc.returncode != 0

    def test_install_root_alias_installs_into_the_requested_prefix(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "npm-root"
            provider = NpmProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            installed = provider.install("zx")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            bin_dir = provider.bin_dir
            assert bin_dir is not None
            assert provider.install_root == install_root
            assert bin_dir == install_root / "node_modules" / ".bin"
            assert bin_dir.exists()
            assert installed.loaded_abspath.parent == bin_dir
            # POSIX writes ``bin_dir/zx`` while Windows writes the
            # ``bin_dir/zx.CMD`` launcher — compare ``.stem`` so both
            # layouts pass.
            assert installed.loaded_abspath.stem == "zx"

    def test_explicit_prefix_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = NpmProvider(
                install_root=temp_dir_path / "ambient-npm",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"zx": {"install_args": ["zx@7.2.3"]}},
            )
            ambient_installed = ambient_provider.install(
                "zx",
                min_version=SemVer("1.0.0"),
            )
            assert ambient_installed is not None

            install_root = temp_dir_path / "npm-root"
            provider = NpmProvider(
                PATH=str(ambient_provider.bin_dir),
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("zx", min_version=SemVer("8.8.0"))

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            bin_dir = provider.bin_dir
            assert bin_dir is not None
            assert provider.install_root == install_root
            assert bin_dir == install_root / "node_modules" / ".bin"
            assert bin_dir.exists()
            assert installed.loaded_abspath.parent == bin_dir
            # POSIX writes ``bin_dir/zx`` while Windows writes the
            # ``bin_dir/zx.CMD`` launcher — compare ``.stem`` so both
            # layouts pass.
            assert installed.loaded_abspath.stem == "zx"
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_setup_falls_back_to_no_cache_when_cache_dir_is_not_a_directory(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            provider = NpmProvider(
                install_root=tmp_path / "npm",
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("zx")
            assert provider.cache_dir.is_dir()
            test_machine.assert_shallow_binary_loaded(installed)

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NpmProvider(
                install_root=Path(temp_dir) / "npm",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="zx")

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            npm_prefix = Path(tmpdir) / "npm"
            old_provider = NpmProvider(
                install_root=npm_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"zx": {"install_args": ["zx@7.2.3"]}},
            )
            old_installed = old_provider.install("zx", min_version=SemVer("1.0.0"))
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("7.2.3")

            upgraded = NpmProvider(
                install_root=npm_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            ).install("zx", min_version=SemVer("8.8.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("8.8.0"),
            )

            with pytest.raises(Exception):
                NpmProvider(
                    install_root=npm_prefix,
                    postinstall_scripts=True,
                    min_release_age=0,
                ).update("zx", min_version=SemVer("999.0.0"))

    def test_provider_defaults_and_binary_overrides_enforce_min_release_age(
        self,
        test_machine,
        caplog,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = NpmProvider(
                install_root=Path(tmpdir) / "strict-npm",
                postinstall_scripts=True,
                min_release_age=36500,
            )
            if strict_provider.supports_min_release_age("install"):
                with pytest.raises(Exception):
                    strict_provider.install("zx")
                test_machine.assert_provider_missing(strict_provider, "zx")
            else:
                direct_default = strict_provider.install("zx")
                test_machine.assert_shallow_binary_loaded(direct_default)
                assert (
                    "ignoring unsupported min_release_age=36500.0 for provider npm"
                    in caplog.text
                )
                assert strict_provider.uninstall("zx")

            direct_override = strict_provider.install("zx", min_release_age=0)
            test_machine.assert_shallow_binary_loaded(direct_override)
            assert strict_provider.uninstall("zx", min_release_age=0)

            binary = Binary(
                name="zx",
                binproviders=[
                    NpmProvider(
                        install_root=Path(tmpdir) / "binary-npm",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

    def test_provider_defaults_and_binary_overrides_enforce_postinstall_scripts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = NpmProvider(
                install_root=Path(tmpdir) / "strict-npm",
                postinstall_scripts=False,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"optipng": {"install_args": ["optipng-bin"]}},
            )
            strict_installed = strict_provider.install("optipng")
            assert strict_installed is not None
            assert strict_installed.loaded_abspath is not None
            strict_proc = strict_installed.exec(cmd=("--version",), quiet=True)
            assert strict_proc.returncode != 0

            direct_override = strict_provider.install(
                "optipng",
                postinstall_scripts=True,
            )
            assert direct_override is not None
            assert direct_override.loaded_abspath is not None
            assert strict_provider.uninstall("optipng", postinstall_scripts=True)

            binary = Binary(
                name="optipng",
                binproviders=[
                    NpmProvider(
                        install_root=Path(tmpdir) / "binary-npm",
                        postinstall_scripts=False,
                        min_release_age=0,
                    ).get_provider_with_overrides(
                        overrides={"optipng": {"install_args": ["optipng-bin"]}},
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            assert installed is not None
            assert installed.loaded_abspath is not None

            failing_binary = Binary(
                name="optipng",
                binproviders=[
                    NpmProvider(
                        install_root=Path(tmpdir) / "failing-npm",
                        postinstall_scripts=False,
                        min_release_age=0,
                    ).get_provider_with_overrides(
                        overrides={"optipng": {"install_args": ["optipng-bin"]}},
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=0,
            )
            failing_installed = failing_binary.install()
            assert failing_installed is not None
            failing_proc = failing_installed.exec(cmd=("--version",), quiet=True)
            assert failing_proc.returncode != 0

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="zx",
                binproviders=[
                    NpmProvider(
                        install_root=Path(temp_dir) / "npm",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_zx(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NpmProvider(
                install_root=Path(temp_dir) / "npm",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="zx")

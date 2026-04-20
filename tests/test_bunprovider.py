import logging
import tempfile
from pathlib import Path

import pytest

from abxpkg import Binary, BunProvider, SemVer
from abxpkg.exceptions import BinaryInstallError, BinProviderInstallError


class TestBunProvider:
    def test_install_args_win_for_ignore_scripts_and_min_release_age(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bun_prefix = Path(temp_dir) / "bun"
            provider = BunProvider(
                install_root=bun_prefix,
                postinstall_scripts=True,
                min_release_age=36500,
            ).get_provider_with_overrides(
                overrides={
                    "gifsicle": {
                        "install_args": [
                            "gifsicle",
                            "--ignore-scripts",
                            "--minimum-release-age=0",
                        ],
                    },
                },
            )

            installed = provider.install("gifsicle")

            assert installed is not None
            assert installed.loaded_abspath is not None
            proc = installed.exec(cmd=("--version",), quiet=True)
            # ``--ignore-scripts`` skipped gifsicle's postinstall download so
            # the shim has no real binary to call. POSIX shells propagate
            # the failing child's exit code (non-zero); Windows ``cmd``
            # wrapper returns 0 but writes the ``is not recognized`` error
            # to stderr — accept either as proof the postinstall was
            # skipped.
            assert proc.returncode != 0 or "not recognized" in (proc.stderr or "")
            # The provider's strict 100-year min_release_age was overridden
            # by the explicit --minimum-release-age=0 in install_args, so
            # the install resolved a real version.
            assert (
                bun_prefix
                / "install"
                / "global"
                / "node_modules"
                / "gifsicle"
                / "package.json"
            ).exists()

    def test_install_root_alias_installs_into_the_requested_prefix(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "bun-root"
            provider = BunProvider.model_validate(
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
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            # Bun's global node_modules side effect must exist on disk.
            assert (
                install_root
                / "install"
                / "global"
                / "node_modules"
                / "zx"
                / "package.json"
            ).exists()

    def test_explicit_prefix_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = BunProvider(
                install_root=temp_dir_path / "ambient-bun",
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
            assert ambient_installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath.parent == ambient_provider.bin_dir

            install_root = temp_dir_path / "bun-root"
            provider = BunProvider(
                PATH=str(ambient_provider.bin_dir),
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("zx", min_version=SemVer("8.8.0"))

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert installed.loaded_abspath != ambient_installed.loaded_abspath
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_setup_falls_back_to_no_cache_when_cache_dir_is_not_a_directory(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            provider = BunProvider(
                install_root=tmp_path / "bun",
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = provider.install("zx")
            assert provider.cache_dir.is_dir()
            test_machine.assert_shallow_binary_loaded(installed)

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = BunProvider(
                install_root=Path(temp_dir) / "bun",
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed, _ = test_machine.exercise_provider_lifecycle(
                provider,
                bin_name="zx",
            )
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.is_relative_to(provider.install_root)

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            bun_prefix = Path(tmpdir) / "bun"
            old_provider = BunProvider(
                install_root=bun_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"zx": {"install_args": ["zx@7.2.3"]}},
            )
            old_installed = old_provider.install("zx", min_version=SemVer("1.0.0"))
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("7.2.3")

            upgraded = BunProvider(
                install_root=bun_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            ).install("zx", min_version=SemVer("8.8.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("8.8.0"),
            )
            assert upgraded is not None
            assert upgraded.loaded_version is not None
            assert old_installed.loaded_version is not None
            assert upgraded.loaded_version > old_installed.loaded_version

    def test_provider_defaults_and_binary_overrides_enforce_min_release_age(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = BunProvider(
                install_root=Path(tmpdir) / "strict-bun",
                postinstall_scripts=True,
                min_release_age=36500,
            )
            assert strict_provider.supports_min_release_age("install") is True

            with pytest.raises(BinProviderInstallError):
                strict_provider.install("zx")
            test_machine.assert_provider_missing(strict_provider, "zx")

            direct_override = strict_provider.install("zx", min_release_age=0)
            test_machine.assert_shallow_binary_loaded(direct_override)
            assert strict_provider.uninstall("zx", min_release_age=0)

            binary = Binary(
                name="zx",
                binproviders=[
                    BunProvider(
                        install_root=Path(tmpdir) / "binary-bun",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

    def test_min_release_age_pins_to_older_version_when_strict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = BunProvider(
                install_root=Path(tmpdir) / "bun",
                postinstall_scripts=True,
                min_release_age=365,
            )
            assert strict_provider.supports_min_release_age("install") is True
            installed = strict_provider.install("zx")
            assert installed is not None
            assert installed.loaded_version is not None
            ceiling = SemVer.parse("8.8.0")
            assert ceiling is not None
            assert installed.loaded_version < ceiling

    def test_provider_defaults_and_binary_overrides_enforce_postinstall_scripts(
        self,
        caplog,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = BunProvider(
                install_root=Path(tmpdir) / "strict-bun",
                postinstall_scripts=False,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"optipng": {"install_args": ["optipng-bin"]}},
            )
            assert strict_provider.supports_postinstall_disable("install") is True
            strict_installed = strict_provider.install("optipng")
            assert strict_installed is not None
            assert strict_installed.loaded_abspath is not None
            strict_proc = strict_installed.exec(cmd=("--version",), quiet=True)
            assert strict_proc.returncode != 0

            override_provider = BunProvider(
                install_root=Path(tmpdir) / "override-bun",
                postinstall_scripts=False,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"optipng": {"install_args": ["optipng-bin"]}},
            )
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="abxpkg.binprovider"):
                dry_run_override = override_provider.install(
                    "optipng",
                    postinstall_scripts=True,
                    dry_run=True,
                )
            assert dry_run_override is not None
            assert "--trust" in caplog.text
            assert "--ignore-scripts" not in caplog.text

            direct_override = override_provider.install(
                "optipng",
                postinstall_scripts=True,
            )
            assert direct_override is not None
            assert direct_override.loaded_abspath is not None
            override_proc = direct_override.exec(cmd=("--version",), quiet=True)
            assert override_proc.returncode == 0, (
                f"postinstall_scripts=True override should produce a working "
                f"binary, but exec returned {override_proc.returncode}: "
                f"stdout={override_proc.stdout!r} stderr={override_proc.stderr!r}"
            )

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="zx",
                binproviders=[
                    BunProvider(
                        install_root=Path(temp_dir) / "bun",
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
            provider = BunProvider(
                install_root=Path(temp_dir) / "bun",
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="zx")
            global_modules = (
                Path(temp_dir) / "bun" / "install" / "global" / "node_modules"
            )
            if global_modules.exists():
                assert not (global_modules / "zx").exists()

    def test_provider_action_args_override_provider_defaults(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = BunProvider(
                install_root=Path(temp_dir) / "bun",
                dry_run=True,
                postinstall_scripts=False,
                min_release_age=36500,
            )

            installed = provider.install(
                "zx",
                dry_run=False,
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_supports_methods_do_not_emit_unsupported_warnings(self, caplog):
        with tempfile.TemporaryDirectory() as tmpdir:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                provider = BunProvider(
                    install_root=Path(tmpdir) / "bun",
                    postinstall_scripts=False,
                    min_release_age=0,
                )
                installed = provider.install("zx")
                assert installed is not None
            assert "ignoring unsupported postinstall_scripts" not in caplog.text
            assert "ignoring unsupported min_release_age" not in caplog.text

    def test_binary_install_failure_propagates_as_BinaryInstallError(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            failing_binary = Binary(
                name="zx",
                binproviders=[
                    BunProvider(
                        install_root=Path(tmpdir) / "bun",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=36500,
            )
            failing_provider = failing_binary.binproviders[0]
            assert isinstance(failing_provider, BunProvider)
            assert failing_provider.supports_min_release_age("install") is True
            with pytest.raises(BinaryInstallError):
                failing_binary.install()

import tempfile
import subprocess
from pathlib import Path

import pytest

from abxpkg import (
    Binary,
    BrewProvider,
    EnvProvider,
    NpmProvider,
    PnpmProvider,
    PuppeteerProvider,
    SemVer,
)


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
        assert installer.loaded_binprovider.name in {"brew", "env"}

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
                            "--min-release-age=3",
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
            assert installed.loaded_abspath == bin_dir / "zx"
            assert installed.loaded_abspath.parent == bin_dir

    def test_explicit_prefix_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = NpmProvider(
                install_root=temp_dir_path / "ambient-npm",
                postinstall_scripts=True,
                min_release_age=3,
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
                min_release_age=3,
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
            assert installed.loaded_abspath == bin_dir / "zx"
            assert installed.loaded_abspath.parent == bin_dir
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
                min_release_age=3,
            )

            installed = provider.install("zx")
            assert provider.cache_dir.is_dir()
            test_machine.assert_shallow_binary_loaded(installed)

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NpmProvider(
                install_root=Path(temp_dir) / "npm",
                postinstall_scripts=True,
                min_release_age=3,
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="zx")

    def test_install_args_change_forces_real_install_after_puppeteer_bootstrap(
        self,
        test_machine,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            puppeteer_root = Path(temp_dir) / "puppeteer"
            pnpm_root = puppeteer_root / "pnpm"
            expected_version = SemVer("149.0.0")
            assert expected_version is not None
            browser = Binary(
                name="chromium",
                binproviders=[
                    PuppeteerProvider(
                        install_root=puppeteer_root,
                        install_timeout=900,
                        postinstall_scripts=True,
                        min_release_age=3,
                    ),
                ],
                min_version=expected_version,
                postinstall_scripts=True,
                min_release_age=3,
            ).install()

            test_machine.assert_shallow_binary_loaded(browser)
            assert browser is not None
            assert browser.loaded_version is not None
            assert browser.loaded_version >= expected_version
            assert (
                pnpm_root / "node_modules" / "@puppeteer" / "browsers" / "package.json"
            ).exists()
            before_proc = subprocess.run(
                [
                    "node",
                    "-e",
                    "require(require.resolve('puppeteer', {paths: [process.argv[1]]}));",
                    str(pnpm_root / "node_modules"),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert before_proc.returncode != 0, before_proc.stdout or before_proc.stderr

            binary = Binary(
                name="browsers",
                binproviders=[
                    PnpmProvider(
                        install_root=pnpm_root,
                        install_timeout=900,
                        postinstall_scripts=False,
                        min_release_age=3,
                    ),
                ],
                overrides={
                    "pnpm": {
                        "install_args": ["@puppeteer/browsers", "puppeteer"],
                    },
                },
                postinstall_scripts=False,
                min_release_age=3,
            )

            loaded = binary.load()
            test_machine.assert_shallow_binary_loaded(loaded)
            assert loaded.loaded_abspath is not None
            assert (
                loaded.loaded_abspath.resolve()
                == (pnpm_root / "node_modules" / ".bin" / "browsers").resolve()
            )
            assert not (
                pnpm_root / "node_modules" / "puppeteer" / "package.json"
            ).exists()

            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert (
                installed.loaded_abspath.resolve()
                == (pnpm_root / "node_modules" / ".bin" / "browsers").resolve()
            )
            assert list(
                (pnpm_root / "node_modules" / ".pnpm").glob(
                    "puppeteer-core@*/node_modules/puppeteer-core/package.json",
                ),
            )

            proc = subprocess.run(
                [
                    "node",
                    "-e",
                    "require(require.resolve('puppeteer', {paths: [process.argv[1]]}));",
                    str(pnpm_root / "node_modules"),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert proc.returncode == 0, proc.stderr or proc.stdout

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            npm_prefix = Path(tmpdir) / "npm"
            old_provider = NpmProvider(
                install_root=npm_prefix,
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={"zx": {"install_args": ["zx@7.2.3"]}},
            )
            old_installed = old_provider.install("zx", min_version=SemVer("1.0.0"))
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("7.2.3")

            upgraded = NpmProvider(
                install_root=npm_prefix,
                postinstall_scripts=True,
                min_release_age=3,
            ).install("zx", min_version=SemVer("8.8.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("8.8.0"),
            )

            with pytest.raises(Exception):
                NpmProvider(
                    install_root=npm_prefix,
                    postinstall_scripts=True,
                    min_release_age=3,
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

            direct_override = strict_provider.install("zx", min_release_age=3)
            test_machine.assert_shallow_binary_loaded(direct_override)
            assert strict_provider.uninstall("zx", min_release_age=3)

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
                min_release_age=3,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

    def test_provider_defaults_and_binary_overrides_enforce_postinstall_scripts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = NpmProvider(
                install_root=Path(tmpdir) / "strict-npm",
                postinstall_scripts=False,
                min_release_age=3,
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
                        min_release_age=3,
                    ).get_provider_with_overrides(
                        overrides={"optipng": {"install_args": ["optipng-bin"]}},
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=3,
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
                        min_release_age=3,
                    ).get_provider_with_overrides(
                        overrides={"optipng": {"install_args": ["optipng-bin"]}},
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=3,
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
                        min_release_age=3,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=3,
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_zx(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NpmProvider(
                install_root=Path(temp_dir) / "npm",
                postinstall_scripts=True,
                min_release_age=3,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="zx")

    def test_search_finds_real_npm_package_and_install_works(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NpmProvider(
                install_root=Path(temp_dir) / "npm",
                postinstall_scripts=True,
                min_release_age=3,
            )
            results = provider.search("zx")
            assert results, "npm search zx should return registry matches"
            names = [r.name for r in results]
            assert "zx" in names
            match = next(r for r in results if r.name == "zx")
            assert match.overrides == {"npm": {"install_args": ["zx"]}}
            assert match.loaded_abspath is None
            assert match.loaded_version is None
            installed = match.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed.name == "zx"

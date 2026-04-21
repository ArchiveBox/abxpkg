import tempfile
from pathlib import Path

from abxpkg import Binary, PuppeteerProvider
from abxpkg.windows_compat import IS_WINDOWS


PUPPETEER_CHROMEDRIVER_ARGS = ["chromedriver@stable"]


class TestPuppeteerProvider:
    def test_chrome_alias_installs_real_browser_binary(self, test_machine):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            puppeteer_root = Path(temp_dir) / "puppeteer-root"
            provider = PuppeteerProvider(
                install_root=puppeteer_root,
                bin_dir=puppeteer_root / "bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"chrome": {"install_args": ["chromium@latest"]}},
            )

            installed = provider.install("chrome", no_cache=True)
            assert installed is not None
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed.name == "chrome"
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.exists()
            assert "@latest" not in installed.name
            assert "@" not in installed.loaded_abspath.name
            bin_dir = provider.bin_dir
            cache_dir = provider.cache_dir
            assert bin_dir is not None
            assert cache_dir is not None
            assert installed.loaded_abspath.parent == bin_dir
            # On Windows ``link_binary`` appends the source's ``.exe``
            # suffix onto the managed shim name so ``PATHEXT``/
            # ``shutil.which`` can resolve it; compare suffix-agnostic.
            expected_shim = bin_dir / ("chrome.exe" if IS_WINDOWS else "chrome")
            assert installed.loaded_abspath == expected_shim
            assert (cache_dir / "chromium").exists()

            loaded = provider.load("chrome", no_cache=True)
            test_machine.assert_shallow_binary_loaded(loaded)
            assert loaded is not None
            assert loaded.loaded_abspath is not None
            assert loaded.loaded_abspath.resolve() == installed.loaded_abspath.resolve()

            loaded_or_installed = provider.install("chrome", no_cache=True)
            test_machine.assert_shallow_binary_loaded(loaded_or_installed)
            assert loaded_or_installed is not None
            assert loaded_or_installed.loaded_abspath is not None
            assert (
                loaded_or_installed.loaded_abspath.resolve()
                == installed.loaded_abspath.resolve()
            )

    def test_install_root_alias_without_explicit_bin_dir_uses_root_bin(
        self,
        test_machine,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "puppeteer-root"
            provider = PuppeteerProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "chromedriver": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS},
                },
            )

            installed = provider.install("chromedriver")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_install_root_and_bin_dir_aliases_install_into_the_requested_paths(
        self,
        test_machine,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "puppeteer-root"
            bin_dir = Path(temp_dir) / "custom-bin"
            provider = PuppeteerProvider.model_validate(
                {
                    "install_root": install_root,
                    "bin_dir": bin_dir,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "chromedriver": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS},
                },
            )

            installed = provider.install("chromedriver")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_explicit_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = PuppeteerProvider(
                install_root=temp_dir_path / "ambient-root",
                bin_dir=temp_dir_path / "ambient-root/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "chromedriver": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS},
                },
            )
            ambient_installed = ambient_provider.install("chromedriver")
            assert ambient_installed is not None

            provider = PuppeteerProvider(
                PATH=str(ambient_provider.bin_dir),
                install_root=temp_dir_path / "puppeteer-root",
                bin_dir=temp_dir_path / "custom-bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "chromedriver": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS},
                },
            )

            installed = provider.install("chromedriver")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert provider.bin_dir == temp_dir_path / "custom-bin"
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert ambient_installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath.parent == ambient_provider.bin_dir

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PuppeteerProvider(
                install_root=Path(temp_dir) / "puppeteer-root",
                bin_dir=Path(temp_dir) / "puppeteer-root/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "chromedriver": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS},
                },
            )

            test_machine.exercise_provider_lifecycle(provider, bin_name="chromedriver")

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="chromedriver",
                binproviders=[
                    PuppeteerProvider(
                        install_root=Path(temp_dir) / "puppeteer-root",
                        bin_dir=Path(temp_dir) / "puppeteer-root/bin",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                overrides={"puppeteer": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS}},
                postinstall_scripts=True,
                min_release_age=0,
            )

            test_machine.exercise_binary_lifecycle(binary)

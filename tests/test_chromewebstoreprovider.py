import json
import tempfile
from pathlib import Path

from abxpkg import Binary, ChromeWebstoreProvider


PACKAGED_CHROME_UTILS_PATH = (
    Path(__file__).resolve().parent.parent
    / "abxpkg"
    / "js"
    / "chrome"
    / "chrome_utils.js"
)
UBLOCK_WEBSTORE_ID = "ddkjiahejlhfcafbddmgiahcphecmpfh"


def assert_extension_binary_loaded(loaded) -> None:
    assert loaded is not None
    assert loaded.is_valid
    assert loaded.loaded_binprovider is not None
    assert loaded.loaded_binprovider.name == "chromewebstore"
    assert loaded.loaded_abspath is not None
    assert loaded.loaded_abspath.name == "manifest.json"
    assert loaded.loaded_abspath.exists()
    assert loaded.loaded_version is not None
    assert loaded.loaded_sha256 is not None

    manifest = json.loads(loaded.loaded_abspath.read_text(encoding="utf-8"))
    assert manifest["version"] == str(loaded.loaded_version)


class TestChromeWebstoreProvider:
    def test_install_root_alias_without_explicit_bin_dir_uses_root_extensions(
        self,
        test_machine,
    ):
        test_machine.require_tool("node")
        assert PACKAGED_CHROME_UTILS_PATH.exists(), PACKAGED_CHROME_UTILS_PATH

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "chromewebstore-root"
            provider = ChromeWebstoreProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "ublock": {
                        "install_args": [UBLOCK_WEBSTORE_ID, "--name=ublock"],
                    },
                },
            )

            installed = provider.install("ublock")

            assert_extension_binary_loaded(installed)
            assert installed is not None
            bin_dir = provider.bin_dir
            assert bin_dir is not None
            assert provider.install_root == install_root
            assert bin_dir == install_root / "extensions"
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.is_relative_to(bin_dir)

    def test_install_root_and_bin_dir_aliases_install_into_the_requested_paths(
        self,
        test_machine,
    ):
        test_machine.require_tool("node")
        assert PACKAGED_CHROME_UTILS_PATH.exists(), PACKAGED_CHROME_UTILS_PATH

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "chromewebstore-root"
            bin_dir = Path(temp_dir) / "custom-extensions"
            provider = ChromeWebstoreProvider.model_validate(
                {
                    "install_root": install_root,
                    "bin_dir": bin_dir,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "ublock": {
                        "install_args": [UBLOCK_WEBSTORE_ID, "--name=ublock"],
                    },
                },
            )

            installed = provider.install("ublock")

            assert_extension_binary_loaded(installed)
            assert installed is not None
            assert bin_dir is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.is_relative_to(bin_dir)

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("node")
        assert PACKAGED_CHROME_UTILS_PATH.exists(), PACKAGED_CHROME_UTILS_PATH

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = ChromeWebstoreProvider(
                install_root=Path(temp_dir) / "chromewebstore-root",
                bin_dir=Path(temp_dir) / "chromewebstore-root/extensions",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "ublock": {
                        "install_args": [UBLOCK_WEBSTORE_ID, "--name=ublock"],
                    },
                },
            )

            provider.setup(
                postinstall_scripts=True,
                min_release_age=0,
                min_version=None,
            )
            assert provider.load("ublock", quiet=True, no_cache=True) is None

            installed = provider.install("ublock", no_cache=True)
            assert_extension_binary_loaded(installed)

            loaded = provider.load("ublock", no_cache=True)
            assert_extension_binary_loaded(loaded)

            loaded_or_installed = provider.install("ublock", no_cache=True)
            assert_extension_binary_loaded(loaded_or_installed)

            updated = provider.update("ublock", no_cache=True)
            assert_extension_binary_loaded(updated)

            assert provider.uninstall("ublock", no_cache=True) is True
            assert provider.load("ublock", quiet=True, no_cache=True) is None

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("node")
        assert PACKAGED_CHROME_UTILS_PATH.exists(), PACKAGED_CHROME_UTILS_PATH

        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="ublock",
                binproviders=[
                    ChromeWebstoreProvider(
                        install_root=Path(temp_dir) / "chromewebstore-root",
                        bin_dir=Path(temp_dir) / "chromewebstore-root/extensions",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                overrides={
                    "chromewebstore": {
                        "install_args": [UBLOCK_WEBSTORE_ID, "--name=ublock"],
                    },
                },
                postinstall_scripts=True,
                min_release_age=0,
            )

            fresh = test_machine.unloaded_binary(binary)
            test_machine.assert_binary_missing(fresh)

            installed = fresh.install()
            assert_extension_binary_loaded(installed)

            loaded = test_machine.unloaded_binary(binary).load(no_cache=True)
            assert_extension_binary_loaded(loaded)

            loaded_or_installed = test_machine.unloaded_binary(binary).install(
                no_cache=True,
            )
            assert_extension_binary_loaded(loaded_or_installed)

            updated = installed.update()
            assert_extension_binary_loaded(updated)

            removed = updated.uninstall()
            assert not removed.is_valid
            assert removed.loaded_abspath is None
            assert removed.loaded_version is None
            assert removed.loaded_sha256 is None
            test_machine.assert_binary_missing(binary)

    def test_search_finds_real_chromewebstore_extension_by_id(self):
        # Search resolves a 32-char Chrome Web Store extension ID into
        # its canonical name by scraping the public detail page's
        # ``<title>`` tag. The Binary's ``.name`` is the stable webstore
        # ID (BinName-safe); the human extension name lives in
        # ``description`` and the ``--name=`` install_arg. Non-ID
        # queries return [] because the Web Store doesn't expose a JSON
        # search API for keyword lookups.
        results = ChromeWebstoreProvider().search(UBLOCK_WEBSTORE_ID)
        assert results, "chromewebstore search by id should resolve uBlock Origin Lite"
        assert len(results) == 1
        match = results[0]
        assert match.name == UBLOCK_WEBSTORE_ID
        assert "ublock" in match.description.lower()
        assert match.overrides == {
            "chromewebstore": {
                "install_args": [
                    UBLOCK_WEBSTORE_ID,
                    f"--name={match.description.split(' (')[0]}",
                ],
            },
        }
        assert ChromeWebstoreProvider().search("not-an-id") == []

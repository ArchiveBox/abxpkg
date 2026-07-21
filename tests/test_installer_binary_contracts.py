import inspect

from abxpkg import (
    CargoProvider,
    GemProvider,
    GoGetProvider,
    NixProvider,
    NpmProvider,
    PipProvider,
    PlaywrightProvider,
    PnpmProvider,
    PuppeteerProvider,
    PyinfraProvider,
)


def _installer_source(provider_cls) -> str:
    return inspect.getsource(provider_cls.INSTALLER_BINARY)


class TestInstallerBinaryContracts:
    def test_external_installer_providers_delegate_to_base_resolver(self):
        for provider_cls in (
            CargoProvider,
            GemProvider,
            GoGetProvider,
            NixProvider,
            NpmProvider,
            PipProvider,
            PyinfraProvider,
        ):
            assert "super().INSTALLER_BINARY" in _installer_source(provider_cls)

    def test_pnpm_provider_preserves_installer_source_ownership(self):
        provider = PnpmProvider(postinstall_scripts=True, min_release_age=3)
        abspath = provider.get_abspath("pnpm", quiet=True, no_cache=True)
        installer = provider.INSTALLER_BINARY(no_cache=True)

        assert abspath is not None
        assert installer.loaded_binprovider is not None
        assert installer.loaded_abspath == abspath
        if abspath.resolve().is_relative_to(
            provider._installer_provider_root().resolve(),
        ):
            assert installer.loaded_binprovider.name == "npm"
        else:
            assert installer.loaded_binprovider.name == "env"

    def test_browser_providers_do_not_delegate_to_base_resolver(self):
        for provider_cls in (PlaywrightProvider, PuppeteerProvider):
            assert "super().INSTALLER_BINARY" not in _installer_source(provider_cls)

    def test_puppeteer_release_age_support_matches_its_pnpm_bootstrap(self):
        puppeteer = PuppeteerProvider(postinstall_scripts=True, min_release_age=3)
        pnpm = PnpmProvider(postinstall_scripts=True, min_release_age=3)

        assert pnpm.supports_min_release_age("install") is True
        assert puppeteer.supports_min_release_age("install") is True
        assert puppeteer.supports_min_release_age("update") is True
        assert puppeteer.supports_min_release_age("uninstall") is False

    def test_browser_providers_check_shared_lib_dir_then_raise_for_setup_bootstrap(
        self,
    ):
        for provider_cls in (PlaywrightProvider, PuppeteerProvider):
            source = _installer_source(provider_cls)
            assert "BinProviderUnavailableError" in source
            assert "Path(lib_dir)" in source
            assert '/ "pnpm"' in source
            assert '/ "packages"' in source

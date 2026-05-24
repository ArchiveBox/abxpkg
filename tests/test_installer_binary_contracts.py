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
            PnpmProvider,
            PyinfraProvider,
        ):
            assert "super().INSTALLER_BINARY" in _installer_source(provider_cls)

    def test_browser_providers_do_not_delegate_to_base_resolver(self):
        for provider_cls in (PlaywrightProvider, PuppeteerProvider):
            assert "super().INSTALLER_BINARY" not in _installer_source(provider_cls)

    def test_browser_providers_check_shared_lib_dir_then_raise_for_setup_bootstrap(
        self,
    ):
        for provider_cls in (PlaywrightProvider, PuppeteerProvider):
            source = _installer_source(provider_cls)
            assert "BinProviderUnavailableError" in source
            assert 'Path(lib_dir) / "npm" / "node_modules" / ".bin"' in source

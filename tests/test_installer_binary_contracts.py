from pathlib import Path

import pytest

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
from abxpkg.exceptions import BinProviderUnavailableError


class TestInstallerBinaryContracts:
    @pytest.mark.parametrize(
        ("provider_cls", "version_args"),
        (
            (CargoProvider, ("--version",)),
            (GemProvider, ("--version",)),
            (GoGetProvider, ("version",)),
            (NixProvider, ("--version",)),
            (NpmProvider, ("--version",)),
            (PipProvider, ("--version",)),
            (PyinfraProvider, ("--version",)),
        ),
    )
    def test_external_installer_providers_resolve_real_installer(
        self,
        test_machine,
        provider_cls,
        version_args: tuple[str, ...],
    ):
        provider = provider_cls(postinstall_scripts=True, min_release_age=3)
        installer = provider.INSTALLER_BINARY(no_cache=True)

        test_machine.assert_shallow_binary_loaded(
            installer,
            version_args=version_args,
            assert_version_command=True,
        )
        assert installer.name == provider.INSTALLER_BIN
        assert installer.loaded_abspath == provider.get_abspath(
            provider.INSTALLER_BIN,
            quiet=True,
            no_cache=True,
        )

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

    def test_puppeteer_release_age_support_matches_its_pnpm_bootstrap(self):
        puppeteer = PuppeteerProvider(postinstall_scripts=True, min_release_age=3)
        pnpm = PnpmProvider(postinstall_scripts=True, min_release_age=3)

        assert pnpm.supports_min_release_age("install") is True
        assert puppeteer.supports_min_release_age("install") is True
        assert puppeteer.supports_min_release_age("update") is True
        assert puppeteer.supports_min_release_age("uninstall") is False

    @pytest.mark.parametrize(
        ("provider_cls", "package_name", "installer_name"),
        (
            (PlaywrightProvider, "playwright", "playwright"),
            (PuppeteerProvider, "puppeteer", "browsers"),
        ),
    )
    def test_browser_installer_resolution_requires_its_managed_package_cli(
        self,
        tmp_path: Path,
        provider_cls,
        package_name: str,
        installer_name: str,
    ):
        lib_dir = tmp_path / "lib"
        install_root = lib_dir / package_name
        expected_cli = (
            lib_dir
            / "pnpm"
            / "packages"
            / package_name
            / "node_modules"
            / ".bin"
            / installer_name
        )
        provider = provider_cls(install_root=install_root)

        assert not expected_cli.exists()
        with pytest.raises(BinProviderUnavailableError) as exc_info:
            provider.INSTALLER_BINARY(no_cache=True)

        assert exc_info.value.provider_name == provider_cls.__name__
        assert exc_info.value.installer_bin == provider.INSTALLER_BIN
        assert provider._INSTALLER_BINARY is None

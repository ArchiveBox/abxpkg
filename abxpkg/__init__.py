__package__ = "abxpkg"

from .base_types import (
    BinName,
    InstallArgs,
    PATHStr,
    HostBinPath,
    HostExistsPath,
    BinDirPath,
    BinProviderName,
    bin_name,
    bin_abspath,
    bin_abspaths,
    func_takes_args_or_kwargs,
)
from .semver import SemVer, bin_version
from .shallowbinary import ShallowBinary
from .logging import (
    logger,
    get_logger,
    configure_logging,
    configure_rich_logging,
    RICH_INSTALLED,
)
from .exceptions import (
    ABXPkgError,
    BinaryOperationError,
    BinaryInstallError,
    BinaryLoadError,
    BinaryUpdateError,
    BinaryUninstallError,
)
from .binprovider import (
    BinProvider,
    EnvProvider,
    OPERATING_SYSTEM,
    DEFAULT_PATH,
    DEFAULT_ENV_PATH,
    PYTHON_BIN_DIR,
    BinProviderOverrides,
    BinaryOverrides,
    ProviderFuncReturnValue,
    HandlerType,
    HandlerValue,
    HandlerDict,
    HandlerReturnValue,
)
from .binary import Binary

from .binprovider_apt import AptProvider
from .binprovider_brew import BrewProvider
from .binprovider_cargo import CargoProvider
from .binprovider_gem import GemProvider
from .binprovider_goget import GoGetProvider
from .binprovider_nix import NixProvider
from .binprovider_docker import DockerProvider
from .binprovider_pip import PipProvider
from .binprovider_uv import UvProvider
from .binprovider_npm import NpmProvider
from .binprovider_pnpm import PnpmProvider
from .binprovider_yarn import YarnProvider
from .binprovider_bun import BunProvider
from .binprovider_deno import DenoProvider
from .binprovider_ansible import AnsibleProvider
from .binprovider_pyinfra import PyinfraProvider
from .binprovider_chromewebstore import ChromeWebstoreProvider
from .binprovider_puppeteer import PuppeteerProvider
from .binprovider_playwright import PlaywrightProvider
from .binprovider_bash import BashProvider
from .binprovider_scoop import ScoopProvider
from .windows_compat import IS_WINDOWS, UNIX_ONLY_PROVIDER_NAMES

ALL_PROVIDERS = [
    EnvProvider,
    UvProvider,
    PnpmProvider,
    PuppeteerProvider,
    GemProvider,
    GoGetProvider,
    CargoProvider,
    BrewProvider,
    PlaywrightProvider,
    AptProvider,
    NixProvider,
    DockerProvider,
    PipProvider,
    NpmProvider,
    BunProvider,
    YarnProvider,
    DenoProvider,
    AnsibleProvider,
    PyinfraProvider,
    ChromeWebstoreProvider,
    BashProvider,
    ScoopProvider,
]


def _provider_class(provider: type[BinProvider] | BinProvider) -> type[BinProvider]:
    return provider if isinstance(provider, type) else type(provider)


ALL_PROVIDER_NAMES = [
    _provider_class(provider).model_fields["name"].default for provider in ALL_PROVIDERS
]  # pip, apt, brew, etc.
ALL_PROVIDER_CLASS_NAMES = [
    _provider_class(provider).__name__ for provider in ALL_PROVIDERS
]  # PipProvider, AptProvider, BrewProvider, etc.


# Default provider names: names of providers that are enabled by default based on the current OS.
# On Windows we also drop everything in ``UNIX_ONLY_PROVIDER_NAMES`` (apt,
# brew, nix, bash, ansible, pyinfra, docker) since none of them have a
# working Windows backend, and we drop ``scoop`` on non-Windows hosts
# since it's Windows-only.
DEFAULT_PROVIDER_NAMES = [
    provider_name
    for provider_name in ALL_PROVIDER_NAMES
    if not (OPERATING_SYSTEM == "darwin" and provider_name == "apt")
    and provider_name not in ("ansible", "pyinfra")
    and not (IS_WINDOWS and provider_name in UNIX_ONLY_PROVIDER_NAMES)
    and not (not IS_WINDOWS and provider_name == "scoop")
]

# Lazy provider singletons: maps provider name -> class
# e.g. 'apt' -> AptProvider, 'pip' -> PipProvider, 'env' -> EnvProvider
PROVIDER_CLASS_BY_NAME = {
    _provider_class(provider).model_fields["name"].default: _provider_class(provider)
    for provider in ALL_PROVIDERS
}
_provider_singletons: dict = {}


# Lazy provider singletons: maps provider name -> class
# e.g. 'apt' -> AptProvider, 'pip' -> PipProvider, 'env' -> EnvProvider
# This is a lazy singleton pattern that allows us to instantiate providers
# only when they are needed, and not when the module is imported.
def __getattr__(name: str):
    if name in PROVIDER_CLASS_BY_NAME:
        if name not in _provider_singletons:
            _provider_singletons[name] = PROVIDER_CLASS_BY_NAME[name]()
        return _provider_singletons[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Main types
    "BinProvider",
    "Binary",
    "SemVer",
    "ShallowBinary",
    "logger",
    "get_logger",
    "configure_logging",
    "configure_rich_logging",
    "RICH_INSTALLED",
    # Exceptions
    "ABXPkgError",
    "BinaryOperationError",
    "BinaryInstallError",
    "BinaryLoadError",
    "BinaryUpdateError",
    "BinaryUninstallError",
    # Helper Types
    "BinName",
    "InstallArgs",
    "PATHStr",
    "BinDirPath",
    "HostBinPath",
    "HostExistsPath",
    "BinProviderName",
    # Override types
    "BinProviderOverrides",
    "BinaryOverrides",
    "ProviderFuncReturnValue",
    "HandlerType",
    "HandlerValue",
    "HandlerDict",
    "HandlerReturnValue",
    # Validator Functions
    "bin_version",
    "bin_name",
    "bin_abspath",
    "bin_abspaths",
    "func_takes_args_or_kwargs",
    # Globals
    "OPERATING_SYSTEM",
    "DEFAULT_PATH",
    "DEFAULT_ENV_PATH",
    "PYTHON_BIN_DIR",
    "PROVIDER_CLASS_BY_NAME",
    "ALL_PROVIDER_NAMES",
    "DEFAULT_PROVIDER_NAMES",
    # BinProviders (classes)
    "EnvProvider",
    "AptProvider",
    "BrewProvider",
    "CargoProvider",
    "GemProvider",
    "GoGetProvider",
    "NixProvider",
    "DockerProvider",
    "PipProvider",
    "UvProvider",
    "NpmProvider",
    "PnpmProvider",
    "YarnProvider",
    "BunProvider",
    "DenoProvider",
    "AnsibleProvider",
    "PyinfraProvider",
    "ChromeWebstoreProvider",
    "PuppeteerProvider",
    "PlaywrightProvider",
    "BashProvider",
    "ScoopProvider",
    # Note: provider singleton names (apt, pip, brew, etc.) are intentionally
    # excluded from __all__ so that `from abxpkg import *` does not eagerly
    # instantiate every provider. Use explicit imports instead:
    #   from abxpkg import apt, pip, brew, npm, pnpm, yarn, bun, deno
]

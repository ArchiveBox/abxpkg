from __future__ import annotations

__package__ = "abxpkg"

import os
from collections.abc import Iterator
from typing import Any


_PACKAGE_DIR = os.path.dirname(__file__)
# Pydantic auto-loads third-party plugins from the active Python environment.
# In CLI/shebang mode that can pull unrelated packages like logfire into every
# model import, adding hundreds of milliseconds before abxpkg touches its own
# cache. abxpkg does not rely on those plugins, so keep model construction local.
os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "1")
_PROVIDER_MODULE_NAMES_CACHE = None
_EXPORT_MODULES_BY_NAME_CACHE = None
_PROVIDER_CLASS_NAMES_BY_NAME_CACHE = None
_ALL_PUBLIC_EXPORT_NAMES_CACHE = None

_CORE_EXPORT_MODULES = (
    "base_types",
    "semver",
    "logging",
    "exceptions",
    "binprovider",
    "binary",
)

# This is provider precedence, not an export registry. Exported symbols are
# discovered from the modules themselves so adding/removing public classes or
# helpers does not require keeping a second hand-written symbol table in sync.
_PROVIDER_NAME_PRIORITY = (
    "env",
    "uv",
    "pnpm",
    "puppeteer",
    "gem",
    "goget",
    "cargo",
    "brew",
    "playwright",
    "apt",
    "nix",
    "docker",
    "pip",
    "npm",
    "bun",
    "yarn",
    "deno",
    "ansible",
    "pyinfra",
    "chromewebstore",
    "bash",
)

_provider_singletons: dict[str, object] = {}


def _provider_class(provider):
    return provider if isinstance(provider, type) else type(provider)


def _module_path(module_name: str) -> str:
    return os.path.join(_PACKAGE_DIR, f"{module_name}.py")


def _provider_module_names() -> tuple[str, ...]:
    global _PROVIDER_MODULE_NAMES_CACHE
    if _PROVIDER_MODULE_NAMES_CACHE is not None:
        return _PROVIDER_MODULE_NAMES_CACHE
    discovered = {
        filename[:-3]
        for filename in os.listdir(_PACKAGE_DIR)
        if filename.startswith("binprovider_") and filename.endswith(".py")
    }
    priority_modules = tuple(
        f"binprovider_{provider_name}"
        for provider_name in _PROVIDER_NAME_PRIORITY
        if f"binprovider_{provider_name}" in discovered
    )
    _PROVIDER_MODULE_NAMES_CACHE = (
        "binprovider",
        *priority_modules,
        *tuple(sorted(discovered - set(priority_modules))),
    )
    return _PROVIDER_MODULE_NAMES_CACHE


def _public_names_in_module(module_name: str) -> tuple[str, ...]:
    names: list[str] = []
    try:
        with open(_module_path(module_name), encoding="utf-8") as source_file:
            for raw_line in source_file:
                if not raw_line or raw_line[0].isspace():
                    continue
                line = raw_line.split("#", 1)[0].strip()
                if line.startswith(("class ", "def ")):
                    name = line.split(None, 1)[1].split("(", 1)[0].split(":", 1)[0]
                elif "=" in line:
                    name = line.split("=", 1)[0].strip()
                    if ":" in name:
                        name = name.split(":", 1)[0].strip()
                else:
                    continue
                if name.isidentifier() and not name.startswith("_"):
                    names.append(name)
    except OSError:
        return ()
    return tuple(dict.fromkeys(names))


def _export_modules_by_name() -> dict[str, str]:
    global _EXPORT_MODULES_BY_NAME_CACHE
    if _EXPORT_MODULES_BY_NAME_CACHE is not None:
        return _EXPORT_MODULES_BY_NAME_CACHE
    exports: dict[str, str] = {}
    for module_name in (*_CORE_EXPORT_MODULES, *_provider_module_names()):
        for export_name in _public_names_in_module(module_name):
            exports.setdefault(export_name, module_name)
    _EXPORT_MODULES_BY_NAME_CACHE = exports
    return _EXPORT_MODULES_BY_NAME_CACHE


def _import_module(module_name: str):
    import importlib

    return importlib.import_module(f".{module_name}", __name__)


def _load_export(name: str):
    module_name = _export_modules_by_name().get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_import_module(module_name), name)
    globals()[name] = value
    return value


def _provider_name_from_class_name(class_name: str) -> str:
    return class_name.removesuffix("Provider").lower()


def _provider_class_names_by_name() -> dict[str, tuple[str, str]]:
    global _PROVIDER_CLASS_NAMES_BY_NAME_CACHE
    if _PROVIDER_CLASS_NAMES_BY_NAME_CACHE is not None:
        return _PROVIDER_CLASS_NAMES_BY_NAME_CACHE
    provider_classes: dict[str, tuple[str, str]] = {}
    for module_name in _provider_module_names():
        for class_name in _public_names_in_module(module_name):
            if not class_name.endswith("Provider") or class_name == "BinProvider":
                continue
            provider_classes[_provider_name_from_class_name(class_name)] = (
                module_name,
                class_name,
            )
    priority = {
        provider_name: provider_classes[provider_name]
        for provider_name in _PROVIDER_NAME_PRIORITY
        if provider_name in provider_classes
    }
    for provider_name in sorted(provider_classes):
        priority.setdefault(provider_name, provider_classes[provider_name])
    _PROVIDER_CLASS_NAMES_BY_NAME_CACHE = priority
    return _PROVIDER_CLASS_NAMES_BY_NAME_CACHE


class ProviderClassByName(dict):
    """Mapping that preserves the public dict API while importing lazily.

    Provider modules are relatively expensive because some wrap external
    package managers. Script execution frequently needs only a few named
    providers, so resolving all classes just to validate/index one provider
    burns the hot-path cache win before provider caches are even consulted.
    """

    def __init__(self) -> None:
        super().__init__()
        self._class_names = _provider_class_names_by_name()

    def __missing__(self, provider_name: str):
        module_name, class_name = self._class_names[provider_name]
        provider_class = getattr(_import_module(module_name), class_name)
        self[provider_name] = provider_class
        return provider_class

    def __contains__(self, provider_name: object) -> bool:
        return provider_name in self._class_names

    def __iter__(self) -> Iterator[str]:
        return iter(self._class_names)

    def __len__(self) -> int:
        return len(self._class_names)

    def keys(self):
        return self._class_names.keys()

    def _load_all(self) -> None:
        for provider_name in self._class_names:
            self[provider_name]

    def items(self):
        self._load_all()
        return super().items()

    def values(self):
        self._load_all()
        return super().values()

    def get(self, provider_name: object, default: Any = None) -> Any:
        if provider_name not in self._class_names:
            return default
        return self[provider_name]


def _all_providers():
    providers = []
    for module_name, class_name in _provider_class_names_by_name().values():
        providers.append(getattr(_import_module(module_name), class_name))
    return providers


def _all_provider_names() -> list[str]:
    return list(_provider_class_names_by_name())


def _all_provider_class_names() -> list[str]:
    return [class_name for _, class_name in _provider_class_names_by_name().values()]


def _default_provider_names() -> list[str]:
    import platform

    operating_system = platform.system().lower()
    return [
        provider_name
        for provider_name in _all_provider_names()
        if not (operating_system == "darwin" and provider_name == "apt")
        and provider_name not in ("ansible", "pyinfra")
    ]


def _provider_class_by_name():
    return ProviderClassByName()


_COMPUTED_EXPORTS = {
    "ALL_PROVIDERS": _all_providers,
    "ALL_PROVIDER_NAMES": _all_provider_names,
    "ALL_PROVIDER_CLASS_NAMES": _all_provider_class_names,
    "DEFAULT_PROVIDER_NAMES": _default_provider_names,
    "PROVIDER_CLASS_BY_NAME": _provider_class_by_name,
}


def __getattr__(name: str) -> Any:
    if name in _COMPUTED_EXPORTS:
        value = _COMPUTED_EXPORTS[name]()
        globals()[name] = value
        return value
    if name in _provider_class_names_by_name():
        if name not in _provider_singletons:
            _provider_singletons[name] = _provider_class_by_name()[name]()
        return _provider_singletons[name]
    return _load_export(name)


def _all_public_export_names() -> tuple[str, ...]:
    global _ALL_PUBLIC_EXPORT_NAMES_CACHE
    if _ALL_PUBLIC_EXPORT_NAMES_CACHE is not None:
        return _ALL_PUBLIC_EXPORT_NAMES_CACHE
    names = [
        *_COMPUTED_EXPORTS,
        *_export_modules_by_name(),
    ]
    _ALL_PUBLIC_EXPORT_NAMES_CACHE = tuple(dict.fromkeys(names))
    return _ALL_PUBLIC_EXPORT_NAMES_CACHE


__all__ = list(_all_public_export_names())

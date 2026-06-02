__package__ = "abxpkg"

from typing import Any
from typing import Self

from pydantic import (
    Field,
    model_validator,
    computed_field,
    field_validator,
    validate_call,
    field_serializer,
    ConfigDict,
    InstanceOf,
)

from .semver import SemVer
from .shallowbinary import ShallowBinary
from .binprovider import BinProvider, EnvProvider, BinaryOverrides
from .logging import (
    format_exception_with_output,
    format_raised_exception,
    get_logger,
    log_method_call,
)
from .exceptions import (
    BinaryInstallError,
    BinaryLoadError,
    BinaryUpdateError,
    BinaryUninstallError,
)
from .base_types import (
    BinName,
    bin_abspath,
    bin_abspaths,
    HostBinPath,
    BinProviderName,
    PATHStr,
)

DEFAULT_PROVIDER = EnvProvider()
logger = get_logger(__name__)


class Binary(ShallowBinary):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        validate_default=True,
        validate_assignment=True,
        from_attributes=True,
        revalidate_instances="always",
        arbitrary_types_allowed=True,
    )

    name: BinName = ""
    description: str = ""

    binproviders: list[InstanceOf[BinProvider]] = Field(  # ty: ignore[invalid-assignment] https://github.com/astral-sh/ty/issues/2403
        default_factory=lambda: [DEFAULT_PROVIDER],
    )
    overrides: BinaryOverrides = Field(default_factory=dict)

    min_version: SemVer | None = None

    postinstall_scripts: bool | None = Field(
        default=None,
        description=(
            "Allow post-install scripts during package installation. "
            "Defaults to the selected provider's configured behavior if unset."
        ),
    )
    min_release_age: float | None = Field(
        default=None,
        description=(
            "Minimum days since publication before a package can be installed. "
            "Defaults to the selected provider's configured behavior if unset."
        ),
    )

    # bin_filename:  see below
    # is_executable: see below
    # is_script
    # is_valid: see below

    @model_validator(mode="after")
    def validate_model(self) -> Self:
        # assert self.name, 'Binary.name must not be empty'
        # self.description = self.description or self.name

        assert self.binproviders, f"No providers were given for package {self.name}"

        # pull in any overrides from the binproviders
        for binprovider in self.binproviders:
            overrides_for_bin = binprovider.overrides.get(self.name, {})
            if overrides_for_bin:
                self.overrides[binprovider.name] = {
                    **overrides_for_bin,
                    **self.overrides.get(binprovider.name, {}),
                }

        explicit_fields = self.model_fields_set
        if "postinstall_scripts" not in explicit_fields:
            provider_values = [
                provider.postinstall_scripts for provider in self.binproviders
            ]
            if len(provider_values) == len(self.binproviders) and all(
                value == provider_values[0] for value in provider_values
            ):
                self.postinstall_scripts = provider_values[0]
        if "min_release_age" not in explicit_fields:
            provider_values = [
                provider.min_release_age for provider in self.binproviders
            ]
            if len(provider_values) == len(self.binproviders) and all(
                value == provider_values[0] for value in provider_values
            ):
                self.min_release_age = provider_values[0]
        return self

    @field_validator("loaded_abspath", mode="before")
    def parse_abspath(cls, value: Any) -> HostBinPath | None:
        return bin_abspath(value) if value else None

    @field_validator("loaded_version", mode="before")
    def parse_version(cls, value: Any) -> SemVer | None:
        return SemVer(value) if value else None

    @field_validator("min_version", mode="before")
    def parse_min_version(cls, value: Any) -> SemVer | None:
        # Preserve the semantic difference between "no version floor" and an
        # actual minimum version. `None` means any discovered version is
        # acceptable; non-empty values get normalized into SemVer.
        return SemVer(value) if value else None

    @field_serializer("overrides", when_used="json")
    def serialize_overrides(
        self,
        overrides: BinaryOverrides,
    ) -> dict[BinProviderName, dict[str, Any]]:
        def serialize_value(value: Any) -> Any:
            if value is None or isinstance(value, str | int | float | bool):
                return value
            if isinstance(value, SemVer):
                return str(value)
            if isinstance(value, dict):
                return {
                    str(key): serialize_value(nested_value)
                    for key, nested_value in value.items()
                }
            if isinstance(value, list | tuple):
                return [serialize_value(item) for item in value]
            return str(value)

        return {
            binprovider_name: {
                override_key: serialize_value(override_value)
                for override_key, override_value in binprovider_overrides.items()
            }
            for binprovider_name, binprovider_overrides in overrides.items()
        }

    @computed_field
    @property
    def loaded_abspaths(self) -> dict[BinProviderName, list[HostBinPath]]:
        if not self.loaded_abspath:
            # binary has not been loaded yet
            return {}

        all_bin_abspaths = (
            {self.loaded_binprovider.name: [self.loaded_abspath]}
            if self.loaded_binprovider
            else {}
        )
        for binprovider in self.binproviders:
            if not binprovider.PATH:
                # print('skipping provider', binprovider.name, binprovider.PATH)
                continue
            for abspath in bin_abspaths(self.name, PATH=binprovider.PATH):
                existing = all_bin_abspaths.get(binprovider.name, [])
                if abspath not in existing:
                    all_bin_abspaths[binprovider.name] = [
                        *existing,
                        abspath,
                    ]
        return all_bin_abspaths

    @computed_field
    @property
    def loaded_bin_dirs(self) -> dict[BinProviderName, PATHStr]:
        return {
            provider_name: ":".join(
                [str(bin_abspath.parent) for bin_abspath in bin_abspaths],
            )
            for provider_name, bin_abspaths in self.loaded_abspaths.items()
        }

    @property
    def abspaths(self) -> dict[BinProviderName, list[HostBinPath]]:
        """Backward-compatible read alias for loaded_abspaths."""
        return self.loaded_abspaths

    @computed_field
    @property
    def python_name(self) -> str:
        return self.name.replace("-", "_").replace(".", "_")

    @computed_field
    @property
    def is_valid(self) -> bool:
        """Pure loaded-state check used by debug logging.

        Logging calls this while formatting return values, so it must stay fast
        and side-effect free. Keep overrides as cheap predicates only; never do
        resolution, subprocess work, cache mutation, or any other side effect.
        """
        if not (self.name and self.loaded_abspath and self.loaded_version):
            return False
        if self.min_version and self.loaded_version < self.min_version:
            return False
        return True

    @log_method_call()
    # @validate_call
    def get_binprovider(
        self,
        binprovider_name: BinProviderName,
        **extra_overrides,
    ) -> InstanceOf[BinProvider]:
        for binprovider in self.binproviders:
            if binprovider.name == binprovider_name:
                overrides_for_binprovider = {
                    self.name: self.overrides.get(binprovider_name, {}),
                }
                return binprovider.get_provider_with_overrides(
                    overrides=overrides_for_binprovider,
                    **extra_overrides,
                )

        raise KeyError(
            f"{binprovider_name} is not a supported BinProvider for Binary(name={self.name})",
        )

    def _debug_provider_failure(
        self,
        operation: str,
        provider: BinProvider,
        err: Exception,
    ) -> None:
        logger.debug("%s", format_raised_exception(err))

    def _binprovider_order(
        self,
        binproviders: list[BinProviderName] | None = None,
    ) -> list[BinProvider]:
        selected_providers: list[BinProvider] = []
        for binprovider in self.binproviders:
            if binproviders and binprovider.name not in binproviders:
                continue
            selected_providers.append(binprovider)

        cached_providers: list[BinProvider] = []
        uncached_providers: list[BinProvider] = []
        for binprovider in selected_providers:
            try:
                if binprovider.has_cached_binary(self.name):
                    cached_providers.append(binprovider)
                else:
                    uncached_providers.append(binprovider)
            except Exception:
                uncached_providers.append(binprovider)

        return [*cached_providers, *uncached_providers]

    def _validated_loaded_copy(
        self,
        provider: BinProvider,
        *,
        abspath: HostBinPath | None,
        version: SemVer | None,
        sha256: str | None,
        mtime: int | None,
        euid: int | None,
    ) -> Self:
        """Return a loaded copy and enforce the Binary-level min_version gate.

        Providers can legitimately resolve a binary that still fails this
        Binary's declared version floor. Keeping the final validation here makes
        install/load/update all share one consistent check.
        """
        result = self.model_copy(
            deep=True,
            update={
                "loaded_binprovider": provider,
                "loaded_abspath": abspath,
                "loaded_version": version,
                "loaded_sha256": sha256,
                "loaded_mtime": mtime,
                "loaded_euid": euid,
            },
        )
        if not result.is_valid:
            raise ValueError(
                f"{provider.name} resolved {self.name} with version {result.loaded_version} which does not satisfy min_version {self.min_version}",
            )
        return result

    @validate_call
    @log_method_call(include_result=True)
    def install(
        self,
        binproviders: list[BinProviderName] | None = None,
        no_cache: bool = False,
        dry_run: bool | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        **extra_overrides,
    ) -> Self:
        assert self.name, f"No binary name was provided! {self}"

        if self.is_valid and not no_cache:
            logger.debug(
                "Skipping install for %s because it is already valid",
                self.name,
            )
            return self

        if binproviders is not None and len(list(binproviders)) == 0:
            logger.debug(
                "Skipping install for %s because binproviders list was empty",
                self.name,
            )
            return self

        # logger.info("Installing %s binary", self.name)
        inner_exc: Exception | None = None
        errors = {}
        binary_postinstall_scripts = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        binary_min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        for binprovider in self.binproviders:
            if binproviders and (binprovider.name not in binproviders):
                continue

            provider = binprovider
            try:
                provider = self.get_binprovider(
                    binprovider_name=binprovider.name,
                    dry_run=dry_run,
                    **extra_overrides,
                )
                resolved_postinstall_scripts = (
                    provider.postinstall_scripts
                    if binary_postinstall_scripts is None
                    else binary_postinstall_scripts
                )
                resolved_min_release_age = (
                    provider.min_release_age
                    if binary_min_release_age is None
                    else binary_min_release_age
                )
                installed_bin = provider.install(
                    self.name,
                    no_cache=no_cache,
                    dry_run=dry_run,
                    postinstall_scripts=resolved_postinstall_scripts,
                    min_release_age=resolved_min_release_age,
                    min_version=self.min_version,
                )
                if installed_bin is not None and installed_bin.loaded_abspath:
                    # print('INSTALLED', self.name, installed_bin)
                    return self._validated_loaded_copy(
                        provider,
                        abspath=installed_bin.loaded_abspath,
                        version=installed_bin.loaded_version,
                        sha256=installed_bin.loaded_sha256,
                        mtime=installed_bin.loaded_mtime,
                        euid=installed_bin.loaded_euid,
                    )
            except Exception as err:
                inner_exc = err
                errors[binprovider.name] = format_exception_with_output(err)
                self._debug_provider_failure("install", provider, err)

        provider_names = ", ".join(
            binproviders or [p.name for p in self.binproviders],
        )
        raise BinaryInstallError(self.name, provider_names, errors) from inner_exc

    @validate_call
    @log_method_call(include_result=True)
    def load(
        self,
        binproviders: list[BinProviderName] | None = None,
        no_cache=False,
        **extra_overrides,
    ) -> Self:
        assert self.name, f"No binary name was provided! {self}"

        # if we're already loaded, skip loading
        if self.is_valid and not no_cache:
            logger.debug("Skipping load for %s because it is already valid", self.name)
            return self

        # if binproviders list is passed but it's empty, skip loading
        if binproviders is not None and len(list(binproviders)) == 0:
            logger.debug(
                "Skipping load for %s because binproviders list was empty",
                self.name,
            )
            return self

        # logger.info("Loading %s binary", self.name)
        inner_exc: Exception | None = None
        errors = {}
        for binprovider in self.binproviders:
            if binproviders and binprovider.name not in binproviders:
                continue

            provider = binprovider
            try:
                provider = self.get_binprovider(
                    binprovider_name=binprovider.name,
                    **extra_overrides,
                )
                installed_bin = provider.load(self.name, no_cache=no_cache)
                if installed_bin is not None and installed_bin.loaded_abspath:
                    # print('LOADED', binprovider, self.name, installed_bin)
                    return self._validated_loaded_copy(
                        provider,
                        abspath=installed_bin.loaded_abspath,
                        version=installed_bin.loaded_version,
                        sha256=installed_bin.loaded_sha256,
                        mtime=installed_bin.loaded_mtime,
                        euid=installed_bin.loaded_euid,
                    )
                else:
                    continue
            except Exception as err:
                inner_exc = err
                errors[binprovider.name] = format_exception_with_output(err)
                self._debug_provider_failure("load", provider, err)

        provider_names = ", ".join(
            binproviders or [p.name for p in self.binproviders],
        )
        raise BinaryLoadError(self.name, provider_names, errors) from inner_exc

    @validate_call
    @log_method_call(include_result=True)
    def update(
        self,
        binproviders: list[BinProviderName] | None = None,
        no_cache: bool = False,
        dry_run: bool | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        **extra_overrides,
    ) -> Self:
        assert self.name, f"No binary name was provided! {self}"

        if binproviders is not None and len(list(binproviders)) == 0:
            logger.debug(
                "Skipping update for %s because binproviders list was empty",
                self.name,
            )
            return self

        # logger.info("Updating %s binary", self.name)
        inner_exc: Exception | None = None
        errors = {}
        binary_postinstall_scripts = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        binary_min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        for binprovider in self._binprovider_order(binproviders):
            provider = binprovider
            try:
                provider = self.get_binprovider(
                    binprovider_name=binprovider.name,
                    dry_run=dry_run,
                    **extra_overrides,
                )
                resolved_postinstall_scripts = (
                    provider.postinstall_scripts
                    if binary_postinstall_scripts is None
                    else binary_postinstall_scripts
                )
                resolved_min_release_age = (
                    provider.min_release_age
                    if binary_min_release_age is None
                    else binary_min_release_age
                )
                updated_bin = provider.update(
                    self.name,
                    no_cache=no_cache,
                    dry_run=dry_run,
                    postinstall_scripts=resolved_postinstall_scripts,
                    min_release_age=resolved_min_release_age,
                    min_version=self.min_version,
                )
                if updated_bin is not None and updated_bin.loaded_abspath:
                    return self._validated_loaded_copy(
                        provider,
                        abspath=updated_bin.loaded_abspath,
                        version=updated_bin.loaded_version,
                        sha256=updated_bin.loaded_sha256,
                        mtime=updated_bin.loaded_mtime,
                        euid=updated_bin.loaded_euid,
                    )
            except Exception as err:
                inner_exc = err
                errors[binprovider.name] = format_exception_with_output(err)
                self._debug_provider_failure("update", provider, err)

        provider_names = ", ".join(
            binproviders or [p.name for p in self.binproviders],
        )
        raise BinaryUpdateError(self.name, provider_names, errors) from inner_exc

    @validate_call
    @log_method_call(include_result=True)
    def uninstall(
        self,
        binproviders: list[BinProviderName] | None = None,
        no_cache: bool = False,
        dry_run: bool | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        **extra_overrides,
    ) -> Self:
        assert self.name, f"No binary name was provided! {self}"

        if binproviders is not None and len(list(binproviders)) == 0:
            logger.debug(
                "Skipping uninstall for %s because binproviders list was empty",
                self.name,
            )
            return self

        # logger.info("Uninstalling %s binary", self.name)
        inner_exc: Exception | None = None
        errors = {}
        binary_postinstall_scripts = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        binary_min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        uninstall_candidates: list[BinProvider] = []
        for binprovider in self._binprovider_order(binproviders):
            if type(binprovider) is EnvProvider:
                binprovider.invalidate_cache(self.name)
                continue
            uninstall_candidates.append(binprovider)
        if not uninstall_candidates:
            for binprovider in self.binproviders:
                if binproviders and binprovider.name not in binproviders:
                    continue
                if type(binprovider) is EnvProvider:
                    binprovider.invalidate_cache(self.name)
                    continue
                uninstall_candidates.append(binprovider)

        for binprovider in uninstall_candidates:
            provider = binprovider
            try:
                provider = self.get_binprovider(
                    binprovider_name=binprovider.name,
                    dry_run=dry_run,
                    **extra_overrides,
                )
                resolved_postinstall_scripts = (
                    provider.postinstall_scripts
                    if binary_postinstall_scripts is None
                    else binary_postinstall_scripts
                )
                resolved_min_release_age = (
                    provider.min_release_age
                    if binary_min_release_age is None
                    else binary_min_release_age
                )
                uninstalled = provider.uninstall(
                    self.name,
                    no_cache=no_cache,
                    dry_run=dry_run,
                    postinstall_scripts=resolved_postinstall_scripts,
                    min_release_age=resolved_min_release_age,
                    min_version=self.min_version,
                )
                if uninstalled:
                    return self.model_copy(
                        deep=True,
                        update={
                            "loaded_binprovider": None,
                            "loaded_abspath": None,
                            "loaded_version": None,
                            "loaded_sha256": None,
                            "loaded_mtime": None,
                            "loaded_euid": None,
                        },
                    )
            except Exception as err:
                inner_exc = err
                errors[binprovider.name] = format_exception_with_output(err)
                self._debug_provider_failure("uninstall", provider, err)

        provider_names = ", ".join(
            binproviders or [p.name for p in self.binproviders],
        )
        raise BinaryUninstallError(self.name, provider_names, errors) from inner_exc

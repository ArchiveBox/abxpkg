__package__ = "abxpkg"

import logging as py_logging
import os
import sys
import pwd
import json
import inspect
import shutil
import stat
import hashlib
import platform
import subprocess
import functools
import tempfile
from contextvars import ContextVar

from typing import (
    Optional,
    ClassVar,
    cast,
    final,
    Any,
    Literal,
    Protocol,
    runtime_checkable,
    TypeVar,
)
from collections.abc import Callable, Iterable, Mapping

from typing_extensions import TypedDict
from typing import Self
from pathlib import Path

from pydantic_core import ValidationError
from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    validate_call,
    ConfigDict,
    InstanceOf,
    computed_field,
    model_validator,
)

from .semver import SemVer
from .base_types import (
    DEFAULT_LIB_DIR,
    BinName,
    BinDirPath,
    HostBinPath,
    BinProviderName,
    PATHStr,
    InstallArgs,
    Sha256,
    MTimeNs,
    EUID,
    SelfMethodName,
    UNKNOWN_SHA256,
    UNKNOWN_MTIME,
    UNKNOWN_EUID,
    bin_name,
    path_is_executable,
    path_is_script,
    abxpkg_install_root_default,
    bin_abspath,
    bin_abspaths,
    func_takes_args_or_kwargs,
)
from .logging import (
    TRACE_DEPTH,
    format_command,
    format_loaded_binary,
    format_subprocess_output,
    get_logger,
    log_with_trace_depth,
    log_subprocess_output,
    log_method_call,
    summarize_value,
)
from .exceptions import (
    BinProviderInstallError,
    BinProviderUnavailableError,
    BinProviderUninstallError,
    BinProviderUpdateError,
)
from .config import (
    apply_exec_env,
    build_exec_env,
    load_derived_cache,
    save_derived_cache,
)

logger = get_logger(__name__)

################## GLOBALS ##########################################

OPERATING_SYSTEM = platform.system().lower()
DEFAULT_PATH = "/home/linuxbrew/.linuxbrew/bin:/opt/homebrew/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_ENV_PATH = os.environ.get("PATH", DEFAULT_PATH)
PYTHON_BIN_DIR = str(Path(sys.executable).parent)

if PYTHON_BIN_DIR not in DEFAULT_ENV_PATH:
    DEFAULT_ENV_PATH = PYTHON_BIN_DIR + ":" + DEFAULT_ENV_PATH

UNKNOWN_ABSPATH = Path("/usr/bin/true")
UNKNOWN_VERSION = cast(SemVer, SemVer.parse("999.999.999"))
ACTIVE_EXEC_LOG_PREFIX: ContextVar[str | None] = ContextVar(
    "abxpkg_active_exec_log_prefix",
    default=None,
)


def env_flag_is_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


################## SUPPLY-CHAIN SECURITY HELPERS ######################


################## VALIDATORS #######################################

NEVER_CACHE = (
    None,
    UNKNOWN_ABSPATH,
    UNKNOWN_VERSION,
    UNKNOWN_SHA256,
)


def binprovider_cache(binprovider_method):
    """cache non-null return values for BinProvider methods on the BinProvider instance"""

    method_name = binprovider_method.__name__

    @functools.wraps(binprovider_method)
    def cached_function(self, bin_name: BinName, **kwargs):
        self._cache = self._cache or {}
        self._cache[method_name] = self._cache.get(method_name, {})
        method_cache = self._cache[method_name]

        if bin_name in method_cache and not kwargs.get("no_cache"):
            # print('USING CACHED VALUE:', f'{self.__class__.__name__}.{method_name}({bin_name}, {kwargs}) -> {method_cache[bin_name]}')
            return method_cache[bin_name]

        return_value = binprovider_method(self, bin_name, **kwargs)

        if return_value and return_value not in NEVER_CACHE:
            self._cache[method_name][bin_name] = return_value
        return return_value

    cached_function.__name__ = f"{method_name}_cached"

    return cached_function


R = TypeVar("R")


def remap_kwargs(
    renamed_kwargs: Mapping[str, str],
) -> Callable[[Callable[..., R]], Callable[..., R]]:
    def decorator(func: Callable[..., R]) -> Callable[..., R]:
        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> R:
            mapped_kwargs = dict(kwargs)
            for old_name, new_name in renamed_kwargs.items():
                if old_name in mapped_kwargs:
                    mapped_kwargs.setdefault(new_name, mapped_kwargs[old_name])
                    mapped_kwargs.pop(old_name, None)
            return func(*args, **mapped_kwargs)

        return wrapper

    return decorator


class ShallowBinary(BaseModel):
    """
    Shallow version of Binary used as a return type for BinProvider methods (e.g. install()).
    (doesn't implement full Binary interface, but can be used to populate a full loaded Binary instance)
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_by_alias=True,
        validate_by_name=True,
        validate_default=True,
        validate_assignment=False,
        from_attributes=True,
        arbitrary_types_allowed=True,
    )

    name: BinName = ""
    description: str = ""

    binproviders: list[InstanceOf["BinProvider"]] = Field(default_factory=list)
    overrides: "BinaryOverrides" = Field(default_factory=dict)

    loaded_binprovider: InstanceOf["BinProvider"] | None = Field(
        default=None,
        alias="binprovider",
    )
    loaded_abspath: HostBinPath | None = Field(default=None, alias="abspath")
    loaded_version: SemVer | None = Field(default=None, alias="version")
    loaded_sha256: Sha256 | None = Field(default=None, alias="sha256")
    loaded_mtime: MTimeNs | None = Field(default=None, alias="mtime")
    loaded_euid: EUID | None = Field(default=None, alias="euid")

    def __getattr__(self, item: str) -> Any:
        """Allow accessing fields by both field name and alias."""
        for field, meta in type(self).model_fields.items():
            if meta.alias == item:
                return getattr(self, field)
        raise AttributeError(item)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"name={self.name!r}, "
            f"abspath={self.loaded_abspath!r}, "
            f"version={self.loaded_version!r}, "
            f"sha256={f'...{str(self.loaded_sha256)[-6:]}' if self.loaded_sha256 else None!r}, "
            f"mtime={self.loaded_mtime!r}, "
            f"euid={self.loaded_euid!r}"
            f")"
        )

    __str__ = __repr__

    @model_validator(mode="after")
    def validate_model(self) -> Self:
        self.description = self.description or self.name
        return self

    @computed_field  # see mypy issue #1362
    @property
    def bin_filename(self) -> BinName:
        if self.is_script:
            # e.g. '.../Python.framework/Versions/3.11/lib/python3.11/sqlite3/__init__.py' -> sqlite
            name = self.name
        elif self.loaded_abspath:
            # e.g. '/opt/homebrew/bin/wget' -> wget
            name = bin_name(self.loaded_abspath)
        else:
            # e.g. 'ytdlp' -> 'yt-dlp'
            name = bin_name(self.name)
        return name

    @computed_field  # see mypy issue #1362
    @property
    def is_executable(self) -> bool:
        try:
            assert self.loaded_abspath and path_is_executable(self.loaded_abspath)
            return True
        except (ValidationError, AssertionError):
            return False

    @computed_field  # see mypy issue #1362
    @property
    def is_script(self) -> bool:
        try:
            assert self.loaded_abspath and path_is_script(self.loaded_abspath)
            return True
        except (ValidationError, AssertionError):
            return False

    @computed_field  # see mypy issue #1362
    @property
    def is_valid(self) -> bool:
        """Pure loaded-state check used by debug logging.

        Logging calls this while formatting return values, so it must stay fast
        and side-effect free. Keep any future override as a cheap in-memory
        predicate only; never resolve binaries, touch disk/network, or mutate
        state from here.
        """
        return bool(
            self.name and self.loaded_abspath and self.loaded_version,
        )

    @computed_field
    @property
    def bin_dir(self) -> BinDirPath | None:
        if not self.loaded_abspath:
            return None
        try:
            return TypeAdapter(BinDirPath).validate_python(self.loaded_abspath.parent)
        except (ValidationError, AssertionError):
            return None

    @computed_field
    @property
    def loaded_respath(self) -> HostBinPath | None:
        return self.loaded_abspath and self.loaded_abspath.resolve()

    # @validate_call
    @log_method_call(include_result=True)
    def exec(
        self,
        bin_name: BinName | HostBinPath | None = None,
        cmd: Iterable[str | Path | int | float | bool] = (),
        cwd: str | Path = ".",
        quiet=False,
        **kwargs,
    ) -> subprocess.CompletedProcess:
        bin_name = str(bin_name or self.loaded_abspath or self.name)
        if bin_name == self.name:
            assert self.loaded_abspath, (
                "Binary must have a loaded_abspath, make sure to load() or install() first"
            )
            assert self.loaded_version, (
                "Binary must have a loaded_version, make sure to load() or install() first"
            )
        assert os.path.isdir(cwd) and os.access(cwd, os.R_OK), (
            f"cwd must be a valid, accessible directory: {cwd}"
        )
        cmd = [str(bin_name), *(str(arg) for arg in cmd)]
        logger.debug("Executing binary command: %s", format_command(cmd))
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        explicit_env = kwargs.pop("env", None)
        if self.loaded_binprovider is not None:
            kwargs["env"] = self.loaded_binprovider.build_exec_env(
                providers=[self.loaded_binprovider],
                base_env=explicit_env,
            )
        elif explicit_env is not None:
            kwargs["env"] = dict(explicit_env)
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            **kwargs,
        )


class BinProvider(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_by_alias=True,
        validate_by_name=True,
        validate_default=True,
        validate_assignment=False,
        from_attributes=True,
        revalidate_instances="always",
        arbitrary_types_allowed=True,
    )
    name: BinProviderName = ""
    _log_emoji = "📦"

    # Runtime PATH for binaries installed by this provider. It starts either
    # empty or pre-seeded by the subclass, then setup_PATH() updates it lazily
    # on first real use. This is never the source of truth for resolving
    # INSTALLER_BIN; installers resolve from the ambient env instead.
    PATH: PATHStr = Field(
        default=str(Path(sys.executable).parent),
        repr=False,
    )  # e.g.  '/opt/homebrew/bin:/opt/archivebox/bin'
    INSTALLER_BIN: BinName = "env"
    INSTALLER_BINPROVIDERS: ClassVar[tuple[BinProviderName, ...] | None] = None

    euid: int | None = None
    install_root: Path | None = None
    bin_dir: Path | None = None
    dry_run: bool = Field(
        default_factory=lambda: (
            env_flag_is_true("ABXPKG_DRY_RUN")
            if "ABXPKG_DRY_RUN" in os.environ
            else env_flag_is_true("DRY_RUN")
        ),
    )
    postinstall_scripts: bool | None = Field(default=None)
    min_release_age: float | None = Field(default=None)

    overrides: "BinProviderOverrides" = Field(  # ty: ignore[invalid-assignment] https://github.com/astral-sh/ty/issues/2403
        default_factory=lambda: {
            "*": {
                "version": "self.default_version_handler",
                "abspath": "self.default_abspath_handler",
                "install_args": "self.default_install_args_handler",
                "install": "self.default_install_handler",
                "update": "self.default_update_handler",
                "uninstall": "self.default_uninstall_handler",
            },
        },
        repr=False,
        exclude=True,
    )

    install_timeout: int = Field(
        default_factory=lambda: int(os.environ.get("ABXPKG_INSTALL_TIMEOUT", "120")),
        repr=False,
    )
    version_timeout: int = Field(
        default_factory=lambda: int(os.environ.get("ABXPKG_VERSION_TIMEOUT", "10")),
        repr=False,
    )

    @computed_field(repr=False)
    @property
    def ENV(self) -> dict[str, str]:
        """Environment variables needed to run code using packages installed
        by this provider. Subclasses override to add provider-specific vars.

        Values are plain strings with optional merge semantics:
          "value"   → overwrite any existing value
          ":value"  → append (existing:value)
          "value:"  → prepend (value:existing)
        """
        return {}

    @computed_field(repr=False)
    @property
    def derived_env_path(self) -> Path | None:
        if self.install_root is None:
            return None
        return self.install_root / "derived.env"

    @staticmethod
    def apply_exec_env(
        exec_env: dict[str, str],
        env: dict[str, str],
    ) -> None:
        apply_exec_env(exec_env, env)

    @staticmethod
    def apply_env(
        exec_env: dict[str, str],
        env: dict[str, str],
    ) -> None:
        BinProvider.apply_exec_env(exec_env, env)

    @staticmethod
    def build_exec_env(
        providers=(),
        *,
        base_env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        return build_exec_env(
            providers=providers,
            base_env=base_env,
            extra_env=extra_env,
        )

    def get_cache_info(
        self,
        bin_name: BinName,
        abspath: HostBinPath,
    ) -> dict[str, list[Path]] | None:
        if not self.install_root:
            return None
        return {
            "fingerprint_paths": [
                Path(abspath).expanduser().resolve(strict=False),
            ],
        }

    class CacheFingerprint(TypedDict):
        path: str
        inode: int
        size: int
        mtime_ns: int
        euid: int

    class CacheRecord(TypedDict):
        fingerprint: list["BinProvider.CacheFingerprint"]
        loaded_version: str
        loaded_sha256: str
        loaded_euid: int
        cache_kind: str
        provider_name: str
        resolved_provider_name: str
        bin_name: str
        abspath: str
        install_args: list[str]
        inode: int
        mtime: int
        euid: int

    def _cache_key(
        self,
        bin_name: BinName,
        abspath: HostBinPath,
    ) -> str:
        resolved_abspath = Path(abspath).expanduser().resolve(strict=False)
        return json.dumps(
            [self.name, str(bin_name), str(resolved_abspath)],
            separators=(",", ":"),
        )

    def _fingerprint_paths(
        self,
        paths: Iterable[Path],
    ) -> list["BinProvider.CacheFingerprint"] | None:
        fingerprints: list["BinProvider.CacheFingerprint"] = []
        for path in paths:
            resolved_path = path.expanduser().resolve(strict=False)
            try:
                stat_result = resolved_path.stat()
            except OSError:
                return None
            fingerprints.append(
                {
                    "path": str(resolved_path),
                    "inode": stat_result.st_ino,
                    "size": stat_result.st_size,
                    "mtime_ns": stat_result.st_mtime_ns,
                    "euid": stat_result.st_uid,
                },
            )
        return fingerprints

    @log_method_call(include_result=True)
    def load_cached_binary(
        self,
        bin_name: BinName,
        abspath: HostBinPath,
    ) -> ShallowBinary | None:
        derived_env_path = self.derived_env_path
        if derived_env_path is None:
            return None

        cache_info = self.get_cache_info(bin_name, abspath)
        if cache_info is None:
            return None

        fingerprints = self._fingerprint_paths(cache_info["fingerprint_paths"])
        if fingerprints is None:
            return None

        cache = load_derived_cache(derived_env_path)
        cache_key = self._cache_key(bin_name, abspath)
        cached_record = cache.get(cache_key)
        if not isinstance(cached_record, dict):
            return None
        if cached_record.get("fingerprint") != fingerprints:
            cache.pop(cache_key, None)
            save_derived_cache(derived_env_path, cache)
            return None

        loaded_version = cached_record.get("loaded_version")
        loaded_sha256 = cached_record.get("loaded_sha256")
        loaded_euid = cached_record.get("loaded_euid")
        if (
            not isinstance(loaded_version, str)
            or not isinstance(loaded_sha256, str)
            or not isinstance(loaded_euid, int)
        ):
            cache.pop(cache_key, None)
            save_derived_cache(derived_env_path, cache)
            return None

        try:
            from . import PROVIDER_CLASS_BY_NAME

            version = SemVer.parse(loaded_version)
            assert version is not None
            sha256 = TypeAdapter(Sha256).validate_python(loaded_sha256)
            mtime = TypeAdapter(MTimeNs).validate_python(fingerprints[0]["mtime_ns"])
            euid = TypeAdapter(EUID).validate_python(loaded_euid)
        except Exception:
            cache.pop(cache_key, None)
            save_derived_cache(derived_env_path, cache)
            return None

        original_abspath = str(Path(abspath).expanduser().absolute())
        resolved_abspath = str(Path(abspath).expanduser().resolve(strict=False))
        cached_install_args = cached_record.get("install_args")
        resolved_provider_name = cached_record.get("resolved_provider_name")
        cache_kind = cached_record.get("cache_kind")
        cached_abspath = cached_record.get("abspath")
        if not isinstance(resolved_provider_name, str):
            resolved_provider_name = self.name
        if not isinstance(cache_kind, str):
            cache_kind = (
                "dependency" if str(bin_name) == str(self.INSTALLER_BIN) else "binary"
            )
        if not isinstance(cached_abspath, str):
            cache.pop(cache_key, None)
            save_derived_cache(derived_env_path, cache)
            return None
        primary_fingerprint = fingerprints[0]
        if cached_abspath == resolved_abspath and original_abspath != resolved_abspath:
            cached_abspath = original_abspath
        if (
            cached_record.get("provider_name") != self.name
            or cached_record.get("resolved_provider_name") != resolved_provider_name
            or cached_record.get("cache_kind") != cache_kind
            or cached_record.get("bin_name") != str(bin_name)
            or cached_abspath not in {original_abspath, resolved_abspath}
            or not isinstance(cached_install_args, list)
            or not all(isinstance(arg, str) for arg in cached_install_args)
            or cached_record.get("inode") != primary_fingerprint["inode"]
            or cached_record.get("mtime") != primary_fingerprint["mtime_ns"]
            or cached_record.get("euid") != primary_fingerprint["euid"]
        ):
            cache[cache_key] = {
                "fingerprint": fingerprints,
                "loaded_version": str(version),
                "loaded_sha256": str(sha256),
                "loaded_euid": euid,
                "cache_kind": cache_kind,
                "provider_name": self.name,
                "resolved_provider_name": resolved_provider_name,
                "bin_name": str(bin_name),
                "abspath": original_abspath,
                "install_args": list(
                    self.get_install_args(bin_name, quiet=True, no_cache=True),
                ),
                "inode": primary_fingerprint["inode"],
                "mtime": primary_fingerprint["mtime_ns"],
                "euid": primary_fingerprint["euid"],
            }
            save_derived_cache(derived_env_path, cache)
            cached_abspath = original_abspath

        resolved_provider = (
            self
            if resolved_provider_name == self.name
            else PROVIDER_CLASS_BY_NAME.get(resolved_provider_name, type(self))()
        )
        return ShallowBinary.model_validate(
            {
                "name": bin_name,
                "binprovider": resolved_provider,
                "abspath": Path(cached_abspath),
                "version": version,
                "sha256": sha256,
                "mtime": mtime,
                "euid": euid,
                "binproviders": [resolved_provider],
            },
        )

    @log_method_call()
    def write_cached_binary(
        self,
        bin_name: BinName,
        abspath: HostBinPath,
        loaded_version: SemVer,
        loaded_sha256: Sha256,
        resolved_provider_name: str | None = None,
        cache_kind: str = "binary",
    ) -> tuple[MTimeNs, EUID] | None:
        derived_env_path = self.derived_env_path
        cache_info = self.get_cache_info(bin_name, abspath)
        if cache_info is None:
            return None

        fingerprints = self._fingerprint_paths(cache_info["fingerprint_paths"])
        if fingerprints is None:
            return None

        original_abspath = str(Path(abspath).expanduser().absolute())
        primary_fingerprint = fingerprints[0]
        record: dict[str, object] = {
            "fingerprint": fingerprints,
            "loaded_version": str(loaded_version),
            "loaded_sha256": str(loaded_sha256),
            "loaded_euid": primary_fingerprint["euid"],
            "cache_kind": cache_kind,
            "provider_name": self.name,
            "resolved_provider_name": resolved_provider_name or self.name,
            "bin_name": str(bin_name),
            "abspath": original_abspath,
            "install_args": list(
                self.get_install_args(bin_name, quiet=True, no_cache=True),
            ),
            "inode": primary_fingerprint["inode"],
            "mtime": primary_fingerprint["mtime_ns"],
            "euid": primary_fingerprint["euid"],
        }
        if derived_env_path is None:
            return None
        if not (
            derived_env_path.parent.exists() or derived_env_path.parent.is_symlink()
        ):
            derived_env_path.parent.mkdir(parents=True, exist_ok=True)
        cache = load_derived_cache(derived_env_path)
        cache[self._cache_key(bin_name, abspath)] = record
        try:
            save_derived_cache(derived_env_path, cache)
        except Exception as err:
            logger.debug(
                "Skipping cache write for %s via %s: %s",
                bin_name,
                self.name,
                err,
            )
            return None
        return (
            TypeAdapter(MTimeNs).validate_python(fingerprints[0]["mtime_ns"]),
            TypeAdapter(EUID).validate_python(fingerprints[0]["euid"]),
        )

    _cache: dict[str, dict[str, Any]] | None = (
        None  # Per-method in-memory cache populated by @binprovider_cache during the current process only.
    )
    _INSTALLER_BINARY: ShallowBinary | None = (
        None  # cached by INSTALLER_BINARY property after first resolution
    )

    def __eq__(self, other: Any) -> bool:
        try:
            return (
                dict(self) == dict(other)
            )  # only compare pydantic fields, ignores classvars/@properties/@cached_properties/_fields/etc.
        except Exception:
            return False

    @staticmethod
    def uid_has_passwd_entry(uid: int) -> bool:
        try:
            pwd.getpwuid(uid)
        except KeyError:
            return False
        return True

    def detect_euid(
        self,
        owner_paths: Iterable[str | Path | None] = (),
        preserve_root: bool = False,
    ) -> int:
        current_euid = os.geteuid()
        candidate_euid = None

        for path in owner_paths:
            if path and os.path.isdir(path):
                candidate_euid = os.stat(path).st_uid
                break

        if candidate_euid is None:
            if preserve_root and current_euid == 0:
                candidate_euid = 0
            else:
                try:
                    if (
                        self._INSTALLER_BINARY is not None
                        and self._INSTALLER_BINARY.loaded_abspath is not None
                    ):
                        installer_abspath = self._INSTALLER_BINARY.loaded_abspath
                    else:
                        installer_abspath = bin_abspath(
                            self.INSTALLER_BIN,
                            PATH=self.PATH,
                        ) or bin_abspath(self.INSTALLER_BIN)
                    if installer_abspath is not None:
                        installer_owner = (
                            self._INSTALLER_BINARY.loaded_euid
                            if (
                                self._INSTALLER_BINARY is not None
                                and self._INSTALLER_BINARY.loaded_abspath
                                == installer_abspath
                                and self._INSTALLER_BINARY.loaded_euid is not None
                            )
                            else os.stat(installer_abspath).st_uid
                        )
                        if installer_owner != 0:
                            candidate_euid = installer_owner
                except Exception:
                    # INSTALLER_BIN is not always available (e.g. at import time, or if it dynamically changes)
                    pass

        if candidate_euid is not None and not self.uid_has_passwd_entry(candidate_euid):
            candidate_euid = current_euid

        return candidate_euid if candidate_euid is not None else current_euid

    def get_pw_record(self, uid: int) -> pwd.struct_passwd:
        try:
            return pwd.getpwuid(uid)
        except KeyError:
            if uid != os.geteuid():
                raise
            return pwd.struct_passwd(
                (
                    os.environ.get("USER") or os.environ.get("LOGNAME") or str(uid),
                    "x",
                    uid,
                    os.getegid(),
                    "",
                    os.environ.get("HOME", tempfile.gettempdir()),
                    os.environ.get("SHELL", "/bin/sh"),
                ),
            )

    @property
    def EUID(self) -> int:
        """
        Detect the user (UID) to run as when executing this binprovider's INSTALLER_BIN
        e.g. homebrew should never be run as root, we can tell which user to run it as by looking at who owns its binary
        apt should always be run as root, pip should be run as the user that owns the venv, etc.
        """

        # use user-provided value if one is set
        if self.euid is not None:
            return self.euid

        return self.detect_euid()

    def INSTALLER_BINARY(self, no_cache: bool = False) -> ShallowBinary:
        """Resolve the provider's own installer binary (e.g. npm, pip, cargo).

        Cached after first resolution. Pass no_cache=True to force re-resolution.
        Raises BinProviderUnavailableError if the installer cannot be found.
        Checks ``{INSTALLER_BIN}_BINARY`` env var (e.g. ``NPM_BINARY``) first.
        Subclasses override only if they need extra resolution logic.
        """
        from . import Binary, DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

        if not no_cache and self._INSTALLER_BINARY and self._INSTALLER_BINARY.is_valid:
            return self._INSTALLER_BINARY

        derived_env_path = self.derived_env_path
        if not no_cache and derived_env_path and derived_env_path.is_file():
            cache = load_derived_cache(derived_env_path)
            for cached_record in cache.values():
                if not isinstance(cached_record, dict):
                    continue
                if cached_record.get("provider_name") != self.name or cached_record.get(
                    "bin_name",
                ) != str(self.INSTALLER_BIN):
                    continue
                cached_abspath = cached_record.get("abspath")
                if not isinstance(cached_abspath, str):
                    continue
                loaded = self.load_cached_binary(
                    self.INSTALLER_BIN,
                    Path(cached_abspath),
                )
                if loaded and loaded.loaded_abspath:
                    self._INSTALLER_BINARY = loaded
                    return loaded

        env_provider = EnvProvider(install_root=None, bin_dir=None)
        raw_provider_names = os.environ.get("ABXPKG_BINPROVIDERS")
        selected_provider_names = (
            [provider_name.strip() for provider_name in raw_provider_names.split(",")]
            if raw_provider_names
            else list(DEFAULT_PROVIDER_NAMES)
        )
        preferred_provider_names = (
            selected_provider_names
            if raw_provider_names or not self.INSTALLER_BINPROVIDERS
            else list(self.INSTALLER_BINPROVIDERS)
        )
        installer_provider_names = [
            provider_name
            for provider_name in preferred_provider_names
            if provider_name
            and provider_name in selected_provider_names
            and provider_name in PROVIDER_CLASS_BY_NAME
            and provider_name != self.name
        ]
        installer_providers: list[BinProvider] = [
            env_provider
            if provider_name == "env"
            else PROVIDER_CLASS_BY_NAME[provider_name]()
            for provider_name in installer_provider_names
        ]
        if not installer_providers:
            installer_providers = [env_provider]

        # Check {INSTALLER_BIN}_BINARY env var (e.g. NPM_BINARY, UV_BINARY)
        env_var = f"{self.INSTALLER_BIN.upper()}_BINARY"
        manual = os.environ.get(env_var)
        if manual and os.path.isabs(manual) and Path(manual).is_file():
            manual_dir = str(Path(manual).parent)
            try:
                env_provider.PATH = env_provider._merge_PATH(
                    manual_dir,
                    PATH=env_provider.PATH,
                    prepend=True,
                )
                manual_installer_providers = (
                    [env_provider, *installer_providers]
                    if all(provider.name != "env" for provider in installer_providers)
                    else installer_providers
                )
                loaded = Binary(
                    name=self.INSTALLER_BIN,
                    binproviders=manual_installer_providers,
                ).install(
                    no_cache=no_cache,
                )
                if loaded and loaded.loaded_abspath:
                    if loaded.loaded_version and loaded.loaded_sha256:
                        self.write_cached_binary(
                            self.INSTALLER_BIN,
                            loaded.loaded_abspath,
                            loaded.loaded_version,
                            loaded.loaded_sha256,
                            resolved_provider_name=(
                                loaded.loaded_binprovider.name
                                if loaded.loaded_binprovider is not None
                                else self.name
                            ),
                            cache_kind="dependency",
                        )
                    self._INSTALLER_BINARY = loaded
                    return self._INSTALLER_BINARY
            except Exception:
                pass

        try:
            loaded = Binary(
                name=self.INSTALLER_BIN,
                binproviders=installer_providers,
            ).install(no_cache=no_cache)
            if loaded and loaded.loaded_abspath:
                if loaded.loaded_version and loaded.loaded_sha256:
                    self.write_cached_binary(
                        self.INSTALLER_BIN,
                        loaded.loaded_abspath,
                        loaded.loaded_version,
                        loaded.loaded_sha256,
                        resolved_provider_name=(
                            loaded.loaded_binprovider.name
                            if loaded.loaded_binprovider is not None
                            else self.name
                        ),
                        cache_kind="dependency",
                    )
                self._INSTALLER_BINARY = loaded
                return self._INSTALLER_BINARY
        except Exception:
            pass

        raise BinProviderUnavailableError(
            self.__class__.__name__,
            self.INSTALLER_BIN,
        )

    @computed_field(repr=False)
    @property
    def is_valid(self) -> bool:
        """Pure provider-health check used by debug logging.

        Logging calls this while formatting return values, so it must stay fast
        and side-effect free. Any override must remain a cheap readiness check
        only and must never install, resolve, invalidate cache, or mutate state.
        """
        if self.bin_dir is not None:
            if self.bin_dir.is_dir() and os.access(self.bin_dir, os.R_OK):
                pass
            elif self.bin_dir.exists():
                return False
            else:
                candidate_dir = self.bin_dir.parent
                remaining_hops = 3
                while remaining_hops > 0 and not candidate_dir.exists():
                    candidate_dir = candidate_dir.parent
                    remaining_hops -= 1
                if not (
                    candidate_dir.is_dir()
                    and os.access(candidate_dir, os.R_OK | os.W_OK)
                ):
                    return False
        installer = self._INSTALLER_BINARY
        if installer and installer.is_valid:
            return True
        manual_installer = os.environ.get(f"{self.INSTALLER_BIN.upper()}_BINARY")
        if manual_installer:
            manual_path = Path(manual_installer).expanduser()
            if (
                manual_path.is_absolute()
                and manual_path.is_file()
                and os.access(manual_path, os.X_OK)
            ):
                return True
        return bool(
            bin_abspath(self.INSTALLER_BIN, PATH=self.PATH)
            or bin_abspath(self.INSTALLER_BIN),
        )

    @final
    # @validate_call(config={'arbitrary_types_allowed': True})
    @log_method_call()
    def get_provider_with_overrides(
        self,
        overrides: Optional["BinProviderOverrides"] = None,
        dry_run: bool | None = None,
        install_timeout: int | None = None,
        version_timeout: int | None = None,
        **provider_patches: Any,
    ) -> Self:
        # created an updated copy of the BinProvider with the overrides applied, then get the handlers on it.
        # important to do this so that any subsequent calls to handler functions down the call chain
        # still have access to the overrides, we don't have to have to pass them down as args all the way down the stack

        updated_binprovider: Self = self.model_copy(deep=True)

        # main binary-specific overrides for [abspath, version, install_args, install, update, uninstall]
        overrides = overrides or {}

        # extra overrides that are also configurable, can add more in the future as-needed for tunable options
        updated_binprovider.dry_run = self.dry_run if dry_run is None else dry_run
        updated_binprovider.install_timeout = (
            self.install_timeout if install_timeout is None else install_timeout
        )
        updated_binprovider.version_timeout = (
            self.version_timeout if version_timeout is None else version_timeout
        )

        # overrides = {
        #     'wget': {
        #         'install_args': lambda: ['wget'],
        #         'abspath': lambda: shutil.which('wget'),
        #         'version': lambda: SemVer.parse(os.system('wget --version')),
        #         'install': lambda: os.system('brew install wget'),
        #     },
        # }
        for binname, bin_overrides in overrides.items():
            provider_field_overrides: dict[str, Any] = {}
            handler_overrides: dict[str, Any] = {}
            for key, value in bin_overrides.items():
                if key != "overrides" and key in type(updated_binprovider).model_fields:
                    provider_field_overrides[key] = value
                else:
                    handler_overrides[key] = value

            if provider_field_overrides:
                updated_binprovider = type(self).model_validate(
                    {
                        **updated_binprovider.model_dump(
                            mode="python",
                            round_trip=True,
                        ),
                        "overrides": updated_binprovider.overrides,
                        **provider_field_overrides,
                    },
                )

            if handler_overrides:
                updated_binprovider.overrides[binname] = cast(
                    HandlerDict,
                    {
                        **updated_binprovider.overrides.get(binname, {}),
                        **handler_overrides,
                    },
                )

        if provider_patches:
            updated_binprovider = type(self).model_validate(
                {
                    **updated_binprovider.model_dump(
                        mode="python",
                        round_trip=True,
                    ),
                    "overrides": updated_binprovider.overrides,
                    **provider_patches,
                },
            )

        return updated_binprovider

    # @validate_call
    @log_method_call(include_result=True)
    def _get_handler_keys(
        self,
        handler_type: "HandlerType",
    ) -> tuple["HandlerType", ...]:
        if handler_type in ("install_args", "packages"):
            return ("install_args", "packages")
        return (handler_type,)

    @log_method_call(include_result=True)
    def _get_handler_for_action(
        self,
        bin_name: BinName,
        handler_type: "HandlerType",
    ) -> Callable[..., "HandlerReturnValue"]:
        """
        Get the handler func for a given key + Dict of handler callbacks + fallback default handler.
        e.g. _get_handler_for_action(bin_name='yt-dlp', 'install', default_handler=self.default_install_handler, ...) -> Callable
        """

        handler: HandlerValue | None = None
        for overrides_for_bin in (
            self.overrides.get(bin_name, {}),
            self.overrides.get("*", {}),
        ):
            for handler_key in self._get_handler_keys(handler_type):
                handler = overrides_for_bin.get(handler_key)
                if handler:
                    break
            if handler:
                break
        # print('getting handler for action', bin_name, handler_type, handler_func)
        assert handler, (
            f"🚫 BinProvider(name={self.name}) has no {handler_type} handler implemented for Binary(name={bin_name})"
        )

        # if handler_func is already a callable, return it directly
        if isinstance(handler, Callable):
            return handler

        # if handler_func is string reference to a function on self, swap it for the actual function
        elif isinstance(handler, str) and (
            handler.startswith("self.") or handler.startswith("BinProvider.")
        ):
            # special case, allow dotted path references to methods on self (where self refers to the BinProvider)
            handler_method: Callable[..., HandlerReturnValue] = getattr(
                self,
                handler.split("self.", 1)[-1],
            )
            return handler_method

        # if handler_func is any other value, treat is as a literal and return a func that provides the literal
        literal_value = TypeAdapter(HandlerReturnValue).validate_python(handler)

        def literal_handler() -> HandlerReturnValue:
            return literal_value

        return literal_handler

    # @validate_call
    @log_method_call(include_result=True)
    def _get_compatible_kwargs(
        self,
        handler_func: Callable[..., "HandlerReturnValue"],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        if not kwargs:
            return kwargs

        signature = inspect.signature(handler_func)
        if any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        ):
            return kwargs

        accepted_kwargs = set(signature.parameters)
        return {key: value for key, value in kwargs.items() if key in accepted_kwargs}

    @log_method_call(include_result=True)
    def _call_handler_for_action(
        self,
        bin_name: BinName,
        handler_type: "HandlerType",
        **kwargs,
    ) -> "HandlerReturnValue":
        handler_func: Callable[..., HandlerReturnValue] = self._get_handler_for_action(
            bin_name=bin_name,  # e.g. 'yt-dlp', or 'wget', etc.
            handler_type=handler_type,  # e.g. abspath, version, install_args, install
        )

        # def timeout_handler(signum, frame):
        # raise TimeoutError(f'{self.__class__.__name__} Timeout while running {handler_type} for Binary {bin_name}')

        # signal ONLY WORKS IN MAIN THREAD, not a viable solution for timeout enforcement! breaks in prod
        # signal.signal(signal.SIGALRM, handler=timeout_handler)
        # signal.alarm(timeout)
        try:
            if not func_takes_args_or_kwargs(handler_func):
                # if it's a pure argless lambda/func, dont pass bin_path and other **kwargs
                handler_func_without_args = cast(
                    Callable[[], HandlerReturnValue],
                    handler_func,
                )
                return handler_func_without_args()

            compatible_kwargs = self._get_compatible_kwargs(handler_func, kwargs)
            if hasattr(handler_func, "__self__"):
                # func is already a method bound to self, just call it directly
                return handler_func(bin_name, **compatible_kwargs)
            else:
                # func is not bound to anything, pass BinProvider as first arg
                return handler_func(self, bin_name, **compatible_kwargs)
        except TimeoutError:
            raise
        # finally:
        #     signal.alarm(0)

    # DEFAULT HANDLERS, override these in subclasses as needed:

    # @validate_call
    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> "AbspathFuncReturnValue":  # aka str | Path | None
        # If asked for the installer binary itself, resolve directly via
        # bin_abspath (NOT via INSTALLER_BINARY, which would recurse back here).
        if str(bin_name) == self.INSTALLER_BIN:
            return bin_abspath(bin_name, PATH=self.PATH) or bin_abspath(bin_name)

        if not self.PATH:
            return None

        bin_dir = self.bin_dir
        if bin_dir is not None:
            managed_abspath = bin_abspath(bin_name, PATH=str(bin_dir))
            if managed_abspath is not None:
                return managed_abspath
            return None

        return bin_abspath(bin_name, PATH=self.PATH)

    # @validate_call
    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> "VersionFuncReturnValue":  # aka List[str] | Tuple[str, ...]
        return self._version_from_exec(
            bin_name,
            abspath=abspath,
            timeout=timeout,
        )

    # @validate_call
    def default_install_args_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> "InstallArgsFuncReturnValue":  # aka List[str] aka InstallArgs
        # print(f'[*] {self.__class__.__name__}: Getting install command for {bin_name}')
        # ... install command calculation logic here
        return [bin_name]

    def default_packages_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> "InstallArgsFuncReturnValue":
        return self.default_install_args_handler(bin_name, **context)

    # @validate_call
    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> "InstallFuncReturnValue":  # aka str
        self.setup(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
            no_cache=no_cache,
        )
        install_args = install_args or self.get_install_args(bin_name)
        self.INSTALLER_BINARY(no_cache=no_cache)

        # print(f'[*] {self.__class__.__name__}: Installing {bin_name}: {install_args}')

        # ... override the default install logic here ...

        # installer_binary = self.INSTALLER_BINARY(no_cache=no_cache); assert installer_binary; proc = self.exec(bin_name=installer_binary.loaded_abspath, cmd=['install', *install_args], timeout=self.install_timeout)
        # if not proc.returncode == 0:
        #     print(proc.stdout.strip())
        #     print(proc.stderr.strip())
        #     raise Exception(f'{self.name} Failed to install {bin_name}: {proc.stderr.strip()}\n{proc.stdout.strip()}')

        return f"🚫 {self.name} BinProvider does not implement any .install() method"

    # @validate_call
    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> "ActionFuncReturnValue":
        self.INSTALLER_BINARY(no_cache=no_cache)
        return f"🚫 {self.name} BinProvider does not implement any .update() method"

    # @validate_call
    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> "ActionFuncReturnValue":
        self.INSTALLER_BINARY(no_cache=no_cache)
        return False

    @log_method_call()
    def invalidate_cache(self, bin_name: BinName) -> None:
        if self._cache:
            for method_cache in self._cache.values():
                method_cache.pop(bin_name, None)
        derived_env_path = self.derived_env_path
        if derived_env_path is None:
            return
        cache = load_derived_cache(derived_env_path)
        updated_cache: dict[str, object] = {}
        for cache_key, cache_value in cache.items():
            try:
                provider_name, cached_bin_name, _cached_abspath = json.loads(cache_key)
            except Exception:
                continue
            if provider_name == self.name and cached_bin_name == str(bin_name):
                continue
            updated_cache[cache_key] = cache_value
        if updated_cache != cache:
            save_derived_cache(derived_env_path, updated_cache)
        if str(bin_name) == self.INSTALLER_BIN:
            self._INSTALLER_BINARY = None

    @log_method_call(include_result=True)
    def has_cached_binary(self, bin_name: BinName) -> bool:
        derived_env_path = self.derived_env_path
        if derived_env_path is None or not derived_env_path.is_file():
            return False
        cache = load_derived_cache(derived_env_path)
        cache_changed = False
        has_valid_cache = False
        for cache_key, cache_value in list(cache.items()):
            if not isinstance(cache_value, dict):
                continue
            cached_provider_name = cache_value.get("provider_name")
            cached_bin_name = cache_value.get("bin_name")
            cached_abspath = cache_value.get("abspath")
            cache_kind = cache_value.get("cache_kind")
            if not isinstance(cached_provider_name, str) or not isinstance(
                cached_bin_name,
                str,
            ):
                try:
                    cached_provider_name, cached_bin_name, cached_abspath = json.loads(
                        cache_key,
                    )
                except Exception:
                    continue
            if (
                cached_provider_name != self.name
                or cached_bin_name != str(bin_name)
                or not isinstance(cached_abspath, str)
            ):
                continue
            if not isinstance(cache_kind, str):
                cache_kind = (
                    "dependency"
                    if str(cached_bin_name) == str(self.INSTALLER_BIN)
                    else "binary"
                )
            if cache_kind != "binary":
                continue
            cached_path = Path(cached_abspath)
            if not (cached_path.exists() or cached_path.is_symlink()):
                cache.pop(cache_key, None)
                cache_changed = True
                continue
            has_valid_cache = True
        if cache_changed:
            save_derived_cache(derived_env_path, cache)
        return has_valid_cache

    @log_method_call(include_result=True)
    def depends_on_binaries(self) -> list[ShallowBinary]:
        derived_env_path = self.derived_env_path
        if derived_env_path is None or not derived_env_path.is_file():
            return []
        cache = load_derived_cache(derived_env_path)
        dependencies: list[ShallowBinary] = []
        seen: set[tuple[str, str, str]] = set()
        for cache_key, cache_value in sorted(cache.items()):
            if not isinstance(cache_value, dict):
                continue
            cached_provider_name = cache_value.get("provider_name")
            cached_bin_name = cache_value.get("bin_name")
            cached_abspath = cache_value.get("abspath")
            cache_kind = cache_value.get("cache_kind")
            if (
                not isinstance(cached_provider_name, str)
                or not isinstance(cached_bin_name, str)
                or not isinstance(cached_abspath, str)
            ):
                try:
                    cached_provider_name, cached_bin_name, cached_abspath = json.loads(
                        cache_key,
                    )
                except Exception:
                    continue
            if cached_provider_name != self.name or not isinstance(cached_abspath, str):
                continue
            if not isinstance(cache_kind, str):
                cache_kind = (
                    "dependency"
                    if str(cached_bin_name) == str(self.INSTALLER_BIN)
                    else "binary"
                )
            if cache_kind != "dependency":
                continue
            loaded = self.load_cached_binary(cached_bin_name, Path(cached_abspath))
            if loaded is None or loaded.loaded_abspath is None:
                continue
            resolved_provider = loaded.loaded_binprovider or self
            dedupe_key = (
                loaded.name,
                str(loaded.loaded_abspath),
                resolved_provider.name,
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            dependencies.append(
                ShallowBinary.model_validate(
                    {
                        "name": loaded.name,
                        "description": loaded.description,
                        "binprovider": resolved_provider,
                        "abspath": loaded.loaded_abspath,
                        "version": loaded.loaded_version,
                        "sha256": loaded.loaded_sha256,
                        "mtime": loaded.loaded_mtime,
                        "euid": loaded.loaded_euid,
                        "binproviders": [resolved_provider],
                        "overrides": loaded.overrides,
                    },
                ),
            )
        return dependencies

    @log_method_call(include_result=True)
    def installed_binaries(self) -> list[ShallowBinary]:
        derived_env_path = self.derived_env_path
        if derived_env_path is None or not derived_env_path.is_file():
            return []
        cache = load_derived_cache(derived_env_path)
        binaries: list[ShallowBinary] = []
        seen: set[tuple[str, str, str]] = set()
        for cache_key, cache_value in sorted(cache.items()):
            if not isinstance(cache_value, dict):
                continue
            cached_provider_name = cache_value.get("provider_name")
            cached_bin_name = cache_value.get("bin_name")
            cached_abspath = cache_value.get("abspath")
            cache_kind = cache_value.get("cache_kind")
            if (
                not isinstance(cached_provider_name, str)
                or not isinstance(cached_bin_name, str)
                or not isinstance(cached_abspath, str)
            ):
                try:
                    cached_provider_name, cached_bin_name, cached_abspath = json.loads(
                        cache_key,
                    )
                except Exception:
                    continue
            if cached_provider_name != self.name or not isinstance(cached_abspath, str):
                continue
            if not isinstance(cache_kind, str):
                cache_kind = (
                    "dependency"
                    if str(cached_bin_name) == str(self.INSTALLER_BIN)
                    else "binary"
                )
            if cache_kind != "binary":
                continue
            loaded = self.load_cached_binary(cached_bin_name, Path(cached_abspath))
            if loaded is None or loaded.loaded_abspath is None:
                continue
            resolved_provider = loaded.loaded_binprovider or self
            dedupe_key = (
                loaded.name,
                str(loaded.loaded_abspath),
                resolved_provider.name,
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            binaries.append(
                ShallowBinary.model_validate(
                    {
                        "name": loaded.name,
                        "description": loaded.description,
                        "binprovider": resolved_provider,
                        "abspath": loaded.loaded_abspath,
                        "version": loaded.loaded_version,
                        "sha256": loaded.loaded_sha256,
                        "mtime": loaded.loaded_mtime,
                        "euid": loaded.loaded_euid,
                        "binproviders": [resolved_provider],
                        "overrides": loaded.overrides,
                    },
                ),
            )
        return binaries

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate runtime PATH lazily.

        Called by resolution/install/update/uninstall entrypoints before they
        need provider binaries. Subclasses document whether PATH stays
        ambient, gets replaced with install_root/bin_dir paths, or prepends those dirs
        to an ambient seed. This method must not resolve INSTALLER_BINARY()
        from here or perform eager work at construction time.
        """
        for path in reversed(self.PATH.split(":")):
            if path not in sys.path:
                sys.path.insert(
                    0,
                    path,
                )  # e.g. /opt/archivebox/bin:/bin:/usr/local/bin:...

    # _require_installer_bin removed: inline ``assert self.INSTALLER_BINARY()`` at callsites

    def _merge_PATH(
        self,
        *entries: str | Path,
        PATH: str | None = None,
        prepend: bool = False,
    ) -> PATHStr:
        new_entries = [str(entry) for entry in entries if str(entry)]
        existing_entries = [entry for entry in (PATH or "").split(":") if entry]
        merged_entries = (
            [*new_entries, *existing_entries]
            if prepend
            else [*existing_entries, *new_entries]
        )
        return TypeAdapter(PATHStr).validate_python(
            ":".join(dict.fromkeys(merged_entries)),
        )

    def _version_from_exec(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
    ) -> SemVer | None:
        abspath = abspath or self.get_abspath(bin_name, quiet=True)
        if not abspath:
            return None

        timeout = self.version_timeout if timeout is None else timeout
        validation_err = None
        version_outputs: list[str] = []

        for version_arg in ("--version", "-version", "-v"):
            proc = self.exec(
                bin_name=abspath,
                cmd=[version_arg],
                timeout=timeout,
                quiet=True,
            )
            version_output = proc.stdout.strip() or proc.stderr.strip()
            version_outputs.append(version_output)
            if proc.returncode != 0:
                validation_err = validation_err or AssertionError(
                    f"❌ $ {bin_name} {version_arg} exited with status {proc.returncode}",
                )
                continue
            try:
                version = SemVer.parse(version_output)
                assert version, (
                    f"❌ Could not parse version from $ {bin_name} {version_arg}: {version_output}".strip()
                )
                return version
            except (ValidationError, AssertionError) as err:
                validation_err = validation_err or err

        raise ValueError(
            f"❌ Unable to find {bin_name} version from {bin_name} --version, -version or -v output\n{next((output for output in version_outputs if output), '')}".strip(),
        ) from validation_err

    def _ensure_writable_cache_dir(self, cache_dir: Path) -> bool:
        if cache_dir.exists() and not cache_dir.is_dir():
            return False

        cache_dir.mkdir(parents=True, exist_ok=True)

        pw_record = self.get_pw_record(self.EUID)
        try:
            os.chown(cache_dir, self.EUID, pw_record.pw_gid)
        except PermissionError:
            pass

        try:
            cache_dir.chmod(
                cache_dir.stat().st_mode | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH,
            )
        except PermissionError:
            pass

        return cache_dir.is_dir() and os.access(cache_dir, os.W_OK)

    def _raise_proc_error(
        self,
        action: Literal["install", "update", "uninstall"],
        target: object,
        proc: subprocess.CompletedProcess,
    ) -> None:
        log_subprocess_output(
            logger,
            f"{self.__class__.__name__} {action}",
            proc.stdout,
            proc.stderr,
            level=py_logging.DEBUG,
        )
        exc_cls = {
            "install": BinProviderInstallError,
            "update": BinProviderUpdateError,
            "uninstall": BinProviderUninstallError,
        }[action]
        raise exc_cls(
            self.__class__.__name__,
            target,
            returncode=proc.returncode,
            output=format_subprocess_output(proc.stdout, proc.stderr),
        )

    # @validate_call
    def exec(
        self,
        bin_name: BinName | HostBinPath,
        cmd: Iterable[str | Path | int | float | bool] = (),
        cwd: Path | str = ".",
        quiet=False,
        should_log_command: bool = True,
        **kwargs,
    ) -> subprocess.CompletedProcess:
        explicit_abspath = Path(str(bin_name)).expanduser()
        if (
            explicit_abspath.is_absolute()
            and explicit_abspath.is_file()
            and os.access(explicit_abspath, os.X_OK)
        ):
            bin_abspath = explicit_abspath
        else:
            bin_abspath = self.get_abspath(str(bin_name)) or shutil.which(str(bin_name))
        assert bin_abspath, (
            f"❌ BinProvider {self.name} cannot execute bin_name {bin_name} because it could not find its abspath. (Did {self.__class__.__name__}.install({bin_name}) fail?)"
        )
        assert os.access(cwd, os.R_OK) and os.path.isdir(cwd), (
            f"cwd must be a valid, accessible directory: {cwd}"
        )
        cwd_path = Path(cwd).resolve()
        cmd = [str(bin_abspath), *(str(arg) for arg in cmd)]
        is_version_probe = len(cmd) == 2 and cmd[1] in {"--version", "-version", "-v"}
        exec_log_prefix = ACTIVE_EXEC_LOG_PREFIX.get()
        if should_log_command:
            if exec_log_prefix:
                log_with_trace_depth(
                    logger,
                    py_logging.INFO,
                    max(TRACE_DEPTH.get() - 1, 0),
                    "  $ %s",
                    format_command(cmd),
                )
            elif self.dry_run:
                logger.info(
                    "DRY RUN (%s): %s",
                    self.__class__.__name__,
                    format_command(cmd),
                )

        # https://stackoverflow.com/a/6037494/2156113
        # copy env and modify it to run the subprocess as the the designated user
        current_euid = os.geteuid()
        explicit_env = kwargs.pop("env", None)
        base_env = self.build_exec_env(
            providers=[self],
            base_env=explicit_env,
        )
        base_env["PWD"] = str(cwd_path)
        target_pw_record = self.get_pw_record(self.EUID)
        current_pw_record = self.get_pw_record(current_euid)
        run_as_uid = target_pw_record.pw_uid
        run_as_gid = target_pw_record.pw_gid

        def _env_for_identity(
            identity: Any,
            *,
            source_env: dict[str, str],
        ) -> dict[str, str]:
            env = source_env.copy()
            env["HOME"] = identity.pw_dir
            env["LOGNAME"] = identity.pw_name
            env["USER"] = identity.pw_name
            return env

        sudo_env = _env_for_identity(target_pw_record, source_env=base_env)
        fallback_env = _env_for_identity(current_pw_record, source_env=base_env)

        def drop_privileges():
            try:
                os.setuid(run_as_uid)
                os.setgid(run_as_gid)
            except Exception:
                pass

        if self.dry_run and not is_version_probe:
            return subprocess.CompletedProcess(cmd, 0, "", "skipped (dry run)")

        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)

        sudo_failure_output = None
        if current_euid != 0 and run_as_uid != current_euid:
            sudo_abspath = shutil.which("sudo", path=sudo_env["PATH"]) or shutil.which(
                "sudo",
            )
            if sudo_abspath:
                sudo_cmd = [sudo_abspath, "-n"]
                if run_as_uid != 0:
                    sudo_cmd.extend(["-u", target_pw_record.pw_name])
                sudo_cmd.extend(["--", *cmd])
                sudo_proc = subprocess.run(
                    sudo_cmd,
                    cwd=str(cwd_path),
                    env=sudo_env,
                    **kwargs,
                )
                if sudo_proc.returncode == 0:
                    return sudo_proc
                log_subprocess_output(
                    logger,
                    f"{self.__class__.__name__} sudo exec",
                    sudo_proc.stdout,
                    sudo_proc.stderr,
                    level=py_logging.DEBUG,
                )
                sudo_failure_output = format_subprocess_output(
                    sudo_proc.stdout,
                    sudo_proc.stderr,
                )

        # When running as root but dropping to a non-root user (e.g. brew),
        # use the target user's HOME/LOGNAME/USER env so the dropped-privilege
        # subprocess finds its own cache/config dirs instead of root's.
        dropped_env = (
            sudo_env
            if current_euid == 0 and run_as_uid != current_euid
            else fallback_env
        )
        proc = subprocess.run(
            cmd,
            cwd=str(cwd_path),
            env=dropped_env,
            preexec_fn=drop_privileges,
            **kwargs,
        )
        if sudo_failure_output and proc.returncode != 0:
            return subprocess.CompletedProcess(
                proc.args,
                proc.returncode,
                proc.stdout,
                "\n".join(
                    part
                    for part in (
                        proc.stderr,
                        f"Previous sudo attempt failed:\n{sudo_failure_output}",
                    )
                    if part
                ),
            )
        return proc

    # CALLING API, DONT OVERRIDE THESE:

    @final
    @binprovider_cache
    # @validate_call
    @log_method_call(include_result=True)
    def get_abspaths(
        self,
        bin_name: BinName,
        no_cache: bool = False,
    ) -> list[HostBinPath]:
        abspaths: list[HostBinPath] = []

        primary_abspath = self.get_abspath(bin_name, quiet=True, no_cache=no_cache)
        if primary_abspath:
            abspaths.append(primary_abspath)

        for abspath in bin_abspaths(bin_name, PATH=self.PATH):
            if abspath not in abspaths:
                abspaths.append(abspath)

        return abspaths

    @final
    @binprovider_cache
    # @validate_call
    @log_method_call(include_result=True)
    def get_sha256(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        no_cache: bool = False,
    ) -> Sha256 | None:
        """Get the sha256 hash of the binary at the given abspath (or equivalent hash of the underlying package)"""

        abspath = abspath or self.get_abspath(bin_name, no_cache=no_cache)
        if not abspath or not os.access(abspath, os.R_OK):
            return None

        hash_sha256 = hashlib.sha256()
        with open(abspath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
        return TypeAdapter(Sha256).validate_python(hash_sha256.hexdigest())

    @final
    @binprovider_cache
    # @validate_call
    @log_method_call(include_result=True)
    def get_abspath(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
    ) -> HostBinPath | None:
        self.setup_PATH(no_cache=no_cache)
        abspath = None
        try:
            abspath = cast(
                AbspathFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="abspath",
                ),
            )
        except Exception:
            # logger.warning(
            #     "Provider %s failed to resolve abspath for %s: %s",
            #     self.name,
            #     bin_name,
            #     err,
            # )
            if not quiet:
                raise
        if not abspath:
            return None
        result = TypeAdapter(HostBinPath).validate_python(abspath)
        return result

    @final
    @binprovider_cache
    # @validate_call
    @log_method_call(include_result=True)
    def get_version(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        quiet: bool = False,
        no_cache: bool = False,
    ) -> SemVer | None:
        version = None
        try:
            version = cast(
                VersionFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="version",
                    abspath=abspath,
                    timeout=self.version_timeout,
                ),
            )
        except Exception as err:
            logger.warning(
                "%s failed to resolve version for %s: %s",
                self.name,
                bin_name,
                err,
            )
            if not quiet:
                raise

        if not version:
            return None

        if isinstance(version, list):
            version_command = [str(arg) for arg in version]
            if not version_command:
                return None
            version_bin_name: BinName | HostBinPath = abspath or bin_name
            if version_command[0] == str(bin_name):
                version_command = version_command[1:]
            else:
                version_bin_name = version_command[0]
                version_command = version_command[1:]
            proc = self.exec(
                bin_name=version_bin_name,
                cmd=version_command,
                timeout=self.version_timeout,
                quiet=quiet,
            )
            if proc.returncode != 0:
                return None
            version = proc.stdout.strip() or proc.stderr.strip()
            if not version:
                return None

        if not isinstance(version, SemVer):
            version = SemVer.parse(version)

        return version

    @final
    @binprovider_cache
    # @validate_call
    @log_method_call(include_result=True)
    def get_install_args(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
    ) -> InstallArgs:
        install_args = None
        try:
            install_args = cast(
                InstallArgsFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="install_args",
                ),
            )
        except Exception:
            # logger.warning(
            #     "Provider %s failed to resolve install args for %s: %s",
            #     self.name,
            #     bin_name,
            #     err,
            # )
            if not quiet:
                raise

        if not install_args:
            install_args = [bin_name]
        result = TypeAdapter(InstallArgs).validate_python(install_args)
        return result

    @log_method_call(include_result=True)
    def get_packages(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
    ) -> InstallArgs:
        return self.get_install_args(bin_name, quiet=quiet, no_cache=no_cache)

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        """Override this to do any setup steps needed before installing packaged (e.g. create a venv, init an npm prefix, etc.)"""
        pass

    def supports_min_release_age(
        self,
        action: Literal["install", "update"],
        no_cache: bool = False,
    ) -> bool:
        return False

    def supports_postinstall_disable(
        self,
        action: Literal["install", "update"],
        no_cache: bool = False,
    ) -> bool:
        return False

    def _assert_min_version_satisfied(
        self,
        *,
        bin_name: BinName,
        action: Literal["install", "update"],
        loaded_version: SemVer | None,
        min_version: SemVer | None,
    ) -> None:
        if min_version and loaded_version and loaded_version < min_version:
            raise ValueError(
                f"🚫 {self.__class__.__name__}.{action} resolved {bin_name} with version {loaded_version} which does not satisfy min_version {min_version}",
            )

    @final
    @log_method_call(include_result=True)
    @validate_call
    def install(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
        dry_run: bool | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> ShallowBinary | None:
        if dry_run is not None and dry_run != self.dry_run:
            return self.get_provider_with_overrides(dry_run=dry_run).install(
                bin_name=bin_name,
                quiet=quiet,
                no_cache=no_cache,
                postinstall_scripts=postinstall_scripts,
                min_release_age=min_release_age,
                min_version=min_version,
            )
        postinstall_scripts = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        if postinstall_scripts is None:
            postinstall_scripts = not self.supports_postinstall_disable(
                "install",
                no_cache=no_cache,
            )
        if min_release_age is None:
            min_release_age = (
                7.0
                if self.supports_min_release_age("install", no_cache=no_cache)
                else 0.0
            )
        # Warn about unsupported security flags early (before load/install)
        # so warnings fire even when the binary is already cached.
        if (
            min_release_age is not None
            and min_release_age > 0
            and not self.supports_min_release_age("install", no_cache=no_cache)
        ):
            logger.warning(
                "⚠️ %s.install ignoring unsupported min_release_age=%s for provider %s",
                self.__class__.__name__,
                min_release_age,
                self.name,
            )
            min_release_age = 0.0
        if postinstall_scripts is False and not self.supports_postinstall_disable(
            "install",
            no_cache=no_cache,
        ):
            logger.warning(
                "⚠️ %s.install ignoring unsupported postinstall_scripts=%s for provider %s",
                self.__class__.__name__,
                postinstall_scripts,
                self.name,
            )
            postinstall_scripts = True
        if not no_cache:
            try:
                installed = self.load(bin_name=bin_name, quiet=True, no_cache=False)
            except Exception:
                installed = None
            if (
                installed is not None
                and min_version is not None
                and installed.loaded_version is not None
                and installed.loaded_version < min_version
            ):
                installed = self.update(
                    bin_name=bin_name,
                    quiet=quiet,
                    no_cache=False,
                    dry_run=dry_run,
                    postinstall_scripts=postinstall_scripts,
                    min_release_age=min_release_age,
                    min_version=min_version,
                )
            if installed:
                return installed

        install_handler = self._get_handler_for_action(
            bin_name=bin_name,
            handler_type="install",
        )
        if (
            getattr(
                getattr(install_handler, "__func__", install_handler),
                "__name__",
                "",
            )
            == "install_noop"
        ):
            result = self.load(bin_name=bin_name, quiet=quiet, no_cache=no_cache)
            if result is not None:
                self._assert_min_version_satisfied(
                    bin_name=bin_name,
                    action="install",
                    loaded_version=result.loaded_version,
                    min_version=min_version,
                )
            return result

        install_args = self.get_install_args(bin_name, quiet=quiet, no_cache=no_cache)
        self.setup(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
            no_cache=no_cache,
        )

        self.setup_PATH(no_cache=no_cache)
        install_log = None
        exec_log_prefix_token = ACTIVE_EXEC_LOG_PREFIX.set(
            f"⛟  Installing {bin_name} via {self.name}...",
        )
        logger.info(ACTIVE_EXEC_LOG_PREFIX.get())
        try:
            install_log = cast(
                InstallFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="install",
                    install_args=install_args,
                    packages=install_args,
                    no_cache=no_cache,
                    postinstall_scripts=postinstall_scripts,
                    min_release_age=min_release_age,
                    min_version=min_version,
                    timeout=self.install_timeout,
                ),
            )
        except Exception as err:
            install_log = f"❌ {self.__class__.__name__} Failed to install {bin_name}, got {err.__class__.__name__}: {err}"
            if not quiet:
                raise
        finally:
            ACTIVE_EXEC_LOG_PREFIX.reset(exec_log_prefix_token)

        if self.dry_run:
            # return fake ShallowBinary if we're just doing a dry run
            # no point trying to get real abspath or version if nothing was actually installed
            return ShallowBinary.model_construct(
                name=bin_name,
                description=bin_name,
                loaded_binprovider=self,
                loaded_abspath=UNKNOWN_ABSPATH,
                loaded_version=UNKNOWN_VERSION,
                loaded_sha256=UNKNOWN_SHA256,
                loaded_mtime=UNKNOWN_MTIME,
                loaded_euid=UNKNOWN_EUID,
                binproviders=[self],
            )

        self.invalidate_cache(bin_name)

        result = self.load(bin_name, quiet=True, no_cache=no_cache)
        if result is None:
            rollback_output = ""
            try:
                rollback_result = cast(
                    ActionFuncReturnValue,
                    self._call_handler_for_action(
                        bin_name=bin_name,
                        handler_type="uninstall",
                        install_args=install_args,
                        packages=install_args,
                        no_cache=no_cache,
                        postinstall_scripts=postinstall_scripts,
                        min_release_age=min_release_age,
                        min_version=min_version,
                        timeout=self.install_timeout,
                    ),
                )
                if isinstance(rollback_result, str):
                    rollback_output = rollback_result
            except Exception as err:
                rollback_output = (
                    f"Rollback after failed install also failed with "
                    f"{err.__class__.__name__}: {err}"
                )
            self.invalidate_cache(bin_name)
            if not quiet:
                install_output = (
                    f"Installed package did not produce runnable binary {bin_name!r}."
                )
                if install_log:
                    install_output += f"\n{install_log}"
                if rollback_output:
                    install_output += f"\n{rollback_output}"
                raise BinProviderInstallError(
                    self.__class__.__name__,
                    install_args,
                    output=install_output,
                )
            return None
        if result is not None:
            self._assert_min_version_satisfied(
                bin_name=bin_name,
                action="install",
                loaded_version=result.loaded_version,
                min_version=min_version,
            )
        return result

    @final
    @log_method_call(include_result=True)
    @validate_call
    def update(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
        dry_run: bool | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> ShallowBinary | None:
        if dry_run is not None and dry_run != self.dry_run:
            return self.get_provider_with_overrides(dry_run=dry_run).update(
                bin_name=bin_name,
                quiet=quiet,
                no_cache=no_cache,
                postinstall_scripts=postinstall_scripts,
                min_release_age=min_release_age,
                min_version=min_version,
            )
        postinstall_scripts = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        if postinstall_scripts is None:
            postinstall_scripts = not self.supports_postinstall_disable(
                "update",
                no_cache=no_cache,
            )
        if min_release_age is None:
            min_release_age = (
                7.0
                if self.supports_min_release_age("update", no_cache=no_cache)
                else 0.0
            )
        install_args = self.get_install_args(bin_name, quiet=quiet, no_cache=no_cache)
        if (
            min_release_age is not None
            and min_release_age > 0
            and not self.supports_min_release_age("update", no_cache=no_cache)
        ):
            logger.warning(
                "⚠️ %s.update ignoring unsupported min_release_age=%s for provider %s",
                self.__class__.__name__,
                min_release_age,
                self.name,
            )
            min_release_age = 0.0
        if postinstall_scripts is False and not self.supports_postinstall_disable(
            "update",
            no_cache=no_cache,
        ):
            logger.warning(
                "⚠️ %s.update ignoring unsupported postinstall_scripts=%s for provider %s",
                self.__class__.__name__,
                postinstall_scripts,
                self.name,
            )
            postinstall_scripts = True

        update_handler = self._get_handler_for_action(
            bin_name=bin_name,
            handler_type="update",
        )
        if (
            getattr(getattr(update_handler, "__func__", update_handler), "__name__", "")
            == "update_noop"
        ):
            return None

        self.setup(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
            no_cache=no_cache,
        )

        self.setup_PATH(no_cache=no_cache)
        update_log = None
        exec_log_prefix_token = ACTIVE_EXEC_LOG_PREFIX.set(
            f"⬆ Updating {bin_name} via {self.name}...",
        )
        logger.info(ACTIVE_EXEC_LOG_PREFIX.get())
        try:
            update_log = cast(
                ActionFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="update",
                    install_args=install_args,
                    packages=install_args,
                    no_cache=no_cache,
                    postinstall_scripts=postinstall_scripts,
                    min_release_age=min_release_age,
                    min_version=min_version,
                    timeout=self.install_timeout,
                ),
            )
        except Exception as err:
            update_log = f"❌ {self.__class__.__name__} Failed to update {bin_name}, got {err.__class__.__name__}: {err}"
            if not quiet:
                raise
        finally:
            ACTIVE_EXEC_LOG_PREFIX.reset(exec_log_prefix_token)

        if self.dry_run:
            return ShallowBinary.model_construct(
                name=bin_name,
                description=bin_name,
                loaded_binprovider=self,
                loaded_abspath=UNKNOWN_ABSPATH,
                loaded_version=UNKNOWN_VERSION,
                loaded_sha256=UNKNOWN_SHA256,
                loaded_mtime=UNKNOWN_MTIME,
                loaded_euid=UNKNOWN_EUID,
                binproviders=[self],
            )

        self.invalidate_cache(bin_name)

        result = self.load(bin_name, quiet=True, no_cache=no_cache)
        if not quiet:
            assert result is not None, (
                f"❌ {self.__class__.__name__} Unable to find version for {bin_name} after updating. PATH={self.PATH} LOG={update_log}"
            )
        if result is not None:
            self._assert_min_version_satisfied(
                bin_name=bin_name,
                action="update",
                loaded_version=result.loaded_version,
                min_version=min_version,
            )
        return result

    @final
    @log_method_call(include_result=True)
    @validate_call
    def uninstall(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
        dry_run: bool | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> bool:
        if dry_run is not None and dry_run != self.dry_run:
            return self.get_provider_with_overrides(dry_run=dry_run).uninstall(
                bin_name=bin_name,
                quiet=quiet,
                no_cache=no_cache,
                postinstall_scripts=postinstall_scripts,
                min_release_age=min_release_age,
                min_version=min_version,
            )
        postinstall_scripts = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        postinstall_scripts = (
            True if postinstall_scripts is None else postinstall_scripts
        )
        min_release_age = 0.0 if min_release_age is None else min_release_age
        had_cached_binary = self.has_cached_binary(bin_name)
        try:
            loaded_binary = self.load(
                bin_name,
                quiet=True,
                no_cache=no_cache,
            )
        except Exception:
            loaded_binary = None
        if loaded_binary is None:
            if had_cached_binary:
                self.invalidate_cache(bin_name)
            return False
        install_args = self.get_install_args(bin_name, quiet=quiet, no_cache=no_cache)
        self.setup_PATH(no_cache=no_cache)
        uninstall_result = None
        exec_log_prefix_token = ACTIVE_EXEC_LOG_PREFIX.set(
            f"🗑️ Uninstalling {bin_name} via {self.name}...",
        )
        logger.info(ACTIVE_EXEC_LOG_PREFIX.get())
        try:
            uninstall_result = cast(
                ActionFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="uninstall",
                    install_args=install_args,
                    packages=install_args,
                    no_cache=no_cache,
                    postinstall_scripts=postinstall_scripts,
                    min_release_age=min_release_age,
                    min_version=min_version,
                    timeout=self.install_timeout,
                ),
            )
        except Exception:
            if not quiet:
                raise
            return False
        finally:
            ACTIVE_EXEC_LOG_PREFIX.reset(exec_log_prefix_token)

        self.invalidate_cache(bin_name)

        if self.dry_run:
            return True

        if uninstall_result is not False:
            logger.info("🗑️ Uninstalled %s via %s", bin_name, self.name)
        return uninstall_result is not False

    @final
    @log_method_call(include_result=True)
    @validate_call
    def load(
        self,
        bin_name: BinName,
        quiet: bool = True,
        no_cache: bool = False,
    ) -> ShallowBinary | None:
        installed_abspath = self.get_abspath(bin_name, quiet=quiet, no_cache=no_cache)
        if not installed_abspath:
            return None

        result = (
            None if no_cache else self.load_cached_binary(bin_name, installed_abspath)
        )
        if result is None:
            loaded_version = self.get_version(
                bin_name,
                abspath=installed_abspath,
                quiet=quiet,
                no_cache=True,
            )
            if not loaded_version:
                return None
            loaded_sha256 = (
                self.get_sha256(
                    bin_name,
                    abspath=installed_abspath,
                    no_cache=True,
                )
                or UNKNOWN_SHA256
            )
            cache_write_result = self.write_cached_binary(
                bin_name,
                installed_abspath,
                loaded_version,
                loaded_sha256,
            )
            if cache_write_result is None:
                resolved_path = (
                    Path(installed_abspath).expanduser().resolve(strict=False)
                )
                stat_result = resolved_path.stat()
                loaded_mtime = TypeAdapter(MTimeNs).validate_python(
                    stat_result.st_mtime_ns,
                )
                loaded_euid = TypeAdapter(EUID).validate_python(stat_result.st_uid)
            else:
                loaded_mtime, loaded_euid = cache_write_result
            result = ShallowBinary.model_validate(
                {
                    "name": bin_name,
                    "binprovider": self,
                    "abspath": installed_abspath,
                    "version": loaded_version,
                    "sha256": loaded_sha256,
                    "mtime": loaded_mtime,
                    "euid": loaded_euid,
                    "binproviders": [self],
                },
            )

        logger.info(
            format_loaded_binary(
                "☑️ Loaded",
                installed_abspath,
                result.loaded_version,
                self,
                str(bin_name),
            ),
            extra={"abx_cli_duplicate_stdout": True},
        )
        return result


class EnvProvider(BinProvider):
    name: BinProviderName = "env"
    _log_emoji = "🌍"
    INSTALLER_BIN: BinName = "which"
    PATH: PATHStr = (
        DEFAULT_ENV_PATH  # Ambient runtime PATH; no provider-specific setup step.
    )
    install_root: Path | None = Field(
        default_factory=lambda: (
            abxpkg_install_root_default("env") or (DEFAULT_LIB_DIR / "env")
        ),
    )

    overrides: "BinProviderOverrides" = {
        "*": {
            "version": "self.default_version_handler",
            "abspath": "self.default_abspath_handler",
            "install_args": "self.default_install_args_handler",
            "install": "self.install_noop",
            "update": "self.update_noop",
            "uninstall": "self.uninstall_noop",
        },
        "python": {
            "abspath": "self.python_abspath_handler",
            "version": "{}.{}.{}".format(*sys.version_info[:3]),
        },
    }

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH lazily with install_root/bin ahead of the ambient PATH."""
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "bin"
        if self.bin_dir is not None:
            self.bin_dir.mkdir(parents=True, exist_ok=True)
            self.PATH = self._merge_PATH(
                self.bin_dir,
                PATH=self.PATH,
                prepend=True,
            )
        super().setup_PATH(no_cache=no_cache)

    def INSTALLER_BINARY(self, no_cache: bool = False) -> ShallowBinary:
        if not no_cache and self._INSTALLER_BINARY and self._INSTALLER_BINARY.is_valid:
            return self._INSTALLER_BINARY

        derived_env_path = self.derived_env_path
        if not no_cache and derived_env_path and derived_env_path.is_file():
            cache = load_derived_cache(derived_env_path)
            for cached_record in cache.values():
                if not isinstance(cached_record, dict):
                    continue
                if cached_record.get("provider_name") != self.name or cached_record.get(
                    "bin_name",
                ) != str(self.INSTALLER_BIN):
                    continue
                cached_abspath = cached_record.get("abspath")
                if not isinstance(cached_abspath, str):
                    continue
                loaded = self.load_cached_binary(
                    self.INSTALLER_BIN,
                    Path(cached_abspath),
                )
                if loaded and loaded.loaded_abspath:
                    self._INSTALLER_BINARY = loaded
                    return loaded

        env_provider = EnvProvider(
            install_root=None,
            bin_dir=None,
        ).get_provider_with_overrides(
            overrides={
                "*": {
                    "version": ["echo", "1.0.0"],
                },
            },
        )

        env_var = f"{self.INSTALLER_BIN.upper()}_BINARY"
        manual = os.environ.get(env_var)
        if manual and os.path.isabs(manual) and Path(manual).is_file():
            env_provider.PATH = env_provider._merge_PATH(
                str(Path(manual).parent),
                PATH=env_provider.PATH,
                prepend=True,
            )

        loaded = env_provider.load(bin_name=self.INSTALLER_BIN, no_cache=no_cache)
        if loaded and loaded.loaded_abspath:
            if loaded.loaded_version and loaded.loaded_sha256:
                self.write_cached_binary(
                    self.INSTALLER_BIN,
                    loaded.loaded_abspath,
                    loaded.loaded_version,
                    loaded.loaded_sha256,
                    resolved_provider_name=(
                        loaded.loaded_binprovider.name
                        if loaded.loaded_binprovider is not None
                        else self.name
                    ),
                    cache_kind="dependency",
                )
            self._INSTALLER_BINARY = loaded
            return self._INSTALLER_BINARY

        return super().INSTALLER_BINARY(no_cache=no_cache)

    def _link_loaded_binary(
        self,
        bin_name: BinName | str,
        abspath: HostBinPath | Path,
    ) -> HostBinPath:
        source_path = Path(abspath).expanduser().absolute()
        if (
            self.bin_dir is not None
            and source_path.parent == self.bin_dir
            and source_path.is_symlink()
        ):
            return TypeAdapter(HostBinPath).validate_python(source_path)
        target = source_path
        if self.bin_dir is None:
            return TypeAdapter(HostBinPath).validate_python(target)

        link_name = Path(str(bin_name)).name
        if not link_name or link_name in {".", ".."} or "/" in str(bin_name):
            return TypeAdapter(HostBinPath).validate_python(target)

        link_path = self.bin_dir / link_name
        if link_path.exists() or link_path.is_symlink():
            if link_path.is_symlink() and link_path.readlink() == target:
                return TypeAdapter(HostBinPath).validate_python(link_path)
            link_path.unlink()
        link_path.symlink_to(target)
        return TypeAdapter(HostBinPath).validate_python(link_path)

    def _is_managed_by_other_provider(
        self,
        abspath: HostBinPath | Path,
    ) -> bool:
        if self.install_root is None:
            return False

        lib_dir = self.install_root.parent
        if not lib_dir.is_dir():
            return False

        resolved_abspath = Path(abspath).expanduser().resolve(strict=False)
        for provider_root in lib_dir.iterdir():
            if not provider_root.is_dir() or provider_root == self.install_root:
                continue
            try:
                resolved_abspath.relative_to(provider_root.resolve(strict=False))
            except ValueError:
                continue
            return True

        return False

    def python_abspath_handler(
        self,
        bin_name: BinName,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath:
        self.setup_PATH(no_cache=no_cache)
        return self._link_loaded_binary(bin_name, Path(sys.executable).absolute())

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> "AbspathFuncReturnValue":
        bin_name_str = str(bin_name)
        abspath = None
        if self.bin_dir is not None:
            abspath = bin_abspath(bin_name_str, PATH=str(self.bin_dir))
        if not abspath:
            abspath = bin_abspath(bin_name_str, PATH=self.PATH)
        if not abspath:
            return None
        return self._link_loaded_binary(bin_name_str, abspath)

    def supports_min_release_age(
        self,
        action: Literal["install", "update"],
        no_cache: bool = False,
    ) -> bool:
        return False

    def supports_postinstall_disable(
        self,
        action: Literal["install", "update"],
        no_cache: bool = False,
    ) -> bool:
        return False

    @remap_kwargs({"packages": "install_args"})
    @log_method_call(include_result=True)
    def install_noop(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> str:
        """The env BinProvider is ready-only and does not install any packages, so this is a no-op"""
        return "env is ready-only and just checks for existing binaries in $PATH"

    @remap_kwargs({"packages": "install_args"})
    @log_method_call(include_result=True)
    def update_noop(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> str:
        return "env is read-only and just checks for existing binaries in $PATH"

    @remap_kwargs({"packages": "install_args"})
    @log_method_call(include_result=True)
    def uninstall_noop(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> bool:
        return False

    @log_method_call(include_result=True)
    def has_cached_binary(self, bin_name: BinName) -> bool:
        derived_env_path = self.derived_env_path
        if derived_env_path is None or not derived_env_path.is_file():
            return False

        cache = load_derived_cache(derived_env_path)
        cache_changed = False
        has_valid_cache = False

        for cache_key, cache_value in list(cache.items()):
            if not isinstance(cache_value, dict):
                continue

            cached_provider_name = cache_value.get("provider_name")
            cached_bin_name = cache_value.get("bin_name")
            cached_abspath = cache_value.get("abspath")
            cache_kind = cache_value.get("cache_kind")
            if (
                not isinstance(cached_provider_name, str)
                or not isinstance(cached_bin_name, str)
                or not isinstance(cached_abspath, str)
            ):
                try:
                    cached_provider_name, cached_bin_name, cached_abspath = json.loads(
                        cache_key,
                    )
                except Exception:
                    continue

            if cached_provider_name != self.name or cached_bin_name != str(bin_name):
                continue
            if not isinstance(cache_kind, str):
                cache_kind = (
                    "dependency"
                    if str(cached_bin_name) == str(self.INSTALLER_BIN)
                    else "binary"
                )
            if cache_kind != "binary":
                continue

            if self._is_managed_by_other_provider(Path(cached_abspath)):
                cache.pop(cache_key, None)
                cache_changed = True
                continue

            has_valid_cache = True

        if cache_changed:
            save_derived_cache(derived_env_path, cache)

        return has_valid_cache

    @log_method_call(include_result=True)
    def load_cached_binary(
        self,
        bin_name: BinName,
        abspath: HostBinPath,
    ) -> ShallowBinary | None:
        if self._is_managed_by_other_provider(abspath):
            self.invalidate_cache(bin_name)
            return None
        if logger.isEnabledFor(py_logging.DEBUG):
            log_with_trace_depth(
                logger,
                py_logging.DEBUG,
                TRACE_DEPTH.get(),
                "%s(%s)",
                "BinProvider.load_cached_binary",
                ", ".join(
                    (
                        summarize_value(bin_name, 80),
                        f"abspath={summarize_value(abspath, 120)}",
                    ),
                ),
            )
        return cast(Any, BinProvider.load_cached_binary).__wrapped__(
            self,
            bin_name,
            abspath,
        )

    @log_method_call()
    def write_cached_binary(
        self,
        bin_name: BinName,
        abspath: HostBinPath,
        loaded_version: SemVer,
        loaded_sha256: Sha256,
        resolved_provider_name: str | None = None,
        cache_kind: str = "binary",
    ) -> tuple[MTimeNs, EUID] | None:
        if self._is_managed_by_other_provider(abspath):
            self.invalidate_cache(bin_name)
            return None
        if logger.isEnabledFor(py_logging.DEBUG):
            log_with_trace_depth(
                logger,
                py_logging.DEBUG,
                TRACE_DEPTH.get(),
                "%s(%s)",
                "BinProvider.write_cached_binary",
                ", ".join(
                    (
                        summarize_value(bin_name, 80),
                        f"abspath={summarize_value(abspath, 120)}",
                        f"version={summarize_value(loaded_version, 80)}",
                        f"sha256={summarize_value(loaded_sha256, 21)}",
                    ),
                ),
            )
        return cast(Any, BinProvider.write_cached_binary).__wrapped__(
            self,
            bin_name,
            abspath,
            loaded_version,
            loaded_sha256,
            resolved_provider_name,
            cache_kind,
        )


############################################################################################################


AbspathFuncReturnValue = str | HostBinPath | None
VersionFuncReturnValue = (
    str | list[str] | tuple[int, ...] | tuple[str, ...] | SemVer | None
)  # list[str] is also accepted as an argv override for version probes
InstallArgsFuncReturnValue = list[str] | tuple[str, ...] | str | InstallArgs | None
PackagesFuncReturnValue = InstallArgsFuncReturnValue
InstallFuncReturnValue = str | None
ActionFuncReturnValue = str | bool | None
ProviderFuncReturnValue = (
    AbspathFuncReturnValue
    | VersionFuncReturnValue
    | InstallArgsFuncReturnValue
    | InstallFuncReturnValue
    | ActionFuncReturnValue
)


@runtime_checkable
class AbspathFuncWithArgs(Protocol):
    def __call__(
        _self,
        binprovider: "BinProvider",
        bin_name: BinName,
        **context,
    ) -> "AbspathFuncReturnValue": ...


@runtime_checkable
class VersionFuncWithArgs(Protocol):
    def __call__(
        _self,
        binprovider: "BinProvider",
        bin_name: BinName,
        **context,
    ) -> "VersionFuncReturnValue": ...


@runtime_checkable
class InstallArgsFuncWithArgs(Protocol):
    def __call__(
        _self,
        binprovider: "BinProvider",
        bin_name: BinName,
        **context,
    ) -> "InstallArgsFuncReturnValue": ...


PackagesFuncWithArgs = InstallArgsFuncWithArgs


@runtime_checkable
class InstallFuncWithArgs(Protocol):
    def __call__(
        _self,
        binprovider: "BinProvider",
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        **context: Any,
    ) -> "InstallFuncReturnValue": ...


@runtime_checkable
class ActionFuncWithArgs(Protocol):
    def __call__(
        _self,
        binprovider: "BinProvider",
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        **context: Any,
    ) -> "ActionFuncReturnValue": ...


AbspathFuncWithNoArgs = Callable[[], AbspathFuncReturnValue]
VersionFuncWithNoArgs = Callable[[], VersionFuncReturnValue]
InstallArgsFuncWithNoArgs = Callable[[], InstallArgsFuncReturnValue]
PackagesFuncWithNoArgs = InstallArgsFuncWithNoArgs
InstallFuncWithNoArgs = Callable[[], InstallFuncReturnValue]
ActionFuncWithNoArgs = Callable[[], ActionFuncReturnValue]

AbspathHandlerValue = (
    SelfMethodName
    | AbspathFuncWithNoArgs
    | AbspathFuncWithArgs
    | AbspathFuncReturnValue
)
VersionHandlerValue = (
    SelfMethodName
    | VersionFuncWithNoArgs
    | VersionFuncWithArgs
    | VersionFuncReturnValue
)
InstallArgsHandlerValue = (
    SelfMethodName
    | InstallArgsFuncWithNoArgs
    | InstallArgsFuncWithArgs
    | InstallArgsFuncReturnValue
)
PackagesHandlerValue = InstallArgsHandlerValue
InstallHandlerValue = (
    SelfMethodName
    | InstallFuncWithNoArgs
    | InstallFuncWithArgs
    | InstallFuncReturnValue
)
ActionHandlerValue = (
    SelfMethodName | ActionFuncWithNoArgs | ActionFuncWithArgs | ActionFuncReturnValue
)

HandlerType = Literal[
    "abspath",
    "version",
    "install_args",
    "packages",
    "install",
    "update",
    "uninstall",
]
HandlerValue = (
    AbspathHandlerValue
    | VersionHandlerValue
    | InstallArgsHandlerValue
    | InstallHandlerValue
    | ActionHandlerValue
)
HandlerReturnValue = (
    AbspathFuncReturnValue
    | VersionFuncReturnValue
    | InstallArgsFuncReturnValue
    | InstallFuncReturnValue
    | ActionFuncReturnValue
)


class HandlerDict(TypedDict, total=False):
    PATH: PATHStr
    INSTALLER_BIN: BinName
    euid: int | None
    install_root: Path | None
    bin_dir: Path | None
    dry_run: bool
    postinstall_scripts: bool | None
    min_release_age: float | None
    install_timeout: int
    version_timeout: int
    abspath: AbspathHandlerValue
    version: VersionHandlerValue
    install_args: InstallArgsHandlerValue
    packages: InstallArgsHandlerValue
    install: InstallHandlerValue
    update: ActionHandlerValue
    uninstall: ActionHandlerValue


# Binary.overrides map BinProviderName:ProviderFieldOrHandlerPatch
# {'brew': {'install_args': [...], 'min_release_age': 0}}
BinaryOverrides = dict[BinProviderName, HandlerDict]

# BinProvider.overrides map BinName:ProviderFieldOrHandlerPatch
# {'wget': {'install_args': [...], 'version_timeout': 30}}
BinProviderOverrides = dict[BinName | Literal["*"], HandlerDict]

# Resolve forward refs at import time so downstream subclasses don't need to call model_rebuild().
ShallowBinary.model_rebuild(_types_namespace=globals())
BinProvider.model_rebuild(_types_namespace=globals())
EnvProvider.model_rebuild(_types_namespace=globals())

#!/usr/bin/env python3

__package__ = "abxpkg"

import os
import shlex
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import Field, TypeAdapter, model_validator

from .base_types import (
    DEFAULT_ABXPKG_LIB_DIR,
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abxpkg_install_root_default,
)
from .binprovider import (
    BinProviderOverrides,
    EnvProvider,
    HandlerType,
    log_method_call,
    remap_kwargs,
)
from .logging import format_subprocess_output


# Ultimate fallback when neither the constructor arg nor
# ``ABXPKG_BASH_ROOT`` nor ``ABXPKG_LIB_DIR`` is set.
DEFAULT_BASH_ROOT = DEFAULT_ABXPKG_LIB_DIR / "bash"


class BashProvider(EnvProvider):
    name: BinProviderName = "bash"
    _log_emoji = "🧪"
    INSTALLER_BIN: BinName = "bash"
    INSTALLER_BINPROVIDERS: ClassVar[tuple[BinProviderName, ...] | None] = ("env",)
    INVALIDATE_ONLY_ON_UNINSTALL: ClassVar[bool] = False

    PATH: PATHStr = ""  # Starts empty; setup_PATH() replaces it with bin_dir only.
    postinstall_scripts: bool | None = Field(default=None, repr=False)
    min_release_age: float | None = Field(default=None, repr=False)

    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("bash"),
        validation_alias="bash_root",
    )
    # detect_euid_to_use() fills this from install_root/bin and setup() creates it.
    # default_*_handler methods then read it as the writable target dir for shell shims.
    bin_dir: Path | None = Field(default=None, validation_alias="bash_bin_dir")

    overrides: BinProviderOverrides = {
        "*": {
            "version": "self.bash_version_handler",
            "abspath": "self.default_abspath_handler",
            "install_args": "self.default_install_args_handler",
            "install": "self.default_install_handler",
            "update": "self.default_update_handler",
            "uninstall": "self.default_uninstall_handler",
            "docs_url": "self.default_docs_url_handler",
            "search": "self.default_search_handler",
        },
    }

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Fill in the managed bash install_root/bin_dir defaults after validation."""
        if self.install_root is None:
            self.install_root = DEFAULT_BASH_ROOT
        if self.bin_dir is None:
            self.bin_dir = self.install_root / "bin"
        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use with bin_dir only."""
        bin_dir = self.bin_dir
        assert bin_dir is not None
        self.PATH = self._merge_PATH(bin_dir, PATH=self.PATH, prepend=True)
        super().setup_PATH(no_cache=no_cache)

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        return False

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version=None,
        no_cache: bool = False,
    ) -> None:
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(bin_dir, install_root),
                preserve_root=True,
            )
        install_root.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)

    def _literal_override_value(
        self,
        bin_name: str,
        handler_type: HandlerType,
    ) -> Any:
        """Return a literal override payload, skipping callable/self-referential handlers."""
        for overrides_for_bin in (
            self.overrides.get(bin_name, {}),
            self.overrides.get("*", {}),
        ):
            value = overrides_for_bin.get(handler_type)
            if value is None:
                continue
            if callable(value):
                continue
            if isinstance(value, str) and (
                value.startswith("self.") or value.startswith("BinProvider.")
            ):
                continue
            return value
        return None

    def _get_shell_command(
        self,
        bin_name: str,
        handler_type: HandlerType,
    ) -> str | None:
        """Normalize a literal override into the shell command string to execute."""
        value = self._literal_override_value(bin_name, handler_type)
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return shlex.join(str(part) for part in value)
        return str(value)

    @log_method_call(include_result=True)
    def _get_handler_for_action(
        self,
        bin_name: BinName,
        handler_type: HandlerType,
    ):
        if handler_type in ("install", "update", "uninstall"):
            literal = self._literal_override_value(str(bin_name), handler_type)
            if literal is not None:
                return getattr(self, f"default_{handler_type}_handler")
        return super()._get_handler_for_action(bin_name, handler_type)

    def bash_version_handler(
        self,
        bin_name: str,
        abspath: str | Path | None = None,
        **context,
    ) -> str | None:
        """Detect a script version, falling back to literal overrides for pure shell shims."""
        try:
            validated_abspath = (
                TypeAdapter(HostBinPath).validate_python(abspath) if abspath else None
            )
            version = super().default_version_handler(
                bin_name,
                abspath=validated_abspath,
                **context,
            )
            if version:
                return str(version)
        except Exception:
            pass

        if abspath or self.get_abspath(bin_name, quiet=True):
            fallback = self._literal_override_value(bin_name, "version")
            if fallback is not None:
                return str(fallback)
            return "0.0.1"
        return None

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> str:
        command = self._get_shell_command(str(bin_name), "install")
        if not command:
            raise ValueError(
                "BashProvider requires a literal overrides.install shell command",
            )
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None

        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        proc = self.exec(
            bin_name=installer_bin,
            cmd=["-c", command],
            cwd=install_root,
            timeout=timeout if timeout is not None else self.install_timeout,
            env={
                **os.environ,
                "INSTALL_ROOT": str(install_root),
                "BIN_DIR": str(bin_dir),
                "BASH_INSTALL_ROOT": str(install_root),
                "BASH_BIN_DIR": str(bin_dir),
            },
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", bin_name, proc)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> str:
        command = self._get_shell_command(
            str(bin_name),
            "update",
        ) or self._get_shell_command(
            str(bin_name),
            "install",
        )
        if not command:
            raise ValueError(
                "BashProvider requires a literal overrides.install or overrides.update shell command",
            )
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None

        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        proc = self.exec(
            bin_name=installer_bin,
            cmd=["-c", command],
            cwd=install_root,
            timeout=timeout if timeout is not None else self.install_timeout,
            env={
                **os.environ,
                "INSTALL_ROOT": str(install_root),
                "BIN_DIR": str(bin_dir),
                "BASH_INSTALL_ROOT": str(install_root),
                "BASH_BIN_DIR": str(bin_dir),
            },
        )
        if proc.returncode != 0:
            self._raise_proc_error("update", bin_name, proc)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> bool:
        command = self._get_shell_command(str(bin_name), "uninstall")
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None
        if command:
            installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert installer_bin
            proc = self.exec(
                bin_name=installer_bin,  # type: ignore[arg-type]
                cmd=["-c", command],
                cwd=install_root,
                timeout=timeout if timeout is not None else self.install_timeout,
                env={
                    **os.environ,
                    "INSTALL_ROOT": str(install_root),
                    "BIN_DIR": str(bin_dir),
                    "BASH_INSTALL_ROOT": str(install_root),
                    "BASH_BIN_DIR": str(bin_dir),
                },
            )
            if proc.returncode != 0:
                self._raise_proc_error("uninstall", bin_name, proc)

        (bin_dir / str(bin_name)).unlink(missing_ok=True)
        return True

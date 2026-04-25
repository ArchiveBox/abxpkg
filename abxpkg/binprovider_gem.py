#!/usr/bin/env python3
__package__ = "abxpkg"

import os

from pathlib import Path

from pydantic import Field, model_validator, computed_field
from typing import Self, ClassVar

from .binary import Binary
from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    abxpkg_install_root_default,
)
from .semver import SemVer
from .binprovider import (
    BinProvider,
    EnvProvider,
    DEFAULT_ENV_PATH,
    log_method_call,
    remap_kwargs,
)
from .logging import format_subprocess_output


DEFAULT_GEM_HOME = Path(os.environ.get("GEM_HOME", "~/.local/share/gem")).expanduser()


class GemProvider(BinProvider):
    name: BinProviderName = "gem"
    _log_emoji = "💎"
    INSTALLER_BIN: BinName = "gem"
    INSTALLER_BINPROVIDERS: ClassVar[tuple[BinProviderName, ...] | None] = (
        "env",
        "apt",
        "brew",
        "nix",
    )

    PATH: PATHStr = DEFAULT_ENV_PATH  # Starts with ambient system PATH; setup_PATH() prepends/appends gem bin_dir depending on whether install_root/bin_dir were overridden.

    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("gem"),
        validation_alias="gem_home",
    )
    # detect_euid_to_use() expands/fills this to the active gem bindir and setup() ensures
    # it exists before gem writes wrappers that _patch_generated_wrappers() later edits.
    bin_dir: Path | None = Field(default=None, validation_alias="gem_bindir")

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        gem_home = str(self.install_root)
        return {
            "GEM_HOME": gem_home,
            "GEM_PATH": gem_home,
        }

    @computed_field
    @property
    def is_valid(self) -> bool:
        return super().is_valid

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Resolve gem_home/bin_dir defaults and expand any user-relative paths."""
        if self.install_root is None:
            self.install_root = DEFAULT_GEM_HOME
        else:
            self.install_root = self.install_root.expanduser()
        if self.bin_dir is None:
            self.bin_dir = (self.install_root / "bin").expanduser()
        else:
            self.bin_dir = self.bin_dir.expanduser()

        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use with gem's bin_dir plus ambient PATH when using the default global gem home."""
        bin_dir = self.bin_dir
        assert bin_dir is not None
        if self.install_root != DEFAULT_GEM_HOME or "bin_dir" in self.model_fields_set:
            self.PATH = self._merge_PATH(bin_dir)
        else:
            self.PATH = self._merge_PATH(bin_dir, PATH=self.PATH)
        super().setup_PATH(no_cache=no_cache)

    def INSTALLER_BINARY(self, no_cache: bool = False):
        from . import DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

        loaded = super().INSTALLER_BINARY(no_cache=no_cache)
        raw_provider_names = os.environ.get("ABXPKG_BINPROVIDERS")
        selected_provider_names = (
            [provider_name.strip() for provider_name in raw_provider_names.split(",")]
            if raw_provider_names
            else list(DEFAULT_PROVIDER_NAMES)
        )
        if raw_provider_names:
            preferred_provider_names = selected_provider_names
        else:
            installer_binproviders = self.INSTALLER_BINPROVIDERS
            assert installer_binproviders is not None
            preferred_provider_names = list(installer_binproviders)
        dependency_providers = [
            EnvProvider(install_root=None, bin_dir=None)
            if provider_name == "env"
            else PROVIDER_CLASS_BY_NAME[provider_name]()
            for provider_name in preferred_provider_names
            if provider_name
            and provider_name in selected_provider_names
            and provider_name in PROVIDER_CLASS_BY_NAME
            and provider_name != self.name
        ]
        ruby_loaded = (
            Binary(
                name="ruby",
                binproviders=dependency_providers,
            ).load(no_cache=no_cache)
            if dependency_providers
            else None
        )
        if (
            ruby_loaded
            and ruby_loaded.loaded_abspath
            and ruby_loaded.loaded_version
            and ruby_loaded.loaded_sha256
        ):
            self.write_cached_binary(
                "ruby",
                ruby_loaded.loaded_abspath,
                ruby_loaded.loaded_version,
                ruby_loaded.loaded_sha256,
                resolved_provider_name=(
                    ruby_loaded.loaded_binprovider.name
                    if ruby_loaded.loaded_binprovider is not None
                    else self.name
                ),
                cache_kind="dependency",
            )
        return loaded

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(install_root, bin_dir),
                preserve_root=True,
            )
        install_root.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)

    def _patch_generated_wrappers(self) -> None:
        """Patch generated Ruby wrappers so they stay bound to this provider's GEM_HOME."""
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None
        gem_home = str(install_root)
        gem_use_paths_line = f'Gem.use_paths("{gem_home}", ["{gem_home}"])'

        for wrapper_path in bin_dir.iterdir():
            if not wrapper_path.is_file():
                continue

            wrapper_text = wrapper_path.read_text(encoding="utf-8")
            if (
                gem_use_paths_line in wrapper_text
                or "Gem.activate_bin_path" not in wrapper_text
            ):
                continue

            if "require 'rubygems'" in wrapper_text:
                wrapper_text = wrapper_text.replace(
                    "require 'rubygems'",
                    f"require 'rubygems'\n{gem_use_paths_line}",
                    1,
                )
            else:
                wrapper_lines = wrapper_text.splitlines()
                insert_at = (
                    1 if wrapper_lines and wrapper_lines[0].startswith("#!") else 0
                )
                wrapper_lines[insert_at:insert_at] = [gem_use_paths_line, ""]
                wrapper_text = "\n".join(wrapper_lines) + "\n"

            wrapper_path.write_text(wrapper_text, encoding="utf-8")

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)
        if min_version and not any(arg.startswith("--version") for arg in install_args):
            install_args = ["--version", f">={min_version}", *install_args]
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "install",
                "--install-dir",
                str(self.install_root),
                "--bindir",
                str(self.bin_dir),
                "--no-document",
                *install_args,
            ],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", install_args, proc)

        self._patch_generated_wrappers()
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)
        if min_version and not any(arg.startswith("--version") for arg in install_args):
            install_args = ["--version", f">={min_version}", *install_args]
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "update",
                "--install-dir",
                str(self.install_root),
                "--bindir",
                str(self.bin_dir),
                "--no-document",
                *install_args,
            ],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("update", install_args, proc)

        self._patch_generated_wrappers()
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> bool:
        install_args = install_args or self.get_install_args(bin_name)
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "uninstall",
                "--all",
                "--executables",
                "--ignore-dependencies",
                "--force",
                "-i",
                str(self.install_root),
                *install_args,
            ],
            timeout=timeout,
        )
        if proc.returncode != 0 and "is not installed in GEM_HOME" not in proc.stderr:
            self._raise_proc_error("uninstall", install_args, proc)

        bindir = self.bin_dir
        assert bindir is not None
        for install_arg in install_args:
            (bindir / install_arg).unlink(missing_ok=True)
        (bindir / bin_name).unlink(missing_ok=True)

        return True

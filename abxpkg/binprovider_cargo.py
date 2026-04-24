#!/usr/bin/env python3
__package__ = "abxpkg"

import os

from pathlib import Path

from pydantic import Field, model_validator, computed_field
from typing import Self

from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    abxpkg_install_root_default,
)
from .semver import SemVer
from .binprovider import BinProvider, log_method_call, remap_kwargs
from .logging import format_subprocess_output


DEFAULT_CARGO_HOME = Path(os.environ.get("CARGO_HOME", "~/.cargo")).expanduser()


class CargoProvider(BinProvider):
    name: BinProviderName = "cargo"
    _log_emoji = "🦀"
    INSTALLER_BIN: BinName = "cargo"

    PATH: PATHStr = ""  # Starts empty; setup_PATH() fills it with cargo_home/bin plus any install_root/bin override.

    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("cargo"),
        validation_alias="cargo_root",
    )
    # detect_euid_to_use() resolves this to the active cargo install bin dir and setup()
    # creates it when using a managed non-default cargo root.
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        cargo_home = DEFAULT_CARGO_HOME
        env: dict[str, str] = {
            "CARGO_HOME": str(cargo_home),
            "CARGO_TARGET_DIR": str(self.install_root / "target"),
        }
        if self.install_root != cargo_home:
            env["CARGO_INSTALL_ROOT"] = str(self.install_root)
        return env

    @computed_field
    @property
    def is_valid(self) -> bool:
        return super().is_valid

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Resolve Cargo's install_root/bin_dir defaults from the active cargo home."""
        if self.install_root is None:
            self.install_root = DEFAULT_CARGO_HOME
        if self.bin_dir is None:
            self.bin_dir = self.install_root / "bin"

        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from cargo_home/bin and any install_root/bin override."""
        cargo_bin_dirs = [DEFAULT_CARGO_HOME / "bin"]
        install_root = self.install_root
        assert install_root is not None
        if install_root != DEFAULT_CARGO_HOME:
            cargo_bin_dirs.insert(0, install_root / "bin")
        self.PATH = self._merge_PATH(*cargo_bin_dirs, PATH=self.PATH, prepend=True)
        super().setup_PATH(no_cache=no_cache)

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
        assert install_root is not None
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(install_root, DEFAULT_CARGO_HOME),
                preserve_root=True,
            )
        DEFAULT_CARGO_HOME.mkdir(parents=True, exist_ok=True)
        (install_root / "target").mkdir(parents=True, exist_ok=True)
        if install_root != DEFAULT_CARGO_HOME:
            bin_dir = self.bin_dir
            assert bin_dir is not None
            bin_dir.mkdir(parents=True, exist_ok=True)

    def _cargo_package_specs(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
    ) -> list[str]:
        """Extract bare cargo package names from install_args for uninstall operations."""
        install_args = list(install_args or self.get_install_args(bin_name))
        options_with_values = {
            "--version",
            "--git",
            "--branch",
            "--tag",
            "--rev",
            "--path",
            "--root",
            "--index",
            "--registry",
            "--bin",
            "--example",
            "--profile",
            "--target",
            "--target-dir",
            "--config",
            "-j",
            "--jobs",
            "-Z",
        }
        package_specs: list[str] = []
        skip_next = False
        for arg in install_args:
            if skip_next:
                skip_next = False
                continue
            if arg in options_with_values:
                skip_next = True
                continue
            if arg.startswith("-"):
                continue
            package_specs.append(arg)
        return package_specs or [bin_name]

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

        cargo_install_args = ["--locked"]
        if self.install_root != DEFAULT_CARGO_HOME:
            cargo_install_args.extend(["--root", str(self.install_root)])

        proc = self.exec(
            bin_name=installer_bin,
            cmd=["install", *cargo_install_args, *install_args],
            timeout=timeout,
        )
        proc_output = format_subprocess_output(proc.stdout, proc.stderr)
        if (
            proc.returncode != 0
            and "--locked" in cargo_install_args
            and "lock file version 4 requires `-Znext-lockfile-bump`" in proc_output
        ):
            proc = self.exec(
                bin_name=installer_bin,
                cmd=["install", *cargo_install_args[1:], *install_args],
                timeout=timeout,
            )
        if proc.returncode != 0:
            self._raise_proc_error("install", install_args, proc)
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

        cargo_install_args = ["--locked"]
        if self.install_root != DEFAULT_CARGO_HOME:
            cargo_install_args.extend(["--root", str(self.install_root)])

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "install",
                "--force",
                *cargo_install_args,
                *install_args,
            ],
            timeout=timeout,
        )
        proc_output = format_subprocess_output(proc.stdout, proc.stderr)
        if (
            proc.returncode != 0
            and "--locked" in cargo_install_args
            and "lock file version 4 requires `-Znext-lockfile-bump`" in proc_output
        ):
            proc = self.exec(
                bin_name=installer_bin,
                cmd=[
                    "install",
                    "--force",
                    *cargo_install_args[1:],
                    *install_args,
                ],
                timeout=timeout,
            )
        if proc.returncode != 0:
            self._raise_proc_error("update", install_args, proc)
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
        package_specs = self._cargo_package_specs(
            bin_name,
            install_args=install_args,
        )
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "uninstall",
                *(
                    ["--root", str(self.install_root)]
                    if self.install_root is not None
                    and self.install_root != DEFAULT_CARGO_HOME
                    else []
                ),
                *package_specs,
            ],
            timeout=timeout,
        )
        if proc.returncode != 0 and "did not match any packages" not in proc.stderr:
            self._raise_proc_error("uninstall", package_specs, proc)

        return True

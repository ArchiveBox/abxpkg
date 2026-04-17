#!/usr/bin/env python
__package__ = "abxpkg"

import os
import pwd
import sys
import shutil
import importlib
import inspect
import logging as py_logging
import subprocess
import tempfile
from pathlib import Path

from typing import Any

from .binary import Binary
from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .semver import SemVer
from .binprovider import (
    BinProvider,
    EnvProvider,
    OPERATING_SYSTEM,
    DEFAULT_PATH,
    remap_kwargs,
)
from .logging import (
    format_command,
    format_subprocess_output,
    get_logger,
    log_subprocess_output,
)

logger = get_logger(__name__)

SYSTEM_TEMP_DIR = tempfile.gettempdir()


def pyinfra_package_install(
    pkg_names: InstallArgs,
    pyinfra_abspath: str,
    installer_module: str = "auto",
    installer_extra_kwargs: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> str:
    if isinstance(pkg_names, str):
        pkg_names = pkg_names.split(" ")
    else:
        pkg_names = list(pkg_names)

    _sudo_user: str | None = None
    if installer_module == "auto":
        is_macos = OPERATING_SYSTEM == "darwin"
        if is_macos:
            installer_module = "operations.brew.packages"
        else:
            installer_module = "operations.server.packages"
    else:
        # TODO: non-stock pyinfra modules from other libraries?
        assert installer_module.startswith("operations.")

    # Homebrew refuses to run as root, so when we're invoked as root we have
    # to drop privileges to the user that owns ``brew``. Previously this was
    # only wired up for the macOS auto-detect branch, which left live Linux
    # (``linuxbrew``) installs broken whenever the caller happened to be root.
    if installer_module == "operations.brew.packages" and os.geteuid() == 0:
        try:
            brew_abspath = shutil.which("brew")
            if brew_abspath:
                brew_owner_uid = Path(brew_abspath).resolve().stat().st_uid
                if brew_owner_uid != 0:
                    _sudo_user = pwd.getpwuid(brew_owner_uid).pw_name
        except Exception:
            pass

    try:
        module_name, operation_name = installer_module.rsplit(".", 1)
        installer_module_obj = importlib.import_module(f"pyinfra.{module_name}")
        installer_module_op = getattr(installer_module_obj, operation_name)
        operation_signature = inspect.signature(installer_module_op)
        operation_arg_name = next(iter(operation_signature.parameters))
    except Exception as err:
        raise RuntimeError(
            f"Failed to import pyinfra installer_module {installer_module}: {err.__class__.__name__}",
        ) from err

    accepted_kwargs = {
        name
        for name, parameter in operation_signature.parameters.items()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }
    operation_kwargs: dict[str, Any] = {
        operation_arg_name: pkg_names,
        **({"_sudo": True, "_sudo_user": _sudo_user} if _sudo_user is not None else {}),
        **{
            key: value
            for key, value in (installer_extra_kwargs or {}).items()
            if key in accepted_kwargs
        },
    }

    temp_dir = Path(tempfile.mkdtemp(dir=SYSTEM_TEMP_DIR))
    if _sudo_user is not None:
        # pyinfra's brew operations drop privileges to the brew-owner user
        # (via ``_sudo_user``), and brew refuses to start if the CWD is not
        # readable by the effective user. ``mkdtemp`` defaults to mode 0700,
        # so relax it on this one path so the dropped-privileges user can
        # chdir into it. We deliberately do NOT relax it on the default
        # (no-drop) path to avoid widening permissions on the generated
        # deploy script for unrelated installs.
        try:
            os.chmod(temp_dir, 0o755)
        except OSError:
            pass
    sudo_bin = None
    try:
        deploy_path = temp_dir / "deploy.py"
        deploy_path.write_text(
            "\n".join(
                [
                    f"from pyinfra.{module_name} import {operation_name}",
                    "",
                    f"{operation_name}(",
                    f"    name={f'Install system packages: {pkg_names}'!r},",
                    *(
                        f"    {key}={value!r},"
                        for key, value in operation_kwargs.items()
                    ),
                    ")",
                    "",
                ],
            ),
            encoding="utf-8",
        )
        cmd = [pyinfra_abspath, "--yes", "@local", str(deploy_path)]
        env = os.environ.copy()
        env["TMPDIR"] = SYSTEM_TEMP_DIR
        proc = None
        sudo_failure_output = None
        if (
            OPERATING_SYSTEM != "darwin"
            and installer_module != "operations.brew.packages"
        ):
            sudo_bin = shutil.which("sudo", path=os.environ.get("PATH", DEFAULT_PATH))
            if os.geteuid() != 0 and sudo_bin:
                sudo_proc = subprocess.run(
                    [sudo_bin, "-n", "--", *cmd],
                    capture_output=True,
                    text=True,
                    cwd=temp_dir,
                    env=env,
                    timeout=timeout,
                )
                if sudo_proc.returncode == 0:
                    proc = sudo_proc
                else:
                    log_subprocess_output(
                        logger,
                        "pyinfra sudo exec",
                        sudo_proc.stdout,
                        sudo_proc.stderr,
                        level=py_logging.DEBUG,
                    )
                    sudo_failure_output = format_subprocess_output(
                        sudo_proc.stdout,
                        sudo_proc.stderr,
                    )
        if proc is None:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=temp_dir,
                env=env,
                timeout=timeout,
            )
    finally:
        if os.geteuid() != 0 and sudo_bin:
            chown_proc = subprocess.run(
                [
                    sudo_bin,
                    "-n",
                    "chown",
                    "-R",
                    f"{os.geteuid()}:{os.getegid()}",
                    str(temp_dir),
                ],
                capture_output=True,
                text=True,
            )
            if chown_proc.returncode != 0:
                log_subprocess_output(
                    logger,
                    "pyinfra sudo chown",
                    chown_proc.stdout,
                    chown_proc.stderr,
                    level=py_logging.DEBUG,
                )
        if temp_dir.exists():
            logger.info("$ %s", format_command(["rm", "-rf", str(temp_dir)]))
        shutil.rmtree(temp_dir, ignore_errors=True)

    succeeded = proc.returncode == 0
    result_text = f"Installing {pkg_names} on {OPERATING_SYSTEM} using Pyinfra {installer_module} {['failed', 'succeeded'][succeeded]}\n{proc.stdout}\n{proc.stderr}".strip()
    if sudo_failure_output and not succeeded:
        result_text = (
            f"{result_text}\n\nPrevious sudo attempt failed:\n{sudo_failure_output}"
        )

    if succeeded:
        return result_text

    if "Permission denied" in result_text:
        raise PermissionError(
            f"Installing {pkg_names} failed! Need to be root to use package manager (retry with sudo, or install manually)",
        )
    raise Exception(
        f"Installing {pkg_names} failed! (retry with sudo, or install manually)\n{result_text}",
    )


class PyinfraProvider(BinProvider):
    name: BinProviderName = "pyinfra"
    _log_emoji = "🛠️"
    INSTALLER_BIN: BinName = "pyinfra"
    PATH: PATHStr = os.environ.get(
        "PATH",
        DEFAULT_PATH,
    )  # Always ambient system PATH. Pyinfra has no bin_dir field of its own and never mutates PATH in setup().

    def INSTALLER_BINARY(self, no_cache: bool = False):
        from . import DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

        loaded = super().INSTALLER_BINARY(no_cache=no_cache)
        raw_provider_names = os.environ.get("ABXPKG_BINPROVIDERS")
        selected_provider_names = (
            [provider_name.strip() for provider_name in raw_provider_names.split(",")]
            if raw_provider_names
            else list(DEFAULT_PROVIDER_NAMES)
        )
        dependency_providers = [
            EnvProvider(install_root=None, bin_dir=None)
            if provider_name == "env"
            else PROVIDER_CLASS_BY_NAME[provider_name]()
            for provider_name in selected_provider_names
            if provider_name
            and provider_name in PROVIDER_CLASS_BY_NAME
            and provider_name != self.name
        ]
        python_loaded = (
            Binary(
                name="python",
                binproviders=dependency_providers,
            ).load(no_cache=no_cache)
            if dependency_providers
            else None
        )
        if (
            python_loaded
            and python_loaded.loaded_abspath
            and python_loaded.loaded_version
            and python_loaded.loaded_sha256
        ):
            self.write_cached_binary(
                "python",
                python_loaded.loaded_abspath,
                python_loaded.loaded_version,
                python_loaded.loaded_sha256,
                resolved_provider_name=(
                    python_loaded.loaded_binprovider.name
                    if python_loaded.loaded_binprovider is not None
                    else self.name
                ),
                cache_kind="dependency",
            )
        return loaded

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
        pyinfra_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert pyinfra_abspath

        if self.dry_run:
            logger.info(
                "DRY RUN (%s): pyinfra install %s",
                self.__class__.__name__,
                " ".join(install_args),
            )
            return f"DRY RUN: would install {install_args} via pyinfra"

        return pyinfra_package_install(
            pkg_names=install_args,
            pyinfra_abspath=str(pyinfra_abspath),
            installer_module="auto",
            installer_extra_kwargs={},
            timeout=timeout,
        )

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
        pyinfra_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert pyinfra_abspath

        if self.dry_run:
            logger.info(
                "DRY RUN (%s): pyinfra update %s",
                self.__class__.__name__,
                " ".join(install_args),
            )
            return f"DRY RUN: would update {install_args} via pyinfra"

        return pyinfra_package_install(
            pkg_names=install_args,
            pyinfra_abspath=str(pyinfra_abspath),
            installer_module="auto",
            installer_extra_kwargs={
                "latest": True,
            },
            timeout=timeout,
        )

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
        pyinfra_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert pyinfra_abspath

        if self.dry_run:
            logger.info(
                "DRY RUN (%s): pyinfra uninstall %s",
                self.__class__.__name__,
                " ".join(install_args),
            )
            return True

        pyinfra_package_install(
            pkg_names=install_args,
            pyinfra_abspath=str(pyinfra_abspath),
            installer_module="auto",
            installer_extra_kwargs={
                "present": False,
            },
            timeout=timeout,
        )
        return True


if __name__ == "__main__":
    result = pyinfra = PyinfraProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(pyinfra, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)

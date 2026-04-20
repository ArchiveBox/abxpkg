#!/usr/bin/env python
__package__ = "abxpkg"

import os
import sys
import json
import shutil
import logging as py_logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .binary import Binary
from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .semver import SemVer
from .shallowbinary import ShallowBinary
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
from .config import apply_exec_env
from .windows_compat import (
    IS_WINDOWS,
    chown_recursive,
    get_current_egid,
    get_current_euid,
)

logger = get_logger(__name__)


SYSTEM_TEMP_DIR = tempfile.gettempdir()


# Installer modules that require the ``apt_pkg`` Python extension on the
# target interpreter (see ansible.builtin.apt and the Debian/Ubuntu catchall
# auto-detect behavior of ansible.builtin.package).
_APT_INSTALLER_MODULES = frozenset(
    {
        "ansible.builtin.apt",
        "apt",
    },
)


def _interpreter_has_module(interpreter: str, module: str) -> bool:
    try:
        return (
            subprocess.run(
                [interpreter, "-c", f"import {module}"],
                capture_output=True,
                text=True,
                timeout=5,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _pick_ansible_python_interpreter(installer_module: str) -> str:
    """Return a Python interpreter suitable for running ``installer_module``.

    ``sys.executable`` is preferred so ansible runs inside the active venv,
    but some modules (notably ``ansible.builtin.apt``) require a native C
    extension (``apt_pkg``) that ships with the Debian/Ubuntu system Python
    via the ``python3-apt`` package and cannot be ``pip install``ed into an
    arbitrary venv. When that is the case we fall back to the first system
    Python on ``PATH`` that can ``import apt_pkg``.
    """

    needs_apt_pkg = installer_module in _APT_INSTALLER_MODULES or (
        installer_module == "ansible.builtin.package" and shutil.which("apt-get")
    )
    if not needs_apt_pkg:
        return sys.executable
    if _interpreter_has_module(sys.executable, "apt_pkg"):
        return sys.executable
    candidates: list[str] = []
    system_python = shutil.which("python3")
    if system_python:
        candidates.append(system_python)
    for minor in (13, 12, 11, 10):
        candidate = shutil.which(f"python3.{minor}")
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if candidate == sys.executable:
            continue
        if _interpreter_has_module(candidate, "apt_pkg"):
            return candidate
    return sys.executable


ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE = """
---
- name: Install system packages
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Install system packages
      {installer_module}:
        name: "{{{{item}}}}"
        state: {state}
{module_extra_yaml}
      loop: {pkg_names}
"""


def render_ansible_module_extra_yaml(
    module_extra_kwargs: dict[str, Any] | None = None,
) -> str:
    if not module_extra_kwargs:
        return ""

    return "".join(
        f"        {key}: {json.dumps(value)}\n"
        for key, value in module_extra_kwargs.items()
    ).rstrip("\n")


def get_homebrew_search_path() -> str | None:
    brew_abspath = shutil.which("brew", path=DEFAULT_PATH) or shutil.which("brew")
    if not brew_abspath:
        return None
    return str(Path(brew_abspath).parent)


def ansible_package_install(
    pkg_names: str | InstallArgs,
    ansible_playbook_abspath: str,
    playbook_template=ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE,
    installer_module="auto",
    state="present",
    quiet=True,
    module_extra_kwargs: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> str:
    if isinstance(pkg_names, str):
        pkg_names = pkg_names.split(" ")
    else:
        pkg_names = list(pkg_names)

    if installer_module == "community.general.homebrew":
        homebrew_path = get_homebrew_search_path()
        module_extra_kwargs = {
            **({"path": homebrew_path} if homebrew_path else {}),
            **(module_extra_kwargs or {}),
        }

    module_extra_yaml = render_ansible_module_extra_yaml(module_extra_kwargs)

    if installer_module == "auto":
        if OPERATING_SYSTEM == "darwin":
            # macOS: Use homebrew
            resolved_installer_module = "community.general.homebrew"
        else:
            # Linux: Use Ansible catchall that autodetects apt/yum/pkg/nix/etc.
            resolved_installer_module = "ansible.builtin.package"
    else:
        resolved_installer_module = installer_module

    playbook = playbook_template.format(
        pkg_names=pkg_names,
        state=state,
        installer_module=resolved_installer_module,
        module_extra_yaml=module_extra_yaml,
    )

    temp_dir = Path(tempfile.mkdtemp(dir=SYSTEM_TEMP_DIR))
    sudo_bin = None
    try:
        ansible_home = temp_dir / "tmp"
        ansible_home.mkdir(exist_ok=True)

        playbook_path = temp_dir / "install_playbook.yml"
        playbook_path.write_text(playbook)

        env = os.environ.copy()
        env["ANSIBLE_INVENTORY_UNPARSED_WARNING"] = "False"
        env["ANSIBLE_LOCALHOST_WARNING"] = "False"
        env["ANSIBLE_HOME"] = str(ansible_home)
        env["ANSIBLE_PYTHON_INTERPRETER"] = _pick_ansible_python_interpreter(
            resolved_installer_module,
        )
        env["TMPDIR"] = SYSTEM_TEMP_DIR
        apply_exec_env({"PATH": f"{Path(sys.executable).parent}{os.pathsep}"}, env)
        cmd = [
            ansible_playbook_abspath,
            "-i",
            "localhost,",
            "-c",
            "local",
            str(playbook_path),
        ]
        proc = None
        sudo_failure_output = None
        if (
            not IS_WINDOWS
            and OPERATING_SYSTEM != "darwin"
            and installer_module != "community.general.homebrew"
        ):
            sudo_bin = shutil.which("sudo", path=env["PATH"]) or shutil.which("sudo")
            if get_current_euid() != 0 and sudo_bin:
                sudo_proc = subprocess.run(
                    [
                        sudo_bin,
                        "-n",
                        "--preserve-env=PATH,HOME,LOGNAME,USER,TMPDIR,ANSIBLE_INVENTORY_UNPARSED_WARNING,ANSIBLE_LOCALHOST_WARNING,ANSIBLE_HOME,ANSIBLE_PYTHON_INTERPRETER",
                        "--",
                        *cmd,
                    ],
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
                        "ansible sudo exec",
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
        succeeded = proc.returncode == 0
        result_text = f"Installing {pkg_names} on {OPERATING_SYSTEM} using Ansible {installer_module} {['failed', 'succeeded'][succeeded]}:{proc.stdout}\n{proc.stderr}".strip()
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
    finally:
        if not IS_WINDOWS and get_current_euid() != 0 and sudo_bin:
            rc = chown_recursive(
                sudo_bin,
                temp_dir,
                get_current_euid(),
                get_current_egid(),
            )
            if rc != 0:
                log_subprocess_output(
                    logger,
                    "ansible sudo chown",
                    "",
                    f"chown -R exited with status {rc}",
                    level=py_logging.DEBUG,
                )
        if temp_dir.exists():
            logger.info("$ %s", format_command(["rm", "-rf", str(temp_dir)]))
        shutil.rmtree(temp_dir, ignore_errors=True)


class AnsibleProvider(BinProvider):
    name: BinProviderName = "ansible"
    _log_emoji = "📘"
    INSTALLER_BIN: BinName = "ansible"
    PATH: PATHStr = os.environ.get(
        "PATH",
        DEFAULT_PATH,
    )  # Always ambient system PATH. Ansible has no bin_dir field of its own and never mutates PATH in setup().

    def INSTALLER_BINARY(self, no_cache: bool = False) -> ShallowBinary:
        if not no_cache and self._INSTALLER_BINARY and self._INSTALLER_BINARY.is_valid:
            return self._INSTALLER_BINARY

        loaded = self.load(bin_name="ansible-playbook", no_cache=no_cache)
        if loaded and loaded.loaded_abspath:
            from . import DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

            raw_provider_names = os.environ.get("ABXPKG_BINPROVIDERS")
            selected_provider_names = (
                [
                    provider_name.strip()
                    for provider_name in raw_provider_names.split(",")
                ]
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
            self._INSTALLER_BINARY = loaded
            return loaded

        raise RuntimeError(
            "Ansible is not installed! To fix:\n    pip install ansible",
        )

    def get_ansible_module_extra_kwargs(self) -> dict[str, Any]:
        """Return provider-specific kwargs to splice into the ansible module block."""
        return {}

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

        ansible_playbook = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert ansible_playbook

        module_extra_kwargs = self.get_ansible_module_extra_kwargs()

        return ansible_package_install(
            pkg_names=install_args,
            ansible_playbook_abspath=str(ansible_playbook),
            quiet=True,
            playbook_template=ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE,
            installer_module="auto",
            module_extra_kwargs=module_extra_kwargs or None,
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

        ansible_playbook = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert ansible_playbook

        module_extra_kwargs = self.get_ansible_module_extra_kwargs()
        if module_extra_kwargs:
            return ansible_package_install(
                pkg_names=install_args,
                ansible_playbook_abspath=str(ansible_playbook),
                quiet=True,
                playbook_template=ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE,
                installer_module="auto",
                state="latest",
                module_extra_kwargs=module_extra_kwargs,
                timeout=timeout,
            )
        return ansible_package_install(
            pkg_names=install_args,
            ansible_playbook_abspath=str(ansible_playbook),
            quiet=True,
            playbook_template=ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE,
            installer_module="auto",
            state="latest",
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

        ansible_playbook = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert ansible_playbook

        module_extra_kwargs = self.get_ansible_module_extra_kwargs()
        if module_extra_kwargs:
            ansible_package_install(
                pkg_names=install_args,
                ansible_playbook_abspath=str(ansible_playbook),
                quiet=True,
                playbook_template=ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE,
                installer_module="auto",
                state="absent",
                module_extra_kwargs=module_extra_kwargs,
                timeout=timeout,
            )
        else:
            ansible_package_install(
                pkg_names=install_args,
                ansible_playbook_abspath=str(ansible_playbook),
                quiet=True,
                playbook_template=ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE,
                installer_module="auto",
                state="absent",
                timeout=timeout,
            )
        return True


if __name__ == "__main__":
    result = ansible = AnsibleProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(ansible, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)

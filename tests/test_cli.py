from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest
import rich_click as click
from click.testing import CliRunner

import abxpkg.cli as cli_module
from abxpkg import PROVIDER_CLASS_BY_INSTALLER_BIN, EnvProvider, PnpmProvider
from abxpkg.config import load_derived_cache, save_derived_cache


def _abxpkg_executable() -> Path:
    """Locate the installed abxpkg console script for subprocess-based tests."""

    candidate = Path(sys.executable).parent / "abxpkg"
    assert candidate.exists(), (
        "abxpkg console script must be installed in the active venv"
    )
    return candidate


def _abx_executable() -> Path:
    """Locate the installed `abx` console script for subprocess-based tests."""

    candidate = Path(sys.executable).parent / "abx"
    assert candidate.exists(), "abx console script must be installed in the active venv"
    return candidate


def _run_cli(
    script: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 600,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke a console script with a clean ABXPKG_* environment."""

    env = {
        key: value for key, value in os.environ.items() if not key.startswith("ABXPKG_")
    }
    if env_overrides:
        env.update(env_overrides)

    return subprocess.run(
        [str(script), *args],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=timeout,
        cwd=cwd,
    )


def _run_abxpkg_cli(
    *args: str,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 600,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real `abxpkg` console script with a clean env."""

    return _run_cli(
        _abxpkg_executable(),
        *args,
        env_overrides=env_overrides,
        timeout=timeout,
        cwd=cwd,
    )


def _run_abx_cli(
    *args: str,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real `abx` console script with a clean env."""

    return _run_cli(
        _abx_executable(),
        *args,
        env_overrides=env_overrides,
        timeout=timeout,
    )


def test_shebang_script_exec_replaces_launcher_so_sigterm_reaches_child(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "required_binaries": [
                    {
                        "name": "node",
                        "binproviders": "env",
                    },
                ],
            },
        ),
    )
    script = tmp_path / "sigterm-hook.js"
    script.write_text(
        """#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries node
// /// script
// ///
process.on('SIGTERM', () => { console.log('clean'); process.exit(0); });
console.log(`ready:${process.pid}`);
setInterval(() => {}, 1000);
""",
    )
    script.chmod(0o755)
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("ABXPKG_")
    }
    env["PATH"] = os.pathsep.join(
        [str(_abxpkg_executable().parent), env.get("PATH", "")],
    )

    proc = subprocess.Popen(
        [str(script)],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    ready = proc.stdout.readline() if proc.stdout else ""
    assert ready.strip() == f"ready:{proc.pid}"

    proc.terminate()
    stdout, stderr = proc.communicate(timeout=5)

    assert proc.returncode == 0, stderr
    assert "clean" in stdout


def test_env_deps_from_honors_dependency_binproviders(tmp_path):
    lib_dir = tmp_path / "lib"
    config_path = tmp_path / "config.json"
    package_root = lib_dir / "pnpm" / "packages" / "abxbus"
    config_path.write_text(
        json.dumps(
            {
                "required_binaries": [
                    {
                        "name": "abxbus",
                        "binproviders": "pnpm",
                        "min_version": "2.5.9",
                        "min_release_age": 0,
                        "overrides": {
                            "pnpm": {
                                "install_root": str(package_root),
                                "install_args": ["abxbus@2.5.9"],
                                "abspath": str(
                                    package_root
                                    / "node_modules"
                                    / "abxbus"
                                    / "dist"
                                    / "cjs"
                                    / "index.js",
                                ),
                                "version": "2.5.9",
                            },
                        },
                    },
                ],
            },
        ),
    )

    proc = _run_abxpkg_cli(
        "env",
        "--install",
        "--json",
        f"--deps-from={config_path}:required_binaries",
        "node",
        env_overrides={
            "ABXPKG_LIB_DIR": str(lib_dir),
            "NODE_MODULES_DIR": str(tmp_path / "stale" / "node_modules"),
            "NODE_MODULE_DIR": str(tmp_path / "stale" / "node_modules"),
        },
    )

    assert proc.returncode == 0, proc.stderr
    env = json.loads(proc.stdout)
    node_path = env["NODE_PATH"].split(os.pathsep)
    assert env["NODE_MODULES_DIR"] == str(package_root / "node_modules")
    assert str(package_root / "node_modules") in node_path
    assert (package_root / "node_modules" / "abxbus" / "package.json").exists()


def test_env_deps_from_projects_managed_pnpm_before_export(tmp_path):
    lib_dir = tmp_path / "lib"
    package_root = lib_dir / "npm" / "packages" / "pnpm"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "properties": {"CI_PNPM_BIN": {"default": "pnpm"}},
                "required_binaries": [
                    {
                        "name": "{CI_PNPM_BIN}",
                        "binproviders": ["npm"],
                        "min_version": "10.19.0",
                        "min_release_age": 0,
                        "overrides": {
                            "npm": {
                                "install_root": str(package_root),
                                "install_args": ["pnpm@10.19.0"],
                            },
                        },
                    },
                ],
            },
        ),
    )

    proc = _run_abxpkg_cli(
        f"--lib={lib_dir}",
        "env",
        "--install",
        "--json",
        f"--deps-from={config_path}:required_binaries",
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    projected = lib_dir / "env" / "bin" / "pnpm"
    assert Path(payload["CI_PNPM_BIN"]) == projected
    assert projected.is_symlink()
    assert projected.resolve().is_relative_to(package_root / "node_modules" / "pnpm")
    assert payload["PATH"].split(os.pathsep)[0] == str(projected.parent)
    projection_records = [
        record
        for record in load_derived_cache(lib_dir / "env" / "derived.env").values()
        if record.get("bin_name") == "pnpm" and record.get("cache_kind") == "projection"
    ]
    assert len(projection_records) == 1
    assert projection_records[0]["provider_name"] == "env"
    assert projection_records[0]["resolved_provider_name"] == "npm"
    loaded_projection = EnvProvider(install_root=lib_dir / "env").load(
        "pnpm",
        no_cache=True,
    )
    assert loaded_projection is not None
    assert loaded_projection.loaded_abspath == projected
    assert loaded_projection.loaded_binprovider is not None
    assert loaded_projection.loaded_binprovider.name == "npm"

    version = _run_abxpkg_cli(
        f"--lib={lib_dir}",
        "--binproviders=env",
        "run",
        str(projected),
        "--version",
        timeout=30,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == "10.19.0"
    refreshed_projection_records = [
        record
        for record in load_derived_cache(lib_dir / "env" / "derived.env").values()
        if record.get("bin_name") == "pnpm" and record.get("cache_kind") == "projection"
    ]
    assert len(refreshed_projection_records) == 2
    assert (
        len({record["cache_context_hash"] for record in refreshed_projection_records})
        == 2
    )
    assert {record["abspath"] for record in refreshed_projection_records} == {
        str(projected),
    }
    assert all(
        record["provider_name"] == "env" for record in refreshed_projection_records
    )
    assert all(
        record["resolved_provider_name"] == "npm"
        for record in refreshed_projection_records
    )


def test_env_deps_from_preserves_pnpm_package_launcher_execution(tmp_path):
    lib_dir = tmp_path / "lib"
    package_root = lib_dir / "pnpm" / "packages" / "zx"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "properties": {"ZX_BINARY": {"default": "zx"}},
                "required_binaries": [
                    {
                        "name": "{ZX_BINARY}",
                        "binproviders": ["env", "pnpm"],
                        "min_version": "8.8.5",
                        "min_release_age": 0,
                        "overrides": {
                            "pnpm": {
                                "install_root": str(package_root),
                                "install_args": ["zx@8.8.5"],
                                "postinstall_scripts": True,
                            },
                        },
                    },
                ],
            },
        ),
    )

    proc = _run_abxpkg_cli(
        f"--lib={lib_dir}",
        "env",
        "--install",
        "--json",
        f"--deps-from={config_path}:required_binaries",
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    projected = Path(payload["ZX_BINARY"])
    assert projected == lib_dir / "env" / "bin" / "zx"

    version = subprocess.run(
        [str(projected), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == "8.8.5"


def test_script_deps_from_loads_cached_binary_before_installing(tmp_path):
    lib_dir = tmp_path / "lib"
    package_root = lib_dir / "pnpm" / "packages" / "zx"
    install_config_path = tmp_path / "install-config.json"
    install_config_path.write_text(
        json.dumps(
            {
                "properties": {"ZX_BINARY": {"default": "zx"}},
                "required_binaries": [
                    {
                        "name": "{ZX_BINARY}",
                        "binproviders": ["pnpm"],
                        "min_version": "8.8.5",
                        "min_release_age": 0,
                        "overrides": {
                            "pnpm": {
                                "install_root": str(package_root),
                                "install_args": ["zx@8.8.5"],
                                "postinstall_scripts": True,
                            },
                        },
                    },
                ],
            },
        ),
    )

    install = _run_abxpkg_cli(
        f"--lib={lib_dir}",
        "env",
        "--install",
        "--json",
        f"--deps-from={install_config_path}:required_binaries",
        timeout=120,
    )
    assert install.returncode == 0, install.stderr

    script_config_path = tmp_path / "script-config.json"
    script_config_path.write_text(
        json.dumps(
            {
                "properties": {"ZX_BINARY": {"default": "zx"}},
                "required_binaries": [
                    {
                        "name": "{ZX_BINARY}",
                        "binproviders": ["pnpm"],
                        "min_version": "8.8.5",
                        "min_release_age": 0,
                        "overrides": {
                            "pnpm": {
                                "install_root": str(package_root),
                                "install_args": [
                                    "archivebox-abxpkg-missing-package-for-cache-regression@0.0.0",
                                ],
                                "postinstall_scripts": True,
                            },
                        },
                    },
                ],
            },
        ),
    )
    script = tmp_path / "cached-dep.py"
    script.write_text(
        f"""#!/usr/bin/env -S abxpkg run --script --deps-from={script_config_path}:required_binaries python3
# /// script
# ///
import os
from pathlib import Path
zx = Path(os.environ["ZX_BINARY"])
assert zx.exists(), zx
print("cached dependency loaded")
""",
        encoding="utf-8",
    )
    script.chmod(0o755)

    proc = _run_cli(
        script,
        env_overrides={
            "ABXPKG_LIB_DIR": str(lib_dir),
            "PATH": os.pathsep.join(
                [str(_abxpkg_executable().parent), os.environ.get("PATH", "")],
            ),
        },
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "cached dependency loaded"


@pytest.fixture(autouse=True)
def restore_abxpkg_logger():
    package_logger = logging.getLogger("abxpkg")
    original_level = package_logger.level
    original_handlers = list(package_logger.handlers)
    original_propagate = package_logger.propagate

    try:
        yield
    finally:
        package_logger.handlers.clear()
        for handler in original_handlers:
            package_logger.addHandler(handler)
        package_logger.setLevel(original_level)
        package_logger.propagate = original_propagate


def test_build_providers_uses_managed_lib_layout(tmp_path):
    old_lib_dir = os.environ.get("ABXPKG_LIB_DIR")
    os.environ["ABXPKG_LIB_DIR"] = str(tmp_path)
    try:
        providers = cli_module.build_providers(
            ["uv", "pip", "pnpm", "cargo", "env"],
            dry_run=True,
        )
    finally:
        if old_lib_dir is None:
            os.environ.pop("ABXPKG_LIB_DIR", None)
        else:
            os.environ["ABXPKG_LIB_DIR"] = old_lib_dir

    assert providers[0].install_root == tmp_path / "uv"
    assert providers[1].install_root == tmp_path / "pip"
    assert providers[2].install_root == tmp_path / "pnpm"
    assert providers[3].install_root == tmp_path / "cargo"
    assert providers[4].name == "env"
    assert all(provider.dry_run for provider in providers)


def test_parse_provider_names_uses_preferred_default_cli_order():
    old_providers = os.environ.pop("ABXPKG_BINPROVIDERS", None)
    try:
        assert cli_module.parse_provider_names(None) == list(
            cli_module.DEFAULT_PROVIDER_NAMES,
        )
    finally:
        if old_providers is not None:
            os.environ["ABXPKG_BINPROVIDERS"] = old_providers


def test_default_cli_sets_managed_lib_dir():
    old_lib_dir = os.environ.pop("ABXPKG_LIB_DIR", None)
    old_providers = os.environ.pop("ABXPKG_BINPROVIDERS", None)
    options = cli_module.build_cli_options(
        None,
        lib_dir=None,
        global_mode=None,
        binproviders="pip",
        dry_run=None,
        debug=None,
        no_cache=None,
        min_version=None,
        postinstall_scripts=None,
        min_release_age=None,
        overrides=None,
        install_root=None,
        bin_dir=None,
        euid=None,
        install_timeout=None,
        version_timeout=None,
    )

    assert options.lib_dir == cli_module.DEFAULT_ABXPKG_LIB_DIR.resolve()
    assert os.environ["ABXPKG_LIB_DIR"] == str(options.lib_dir)
    assert cli_module.build_providers(["pip"], dry_run=True)[0].install_root == (
        options.lib_dir / "pip"
    )
    if old_lib_dir is None:
        os.environ.pop("ABXPKG_LIB_DIR", None)
    else:
        os.environ["ABXPKG_LIB_DIR"] = old_lib_dir
    if old_providers is None:
        os.environ.pop("ABXPKG_BINPROVIDERS", None)
    else:
        os.environ["ABXPKG_BINPROVIDERS"] = old_providers


def test_cli_lib_none_disables_managed_mode(tmp_path):
    result = CliRunner().invoke(
        cli_module.cli,
        ["--lib=None", "--binproviders=env", "load", "python"],
        env={"ABXPKG_LIB_DIR": str(tmp_path)},
    )

    assert result.exit_code == 0
    assert "(env) python" in result.output
    assert not any(tmp_path.iterdir())


def test_cli_global_flag_disables_managed_mode(tmp_path):
    result = CliRunner().invoke(
        cli_module.cli,
        ["--global", "--binproviders=env", "load", "python"],
        env={"ABXPKG_LIB_DIR": str(tmp_path)},
    )

    assert result.exit_code == 0
    assert "(env) python" in result.output
    assert not any(tmp_path.iterdir())


def test_env_lib_none_disables_managed_mode():
    old_lib_dir = os.environ.get("ABXPKG_LIB_DIR")
    old_providers = os.environ.pop("ABXPKG_BINPROVIDERS", None)
    os.environ["ABXPKG_LIB_DIR"] = "None"
    options = cli_module.build_cli_options(
        None,
        lib_dir=None,
        global_mode=None,
        binproviders="pip",
        dry_run=None,
        debug=None,
        no_cache=None,
        min_version=None,
        postinstall_scripts=None,
        min_release_age=None,
        overrides=None,
        install_root=None,
        bin_dir=None,
        euid=None,
        install_timeout=None,
        version_timeout=None,
    )

    assert options.lib_dir == cli_module.DEFAULT_ABXPKG_LIB_DIR.resolve()
    assert os.environ.get("ABXPKG_LIB_DIR") is None
    assert cli_module.build_providers(["pip"], dry_run=True)[0].install_root is None
    if old_lib_dir is not None:
        os.environ["ABXPKG_LIB_DIR"] = old_lib_dir
    if old_providers is None:
        os.environ.pop("ABXPKG_BINPROVIDERS", None)
    else:
        os.environ["ABXPKG_BINPROVIDERS"] = old_providers


def test_install_command_uses_env_defaults(tmp_path):
    result = CliRunner().invoke(
        cli_module.cli,
        ["install", "prettier"],
        env={
            "ABXPKG_LIB_DIR": str(tmp_path),
            "ABXPKG_BINPROVIDERS": "pnpm,uv",
            "ABXPKG_DRY_RUN": "1",
        },
    )

    assert result.exit_code == 0
    assert "Installing prettier via pnpm" in result.output
    assert f"--dir={tmp_path.resolve() / 'pnpm'}" in result.output
    assert not (tmp_path / "pnpm" / "node_modules" / "prettier").exists()


def test_build_cli_options_exports_resolved_provider_names():
    old_providers = os.environ.pop("ABXPKG_BINPROVIDERS", None)
    options = cli_module.build_cli_options(
        None,
        lib_dir=None,
        global_mode=None,
        binproviders="brew,env",
        dry_run=None,
        debug=None,
        no_cache=None,
        min_version=None,
        postinstall_scripts=None,
        min_release_age=None,
        overrides=None,
        install_root=None,
        bin_dir=None,
        euid=None,
        install_timeout=None,
        version_timeout=None,
    )

    assert options.provider_names == ["brew", "env"]
    assert os.environ["ABXPKG_BINPROVIDERS"] == "brew,env"
    if old_providers is None:
        os.environ.pop("ABXPKG_BINPROVIDERS", None)
    else:
        os.environ["ABXPKG_BINPROVIDERS"] = old_providers


def test_install_command_uses_debug_env_default(tmp_path):
    result = CliRunner().invoke(
        cli_module.cli,
        ["--binproviders=env", "load", "python"],
        env={
            "ABXPKG_LIB_DIR": str(tmp_path),
            "ABXPKG_DEBUG": "1",
        },
    )

    assert result.exit_code == 0
    assert "EnvProvider.load('python')" in result.output


def test_install_command_uses_debug_flag(tmp_path):
    result = CliRunner().invoke(
        cli_module.cli,
        ["--debug=True", "--binproviders=env", "load", "python"],
        env={"ABXPKG_LIB_DIR": str(tmp_path)},
    )

    assert result.exit_code == 0
    assert "EnvProvider.load('python')" in result.output


def test_install_command_uses_no_cache_env_default(tmp_path):
    result = CliRunner().invoke(
        cli_module.cli,
        ["--binproviders=env", "load", "python"],
        env={
            "ABXPKG_LIB_DIR": str(tmp_path),
            "ABXPKG_NO_CACHE": "1",
        },
    )

    assert result.exit_code == 0
    projected = tmp_path / "env" / "bin" / "python"
    assert projected.is_symlink()
    assert Path(result.output.split()[1].strip('"')) == projected


def test_clear_command_removes_explicit_lib_dir(tmp_path):
    (tmp_path / "pip").mkdir(parents=True)
    (tmp_path / "pip" / "marker").write_text("x")

    result = CliRunner().invoke(
        cli_module.cli,
        ["clear", f"--lib={tmp_path}"],
    )

    assert result.exit_code == 0
    assert not tmp_path.exists()


def test_clear_command_uses_env_lib_dir(tmp_path):
    (tmp_path / "uv" / "venv").mkdir(parents=True)
    (tmp_path / "uv" / "venv" / "marker").write_text("x")

    result = CliRunner().invoke(
        cli_module.cli,
        ["clear"],
        env={"ABXPKG_LIB_DIR": str(tmp_path)},
    )

    assert result.exit_code == 0
    assert not tmp_path.exists()


def test_version_command_with_binary_aliases_load(tmp_path):
    result = CliRunner().invoke(
        cli_module.cli,
        ["version", f"--lib={tmp_path}", "--binproviders=env", "python3"],
    )

    assert result.exit_code == 0
    assert "(env) python3" in result.output
    assert (tmp_path / "env" / "bin" / "python3").is_symlink()


def test_expand_bare_bool_flags_rewrites_debug_before_run():
    assert cli_module._expand_bare_bool_flags(
        ["--debug", "run", "python3", "--debug"],
    ) == ["--debug=True", "run", "python3", "--debug"]


def test_expand_bare_bool_flags_rewrites_debug_before_exec():
    assert cli_module._expand_bare_bool_flags(
        ["--debug", "exec", "python3", "--debug"],
    ) == ["--debug=True", "exec", "python3", "--debug"]


# ---------------------------------------------------------------------------
# `abxpkg run` subcommand (real live subprocess-based tests)
# ---------------------------------------------------------------------------


def test_run_executes_preinstalled_binary_via_env_provider():
    """`abxpkg run` with an already-installed binary should stream its output.

    Uses ``python3`` rather than ``ls`` because BSD ``ls`` (macOS) does
    not support ``--version`` / ``-version`` / ``-v``, so the env
    provider can't ``load()`` it.
    """

    proc = _run_abxpkg_cli(
        "--binproviders=env",
        "run",
        "python3",
        "-c",
        "print('abx-run-ok')",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "abx-run-ok"
    assert proc.stderr == ""


def test_run_terminates_child_process_group(tmp_path):
    socket_dir = Path(tempfile.mkdtemp(prefix="abxpkg-signal-", dir="/tmp"))
    ready_path = socket_dir / "ready.sock"
    stopped_path = socket_dir / "stopped.sock"
    ready_listener = socket.socket(socket.AF_UNIX)
    stopped_listener = socket.socket(socket.AF_UNIX)
    ready_listener.bind(str(ready_path))
    stopped_listener.bind(str(stopped_path))
    ready_listener.listen(1)
    stopped_listener.listen(1)
    ready_listener.settimeout(8)
    stopped_listener.settimeout(8)
    script = tmp_path / "spawn_child.py"
    script.write_text(
        """import os
import signal
import socket
import subprocess
import sys
ready_path = sys.argv[1]
stopped_path = sys.argv[2]
child_code = (
    'import os, signal, socket, sys\\n'
    'ready_path, stopped_path = sys.argv[1:]\\n'
    'def stop(*_):\\n'
    '    with socket.socket(socket.AF_UNIX) as client:\\n'
    '        client.connect(stopped_path)\\n'
    '        client.sendall(b"stopped\\\\n")\\n'
    '    raise SystemExit(0)\\n'
    'signal.signal(signal.SIGTERM, stop)\\n'
    'with socket.socket(socket.AF_UNIX) as client:\\n'
    '    client.connect(ready_path)\\n'
    '    client.sendall(f"{os.getpid()}\\\\n".encode())\\n'
    'signal.pause()\\n'
)
child = subprocess.Popen(
    [sys.executable, '-c', child_code, ready_path, stopped_path],
)
signal.pause()
""",
    )
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("ABXPKG_")
    }
    proc = subprocess.Popen(
        [
            str(_abxpkg_executable()),
            "--global",
            "--binproviders=env",
            "run",
            "python3",
            str(script),
            str(ready_path),
            str(stopped_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        try:
            ready_connection, _ = ready_listener.accept()
        except TimeoutError:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)
            raise AssertionError(
                f"child did not report readiness; rc={proc.returncode} "
                f"stdout={stdout!r} stderr={stderr!r}",
            ) from None
        with ready_connection:
            child_pid = int(ready_connection.recv(64))
        proc.terminate()
        stopped_connection, _ = stopped_listener.accept()
        with stopped_connection:
            assert stopped_connection.recv(64) == b"stopped\n", (
                f"child process {child_pid} did not receive wrapper termination"
            )
        stdout, stderr = proc.communicate(timeout=8)
        assert proc.returncode is not None, (
            f"wrapper did not terminate; stdout={stdout!r} stderr={stderr!r}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        ready_listener.close()
        stopped_listener.close()
        shutil.rmtree(socket_dir)


def test_run_accepts_update_flag_after_subcommand_for_env_provider():
    proc = _run_abxpkg_cli(
        "--binproviders=env",
        "run",
        "--update",
        "python3",
        "--version",
    )

    assert proc.returncode != 0
    assert "Unable to update binary python3 via providers env" in proc.stderr


def test_run_accepts_binproviders_flag_after_subcommand():
    proc = _run_abxpkg_cli(
        "run",
        "--binproviders=env",
        "python3",
        "--version",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("Python "), proc.stdout


def test_version_subcommand_loads_normal_binary_via_env_provider():
    proc = _run_abxpkg_cli("--binproviders=env", "version", "python3")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith(" python3"), proc.stdout
    assert proc.stderr == ""


def test_version_subcommand_loads_installer_binary_via_env_provider():
    proc = _run_abxpkg_cli("--binproviders=env", "version", "uv")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith(" uv"), proc.stdout
    assert proc.stderr == ""


def test_run_passes_flag_args_through_without_requiring_dash_dash():
    """Flags after `run BINARY_NAME` must reach the binary, not click.

    Uses ``python3 --version`` instead of ``ls --help`` because macOS ships
    BSD ``ls``, which does not understand ``--help`` and exits non-zero.
    """

    proc = _run_abxpkg_cli("--binproviders=env", "run", "python3", "--version")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("Python "), proc.stdout
    assert proc.stderr == ""


def test_warm_load_uses_cached_plan_without_loading_cli_frameworks(tmp_path):
    env = {
        "ABXPKG_LIB_DIR": str(tmp_path / "lib"),
        "ABXPKG_BINPROVIDERS": "env",
    }
    first = _run_abxpkg_cli("load", "python3", env_overrides=env)

    second = _run_abxpkg_cli(
        "load",
        "python3",
        env_overrides={**env, "PYTHONPROFILEIMPORTTIME": "1"},
    )
    started_at = time.perf_counter()
    timed = _run_abxpkg_cli("load", "python3", env_overrides=env)
    elapsed = time.perf_counter() - started_at

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout
    assert "rich_click" not in second.stderr
    assert "pydantic" not in second.stderr
    assert timed.returncode == 0, timed.stderr
    assert timed.stdout == first.stdout
    assert elapsed < 0.1

    explicit_started_at = time.perf_counter()
    explicit = _run_abxpkg_cli(
        "load",
        "--binproviders=env",
        "python3",
        env_overrides={**env, "PYTHONPROFILEIMPORTTIME": "1"},
    )
    explicit_elapsed = time.perf_counter() - explicit_started_at

    assert explicit.returncode == 0, explicit.stderr
    assert explicit.stdout == first.stdout
    assert "rich_click" not in explicit.stderr
    assert "pydantic" not in explicit.stderr
    assert explicit_elapsed < 0.1

    uncached = _run_abxpkg_cli(
        "load",
        "--no-cache",
        "python3",
        env_overrides={**env, "PYTHONPROFILEIMPORTTIME": "1"},
    )

    assert uncached.returncode == 0, uncached.stderr
    assert uncached.stdout == first.stdout
    assert "rich_click" in uncached.stderr


def test_warm_load_falls_back_when_context_or_cache_is_not_plain(tmp_path):
    lib = tmp_path / "lib"
    env = {
        "ABXPKG_LIB_DIR": str(lib),
        "ABXPKG_BINPROVIDERS": "env",
    }
    first = _run_abxpkg_cli("load", "python3", env_overrides=env)
    assert first.returncode == 0, first.stderr

    cases = (
        (("load", "--debug=False", "python3"), {}),
        (("load", "--min-version=0.0.1", "python3"), {}),
        (
            (
                "load",
                f"--abspath={sys.executable}",
                "--version=1.0.0",
                "python3",
            ),
            {},
        ),
        (("load", "python3"), {"ABXPKG_DEBUG": "1"}),
        (("load", "python3"), {"PATH": os.defpath}),
    )
    for args, extra_env in cases:
        proc = _run_abxpkg_cli(
            *args,
            env_overrides={
                **env,
                **extra_env,
                "PYTHONPROFILEIMPORTTIME": "1",
            },
        )
        assert proc.returncode == 0, proc.stderr
        assert "rich_click" in proc.stderr

    derived_env = lib / "env" / "derived.env"
    original_mode = derived_env.stat().st_mode
    derived_env.chmod(0o666)
    insecure = _run_abxpkg_cli(
        "load",
        "python3",
        env_overrides={**env, "PYTHONPROFILEIMPORTTIME": "1"},
    )
    derived_env.chmod(original_mode)

    assert insecure.returncode == 0, insecure.stderr
    assert "rich_click" in insecure.stderr

    changed_order = _run_abxpkg_cli(
        "load",
        "python3",
        env_overrides={
            **env,
            "ABXPKG_BINPROVIDERS": "env,uv",
            "PYTHONPROFILEIMPORTTIME": "1",
        },
    )

    assert changed_order.returncode == 0, changed_order.stderr
    assert "rich_click" in changed_order.stderr


def test_warm_run_uses_cached_exec_plan_without_loading_cli_frameworks(tmp_path):
    args = (
        f"--lib={tmp_path}",
        "--binproviders=env",
        "run",
        "python3",
        "--version",
    )
    first = _run_abxpkg_cli(*args)
    derived_env = tmp_path / "env" / "derived.env"
    first_stat = derived_env.stat()
    records = load_derived_cache(derived_env).values()

    assert first.returncode == 0, first.stderr
    assert any(record.get("exec_plan") for record in records)
    exec_plans = [
        cast(dict[str, object], record["exec_plan"])
        for record in records
        if record.get("exec_plan")
    ]
    assert {plan["version"] for plan in exec_plans} == {6}

    second = _run_abxpkg_cli(
        *args,
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )
    second_stat = derived_env.stat()

    assert second.returncode == 0, second.stderr
    assert second.stdout.strip().startswith("Python ")
    assert "rich_click" not in second.stderr
    assert "pydantic" not in second.stderr
    assert second_stat.st_ino == first_stat.st_ino
    assert second_stat.st_mtime_ns == first_stat.st_mtime_ns

    derived_env.chmod(0o666)
    insecure_cache = _run_abxpkg_cli(
        *args,
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )
    derived_env.chmod(0o600)

    assert insecure_cache.returncode == 0, insecure_cache.stderr
    assert "rich_click" in insecure_cache.stderr

    derived_env.unlink()
    os.mkfifo(derived_env)
    fifo_cache = _run_abxpkg_cli(*args, timeout=5)

    assert fifo_cache.returncode == 0, fifo_cache.stderr
    assert derived_env.is_file()

    default_args = (f"--lib={tmp_path / 'default-lib'}", "run", "python3", "--version")
    default_first = _run_abxpkg_cli(*default_args)
    default_second = _run_abxpkg_cli(
        *default_args,
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )
    default_started_at = time.perf_counter()
    default_timed = _run_abxpkg_cli(*default_args)
    default_elapsed = time.perf_counter() - default_started_at

    assert default_first.returncode == 0, default_first.stderr
    assert default_second.returncode == 0, default_second.stderr
    assert default_timed.returncode == 0, default_timed.stderr
    assert "rich_click" not in default_second.stderr
    assert "pydantic" not in default_second.stderr
    imported_modules = {
        line.rsplit("|", 1)[-1].strip()
        for line in default_second.stderr.splitlines()
        if line.startswith("import time:") and "|" in line
    }
    assert imported_modules.isdisjoint(
        {"pathlib", "platform", "platformdirs", "shutil"},
    )
    assert default_elapsed < 0.1

    default_script = tmp_path / "default-script.py"
    default_script.write_text(
        '# /// script\n# dependencies = []\n# ///\nprint("default-script")\n',
    )
    absolute_lib = tmp_path / "absolute-lib"
    absolute_python = tmp_path / "python3"
    absolute_python.symlink_to(sys.executable)
    absolute_load = _run_abxpkg_cli(
        f"--lib={absolute_lib}",
        "load",
        str(absolute_python),
    )
    assert absolute_load.returncode == 0, absolute_load.stderr
    default_cache = absolute_lib / "env" / "derived.env"
    default_cache_stat = default_cache.stat()
    default_script_started_at = time.perf_counter()
    default_script_first = _run_abxpkg_cli(
        f"--lib={absolute_lib}",
        "run",
        "--script",
        "python3",
        str(default_script),
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )
    default_script_elapsed = time.perf_counter() - default_script_started_at

    assert default_script_first.returncode == 0, default_script_first.stderr
    assert default_script_first.stdout.strip() == "default-script"
    assert "rich_click" not in default_script_first.stderr
    assert "pydantic" not in default_script_first.stderr
    assert default_cache.stat().st_ino == default_cache_stat.st_ino
    assert default_cache.stat().st_mtime_ns == default_cache_stat.st_mtime_ns
    assert default_script_elapsed < 0.1

    cached_dependencies_lib = tmp_path / "cached-dependencies-lib"

    async def resolve_cached_dependencies(
        bus_name: str,
        *,
        lib_dir: Path = cached_dependencies_lib,
        output_env_keys: dict[str, str] | None = None,
    ) -> None:
        import abxbus

        from abxpkg.binary_service import BinaryRequestEvent, BinaryService

        bus = abxbus.EventBus(name=bus_name)
        BinaryService(
            bus,
            auto_install=False,
            provider_names="env",
            lib_dir=lib_dir,
        )
        for binary_name, min_version in (("python3", "3.0.0"), ("git", None)):
            output_env_key = (output_env_keys or {}).get(binary_name)
            await bus.emit(
                BinaryRequestEvent(
                    name=binary_name,
                    binproviders="env",
                    min_version=min_version,
                    base_env={**os.environ, output_env_key: binary_name}
                    if output_env_key
                    else None,
                ),
            ).now()
        await bus.wait_until_idle()

    import asyncio

    asyncio.run(resolve_cached_dependencies("first_script_from_binary_cache"))
    cached_dependency_records = load_derived_cache(
        cached_dependencies_lib / "env" / "derived.env",
    ).values()
    assert all(
        record.get("request_exec_projections") for record in cached_dependency_records
    )
    assert not any(
        record.get("script_exec_plans") for record in cached_dependency_records
    )
    cached_dependencies_cache = cached_dependencies_lib / "env" / "derived.env"
    cached_dependencies = load_derived_cache(cached_dependencies_cache)
    python_record = next(
        record
        for record in cached_dependencies.values()
        if record.get("bin_name") == "python3"
    )
    stale_euid_record = json.loads(json.dumps(python_record))
    for projection in stale_euid_record["request_exec_projections"].values():
        projection["validation"]["euid"] = os.geteuid() + 1
    cached_dependencies["stale-cross-euid-projection"] = stale_euid_record
    save_derived_cache(cached_dependencies_cache, cached_dependencies)
    cached_dependencies_stat = cached_dependencies_cache.stat()

    cached_script = tmp_path / "cached-dependencies.py"
    cached_script.write_text(
        '# /// script\n# dependencies = [{name = "python3", min_version = "3.0.0"}, "git"]\n# ///\n'
        'print("cached-dependencies")\n',
    )
    cached_script_first = _run_abxpkg_cli(
        f"--lib={cached_dependencies_lib}",
        "--binproviders=env",
        "run",
        "--script",
        "python3",
        str(cached_script),
        env_overrides={
            "ABXPKG_NO_CACHE": "False",
            "PYTHONPROFILEIMPORTTIME": "1",
        },
    )
    cached_script_started_at = time.perf_counter()
    cached_script_timed = _run_abxpkg_cli(
        f"--lib={cached_dependencies_lib}",
        "--binproviders=env",
        "run",
        "--script",
        "python3",
        str(cached_script),
    )
    cached_script_elapsed = time.perf_counter() - cached_script_started_at

    assert cached_script_first.returncode == 0, cached_script_first.stderr
    assert cached_script_first.stdout.strip() == "cached-dependencies"
    assert "rich_click" not in cached_script_first.stderr
    assert "pydantic" not in cached_script_first.stderr
    assert cached_dependencies_cache.stat().st_ino == cached_dependencies_stat.st_ino
    assert (
        cached_dependencies_cache.stat().st_mtime_ns
        == cached_dependencies_stat.st_mtime_ns
    )
    assert cached_script_timed.returncode == 0, cached_script_timed.stderr
    assert cached_script_timed.stdout.strip() == "cached-dependencies"
    assert cached_script_elapsed < 0.1

    changed_path = _run_abxpkg_cli(
        f"--lib={cached_dependencies_lib}",
        "--binproviders=env",
        "run",
        "--script",
        "python3",
        str(cached_script),
        env_overrides={
            "PATH": os.pathsep.join(("/usr/bin", os.environ.get("PATH", ""))),
            "PYTHONPROFILEIMPORTTIME": "1",
        },
    )

    assert changed_path.returncode == 0, changed_path.stderr
    assert changed_path.stdout.strip() == "cached-dependencies"
    assert "rich_click" in changed_path.stderr

    mismatched_script = tmp_path / "mismatched-dependency.py"
    mismatched_script.write_text(
        '# /// script\n# dependencies = [{name = "python3", min_version = "999.0.0"}, "git"]\n# ///\n'
        'print("must-not-run")\n',
    )
    mismatched = _run_abxpkg_cli(
        f"--lib={cached_dependencies_lib}",
        "--binproviders=env",
        "run",
        "--script",
        "python3",
        str(mismatched_script),
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )

    assert mismatched.returncode != 0
    assert "must-not-run" not in mismatched.stdout
    assert "rich_click" in mismatched.stderr

    duplicate_target_script = tmp_path / "duplicate-target.py"
    duplicate_target_script.write_text(
        "# /// script\n"
        '# dependencies = [{name = "python3", min_version = "999.0.0"}, '
        '{name = "python3", min_version = "3.0.0"}]\n'
        "# ///\n"
        'print("must-not-run")\n',
    )
    duplicate_target = _run_abxpkg_cli(
        f"--lib={cached_dependencies_lib}",
        "--binproviders=env",
        "run",
        "--script",
        "python3",
        str(duplicate_target_script),
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )

    assert duplicate_target.returncode != 0
    assert "must-not-run" not in duplicate_target.stdout
    assert "rich_click" in duplicate_target.stderr

    malformed_script = tmp_path / "malformed-dependency.py"
    malformed_script.write_text(
        '# /// script\n# dependencies = [{name = "python3", min_version = "3.0.0"}]\n# ///\n'
        'print("must-not-run")\n',
    )
    for label, malformed_dependency in (
        ("empty-providers", {"name": "git", "binproviders": []}),
        ("invalid-providers", {"name": "git", "binproviders": 7}),
        ("empty-name", {"name": "", "binproviders": "env"}),
    ):
        malformed_config = tmp_path / f"{label}.json"
        malformed_config.write_text(
            json.dumps({"required_binaries": [malformed_dependency]}),
        )
        malformed = _run_abxpkg_cli(
            f"--lib={cached_dependencies_lib}",
            "--binproviders=env",
            "run",
            "--script",
            f"--deps-from={malformed_config}:required_binaries",
            "python3",
            str(malformed_script),
            env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
        )

        assert malformed.returncode != 0
        assert "must-not-run" not in malformed.stdout
        assert "rich_click" in malformed.stderr

    env_key_config = tmp_path / "env-key-config.json"
    env_key_config.write_text(
        json.dumps(
            {
                "properties": {
                    "PYTHON3_BINARY": {"default": "python3"},
                    "GIT_BINARY": {"default": "git"},
                },
                "required_binaries": [
                    {
                        "name": "{PYTHON3_BINARY}",
                        "binproviders": "env",
                        "min_version": "3.0.0",
                    },
                    {"name": "{GIT_BINARY}", "binproviders": "env"},
                ],
            },
        ),
    )
    env_key_script = tmp_path / "env-key-script.py"
    env_key_script.write_text(
        "# /// script\n# dependencies = []\n# ///\n"
        'import os\nprint(os.environ["PYTHON3_BINARY"])\nprint(os.environ["GIT_BINARY"])\n',
    )
    env_key_lib = tmp_path / "env-key-lib"
    env_key_args = (
        f"--lib={env_key_lib}",
        "--binproviders=env",
        "run",
        "--script",
        f"--deps-from={env_key_config}:required_binaries",
        "python3",
        str(env_key_script),
    )
    asyncio.run(resolve_cached_dependencies("refresh_script_from_binary_cache"))
    asyncio.run(
        resolve_cached_dependencies(
            "output_env_script_from_binary_cache",
            lib_dir=env_key_lib,
            output_env_keys={"python3": "PYTHON3_BINARY", "git": "GIT_BINARY"},
        ),
    )
    projected_env_key = _run_abxpkg_cli(
        *env_key_args,
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )
    assert projected_env_key.returncode == 0, projected_env_key.stderr
    projected_paths = projected_env_key.stdout.splitlines()
    assert Path(projected_paths[0]).resolve() == Path(sys.executable).resolve()
    assert Path(projected_paths[1]).name == "git"
    assert "rich_click" not in projected_env_key.stderr

    git_binary = shutil.which("git")
    assert git_binary is not None
    git_cache = env_key_lib / "env" / "derived.env"
    corrupted_cache = load_derived_cache(git_cache)
    git_record = next(
        (
            record
            for record in corrupted_cache.values()
            if record.get("bin_name") == "git"
            and record.get("request_exec_projections")
        ),
        None,
    )
    assert git_record is not None, corrupted_cache
    git_record["abspath"] = sys.executable
    save_derived_cache(git_cache, corrupted_cache)
    corrupted_env_key = _run_abxpkg_cli(
        *env_key_args,
        env_overrides={
            "PYTHON3_BINARY": sys.executable,
            "GIT_BINARY": git_binary,
            "PYTHONPROFILEIMPORTTIME": "1",
        },
    )

    assert corrupted_env_key.returncode == 0, corrupted_env_key.stderr
    assert Path(corrupted_env_key.stdout.splitlines()[1]).name == "git"
    assert "rich_click" in corrupted_env_key.stderr

    script = tmp_path / "warm-script.py"
    script.write_text(
        '# /// script\n# dependencies = ["python3"]\n# ///\nprint("warm-script")\n',
    )
    script_args = (
        f"--lib={tmp_path / 'script-lib'}",
        "run",
        "--script",
        "python3",
        str(script),
    )
    script_first = _run_abxpkg_cli(*script_args)
    script_second = _run_abxpkg_cli(
        *script_args,
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )
    script_started_at = time.perf_counter()
    script_timed = _run_abxpkg_cli(*script_args)
    script_elapsed = time.perf_counter() - script_started_at

    assert script_first.returncode == 0, script_first.stderr
    assert script_second.returncode == 0, script_second.stderr
    assert script_second.stdout.strip() == "warm-script"
    assert "rich_click" not in script_second.stderr
    assert "pydantic" not in script_second.stderr
    assert script_timed.returncode == 0, script_timed.stderr
    assert script_timed.stdout.strip() == "warm-script"
    assert script_elapsed < 0.1

    equivalent_hooks_lib = tmp_path / "equivalent-hooks-lib"
    equivalent_hooks = []
    for hook_name in ("first", "second"):
        hook_dir = tmp_path / hook_name
        hook_dir.mkdir()
        (hook_dir / "config.json").write_text(
            json.dumps(
                {
                    "title": hook_name,
                    "required_binaries": [
                        {"name": "python3", "binproviders": "env"},
                    ],
                },
            ),
        )
        hook_script = hook_dir / "hook.py"
        hook_script.write_text(
            '# /// script\n# ///\nprint("' + hook_name + '")\n',
        )
        equivalent_hooks.append(hook_script)

    def equivalent_args(script):
        return (
            f"--lib={equivalent_hooks_lib}",
            "--binproviders=env",
            "run",
            "--script",
            "--deps-from=./config.json:required_binaries",
            "python3",
            str(script),
        )

    equivalent_first = _run_abxpkg_cli(*equivalent_args(equivalent_hooks[0]))
    equivalent_cache = equivalent_hooks_lib / "env" / "derived.env"
    equivalent_stat = equivalent_cache.stat()
    equivalent_second = _run_abxpkg_cli(
        *equivalent_args(equivalent_hooks[1]),
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )

    assert equivalent_first.returncode == 0, equivalent_first.stderr
    assert equivalent_first.stdout.strip() == "first"
    assert equivalent_second.returncode == 0, equivalent_second.stderr
    assert equivalent_second.stdout.strip() == "second"
    assert "rich_click" not in equivalent_second.stderr
    assert "pydantic" not in equivalent_second.stderr
    assert equivalent_cache.stat().st_ino == equivalent_stat.st_ino
    assert equivalent_cache.stat().st_mtime_ns == equivalent_stat.st_mtime_ns

    provider_precedence_args = tuple(
        arg
        for arg in equivalent_args(equivalent_hooks[0])
        if arg != "--binproviders=env"
    )
    implicit_providers = _run_abxpkg_cli(*provider_precedence_args)
    explicit_providers = _run_abxpkg_cli(
        f"--binproviders={','.join(cli_module.DEFAULT_PROVIDER_NAMES)}",
        *provider_precedence_args,
    )
    different_exec_env = _run_abxpkg_cli(
        *provider_precedence_args,
        env_overrides={"NODE_PATH": "/caller/node_modules"},
    )
    provider_precedence_plans = [
        plan
        for record in load_derived_cache(equivalent_cache).values()
        for plan in cast(
            dict[str, object],
            record.get("script_exec_plans", {}),
        ).values()
    ]

    assert implicit_providers.returncode == 0, implicit_providers.stderr
    assert explicit_providers.returncode == 0, explicit_providers.stderr
    assert different_exec_env.returncode == 0, different_exec_env.stderr
    assert len(provider_precedence_plans) == 4

    prepared_lib = tmp_path / "prepared-script-lib"
    prepared_script = tmp_path / "prepared-script.py"
    prepared_script.write_text(
        "#!/usr/bin/env -S abxpkg run --script python3\n"
        "# /// script\n"
        '# dependencies = ["python3"]\n'
        '# [tool.abxpkg]\n# runtime_binproviders = ["uv"]\n'
        "# ///\n"
        "import os\nprint(f\"prepared-script:{os.environ['UV_ACTIVE']}:{os.environ['USER']}\")\n",
    )
    prepared_script.chmod(0o755)
    prepared_env = {
        key: value for key, value in os.environ.items() if not key.startswith("ABXPKG_")
    }
    prepared_env.update(
        {
            "ABXPKG_LIB_DIR": str(prepared_lib),
            "ABXPKG_BINPROVIDERS": "env",
            "PATH": os.pathsep.join(
                [str(_abxpkg_executable().parent), prepared_env.get("PATH", "")],
            ),
            "PYTHON3_BINARY": sys.executable,
            "UV_ACTIVE": "1",
        },
    )
    from abxpkg import prepare_script_exec_plan

    assert prepare_script_exec_plan(prepared_script, env=prepared_env)
    prepared_cache = prepared_lib / "env" / "derived.env"
    prepared_stat = prepared_cache.stat()
    assert prepare_script_exec_plan(prepared_script, env=prepared_env)
    assert prepared_cache.stat().st_ino == prepared_stat.st_ino
    assert prepared_cache.stat().st_mtime_ns == prepared_stat.st_mtime_ns
    runtime_env = dict(prepared_env)
    runtime_env.pop("UV_ACTIVE")
    prepared_first = _run_cli(
        prepared_script,
        env_overrides={
            **runtime_env,
            "PATH": prepared_env["PATH"]
            + os.pathsep
            + str(prepared_lib / "env" / "bin"),
            "PYTHONPROFILEIMPORTTIME": "1",
            "USER": "runtime-user",
        },
    )

    import pwd

    assert prepared_first.returncode == 0, prepared_first.stderr
    assert (
        prepared_first.stdout.strip()
        == f"prepared-script:1:{pwd.getpwuid(os.geteuid()).pw_name}"
    )
    assert "rich_click" not in prepared_first.stderr
    assert "pydantic" not in prepared_first.stderr
    assert prepared_cache.stat().st_ino == prepared_stat.st_ino
    assert prepared_cache.stat().st_mtime_ns == prepared_stat.st_mtime_ns

    unrelated_projection = tmp_path / "script-lib" / "env" / "bin" / "unrelated"
    unrelated_projection.write_text("#!/bin/sh\nexit 0\n")
    unrelated_projection.chmod(0o755)
    script_after_projection = _run_abxpkg_cli(
        *script_args,
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )

    assert script_after_projection.returncode == 0, script_after_projection.stderr
    assert script_after_projection.stdout.strip() == "warm-script"
    assert "rich_click" not in script_after_projection.stderr
    assert "pydantic" not in script_after_projection.stderr

    materialized_script = tmp_path / "materialized-script.py"
    shutil.copy2(script, materialized_script)
    os.replace(materialized_script, script)
    script_after_materialization = _run_abxpkg_cli(
        *script_args,
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )

    assert script_after_materialization.returncode == 0, (
        script_after_materialization.stderr
    )
    assert script_after_materialization.stdout.strip() == "warm-script"
    assert "rich_click" not in script_after_materialization.stderr
    assert "pydantic" not in script_after_materialization.stderr

    script.write_text(
        '# /// script\n# dependencies = ["python3"]\n# ///\nprint("updated-script")\n',
    )
    script_changed = _run_abxpkg_cli(*script_args)
    script_rewarmed = _run_abxpkg_cli(
        *script_args,
        env_overrides={"PYTHONPROFILEIMPORTTIME": "1"},
    )

    assert script_changed.returncode == 0, script_changed.stderr
    assert script_changed.stdout.strip() == "updated-script"
    assert script_rewarmed.returncode == 0, script_rewarmed.stderr
    assert script_rewarmed.stdout.strip() == "updated-script"
    assert "rich_click" not in script_rewarmed.stderr
    assert "pydantic" not in script_rewarmed.stderr

    mutable_bin = tmp_path / "mutable-bin"
    mutable_bin.mkdir()
    mutable_path_args = (
        f"--lib={tmp_path / 'mutable-path-lib'}",
        "--binproviders=env",
        "run",
        "git",
        "status",
        "--short",
    )
    mutable_env = {"PATH": f"{mutable_bin}{os.pathsep}{os.environ['PATH']}"}
    dependency_script = tmp_path / "dependency-script.py"
    dependency_script.write_text(
        "# /// script\n"
        '# dependencies = ["python3", "git"]\n'
        "# ///\n"
        'print("dependency-script")\n',
    )
    dependency_script_args = (
        f"--lib={tmp_path / 'dependency-script-lib'}",
        "--binproviders=env",
        "run",
        "--script",
        "python3",
        str(dependency_script),
    )
    dependency_first = _run_abxpkg_cli(
        *dependency_script_args,
        env_overrides=mutable_env,
    )
    before_addition = _run_abxpkg_cli(
        *mutable_path_args,
        env_overrides=mutable_env,
    )
    mutable_target = mutable_bin / "git"
    mutable_target.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "git version 9.9.9"; '
        "else echo added-to-existing-path; fi\n",
    )
    mutable_target.chmod(0o755)
    after_addition = _run_abxpkg_cli(
        *mutable_path_args,
        env_overrides=mutable_env,
    )
    dependency_changed = _run_abxpkg_cli(
        *dependency_script_args,
        env_overrides=mutable_env,
    )
    dependency_cache = load_derived_cache(
        tmp_path / "dependency-script-lib" / "env" / "derived.env",
    )
    dependency_resolutions: list[dict[str, object]] = []
    for record in dependency_cache.values():
        plans = record.get("script_exec_plans")
        if not isinstance(plans, dict):
            continue
        for plan in plans.values():
            if not isinstance(plan, dict):
                continue
            resolutions = cast(dict[str, object], plan).get("resolutions")
            if not isinstance(resolutions, list):
                continue
            dependency_resolutions.extend(
                cast(dict[str, object], resolution)
                for resolution in resolutions
                if isinstance(resolution, dict)
            )

    assert dependency_first.returncode == 0, dependency_first.stderr
    assert before_addition.returncode == 0, before_addition.stderr
    assert after_addition.returncode == 0, after_addition.stderr
    assert after_addition.stdout.strip() == "added-to-existing-path"
    assert dependency_changed.returncode == 0, dependency_changed.stderr
    assert dependency_changed.stdout.strip() == "dependency-script"
    assert any(
        resolution.get("name") == "git"
        and Path(str(resolution.get("ambient_abspath"))).resolve()
        == mutable_target.resolve()
        for resolution in dependency_resolutions
    )
    dependency_restored = _run_abxpkg_cli(
        *dependency_script_args,
        env_overrides={"PATH": os.environ["PATH"], "PYTHONPROFILEIMPORTTIME": "1"},
    )
    assert dependency_restored.returncode == 0, dependency_restored.stderr
    assert "rich_click" in dependency_restored.stderr

    relative_a = tmp_path / "relative-a"
    relative_b = tmp_path / "relative-b"
    relative_a.mkdir()
    relative_b.mkdir()
    for directory, output in ((relative_a, "relative-a"), (relative_b, "relative-b")):
        target = directory / "git"
        target.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "git version 9.9.9"; '
            f"else echo {output}; fi\n",
        )
        target.chmod(0o755)
    relative_args = (
        f"--lib={tmp_path / 'relative-path-lib'}",
        "--binproviders=env",
        "run",
        "git",
    )
    relative_env = {"PATH": f".{os.pathsep}{os.environ['PATH']}"}
    from_a = _run_abxpkg_cli(
        *relative_args,
        env_overrides=relative_env,
        cwd=relative_a,
    )
    from_b = _run_abxpkg_cli(
        *relative_args,
        env_overrides=relative_env,
        cwd=relative_b,
    )

    assert from_a.returncode == 0, from_a.stderr
    assert from_a.stdout.strip() == "relative-a"
    assert from_b.returncode == 0, from_b.stderr
    assert from_b.stdout.strip() == "relative-b"

    alternate_bin = tmp_path / "alternate-bin"
    alternate_bin.mkdir()
    alternate_target = alternate_bin / "git"
    alternate_target.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "git version 9.9.9"; '
        "else echo alternate-target; fi\n",
    )
    alternate_target.chmod(0o755)
    path_args = (
        f"--lib={tmp_path / 'path-lib'}",
        "--binproviders=env",
        "run",
        "git",
        "status",
        "--short",
    )
    original_path = _run_abxpkg_cli(*path_args)
    changed_path = _run_abxpkg_cli(
        *path_args,
        env_overrides={"PATH": f"{alternate_bin}{os.pathsep}{os.environ['PATH']}"},
    )

    assert original_path.returncode == 0, original_path.stderr
    assert changed_path.returncode == 0, changed_path.stderr
    assert changed_path.stdout.strip() == "alternate-target"


def test_run_help_flag_shows_run_subcommand_help():
    proc = _run_abxpkg_cli("run", "--help")

    assert proc.returncode == 0, proc.stderr
    assert "Usage: abxpkg run" in proc.stdout
    assert "Run an installed binary" in proc.stdout


def test_exec_help_flag_shows_exec_subcommand_help():
    proc = _run_abxpkg_cli("exec", "--help")

    assert proc.returncode == 0, proc.stderr
    assert "Usage: abxpkg exec" in proc.stdout


def test_run_propagates_nonzero_exit_code_from_underlying_binary():
    """Exit codes from the underlying binary must flow back unchanged."""

    proc = _run_abxpkg_cli(
        "--binproviders=env",
        "run",
        "python3",
        "-c",
        "import sys; sys.stderr.write('boom\\n'); sys.exit(7)",
    )

    assert proc.returncode == 7
    assert proc.stdout == ""
    assert "boom" in proc.stderr


def test_run_update_skips_env_for_the_update_step(tmp_path):
    result = CliRunner().invoke(
        cli_module.cli,
        [
            f"--lib={tmp_path}",
            "--binproviders=env,brew",
            "--dry-run=True",
            "run",
            "--update",
            "shellcheck",
            "--version",
        ],
    )

    assert result.exit_code == 0
    assert "Updating shellcheck via brew" in result.output
    assert "via env" not in result.output

    script = tmp_path / "hook.sh"
    script.write_text("# /// script\n# ///\n")
    script_result = CliRunner().invoke(
        cli_module.cli,
        [
            f"--lib={tmp_path}",
            "--binproviders=env,brew",
            "--dry-run=True",
            "run",
            "--update",
            "--script",
            "shellcheck",
            str(script),
        ],
    )

    assert script_result.exit_code == 0
    assert "Updating shellcheck via brew" in script_result.output
    assert "via env" not in script_result.output


def test_run_stdout_stderr_are_separated_and_not_buffered():
    """stdout and stderr from the underlying binary must stream separately."""

    proc = _run_abxpkg_cli(
        "--binproviders=env",
        "run",
        "python3",
        "-c",
        "import sys; print('this goes to stdout', flush=True); "
        "print('this goes to stderr', file=sys.stderr, flush=True); sys.exit(7)",
    )

    assert proc.returncode == 7, proc.stderr
    assert proc.stdout == "this goes to stdout\n"
    assert "this goes to stderr" in proc.stderr
    # Nothing from abxpkg itself should leak into stdout.
    assert "abxpkg" not in proc.stdout.lower()


def test_run_without_install_exits_one_when_binary_is_missing():
    """If the binary is not installed by any provider, we exit 1."""

    proc = _run_abxpkg_cli(
        "--binproviders=env",
        "run",
        "abxpkg-test-definitely-not-installed-xyz",
        "--help",
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "abxpkg-test-definitely-not-installed-xyz" in proc.stderr


def test_run_respects_abxpkg_binproviders_env_var():
    """The ABXPKG_BINPROVIDERS env var should restrict provider resolution."""

    proc = _run_abxpkg_cli(
        "run",
        "python3",
        "-c",
        "print('from env var')",
        env_overrides={"ABXPKG_BINPROVIDERS": "env"},
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "from env var"


def test_run_binproviders_flag_overrides_env_var():
    """`--binproviders` on the command line wins over ABXPKG_BINPROVIDERS."""

    proc = _run_abxpkg_cli(
        "--binproviders=env",
        "run",
        "python3",
        "-c",
        "print('flag wins')",
        env_overrides={"ABXPKG_BINPROVIDERS": "pip,brew"},
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "flag wins"


def test_run_with_install_flag_installs_binary_before_executing(tmp_path):
    """`--install` should install the binary if needed, then exec."""

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "--install",
        "run",
        "black",
        "--version",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    # stdout must contain *only* black's --version output
    assert proc.stdout.strip().startswith("black")
    # The binary must have actually been installed under our isolated lib dir.
    installed = list((tmp_path / "pip").rglob("black"))
    assert installed, (
        f"Expected black to be installed under {tmp_path}/pip, "
        f"found nothing. stderr was:\n{proc.stderr}"
    )


def test_run_with_update_flag_installs_and_updates_before_executing(tmp_path):
    """`--update` should ensure the binary is available, then update it."""

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "--update",
        "run",
        "black",
        "--version",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("black")
    installed = list((tmp_path / "pip").rglob("black"))
    assert installed


def test_run_with_install_keeps_install_logs_off_stdout(tmp_path):
    """Install progress logs must go to stderr, stdout stays clean."""

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "--install",
        "run",
        "black",
        "--version",
        timeout=900,
        # Force a deterministic, non-TTY log level so we can assert on it.
        env_overrides={
            "ABXPKG_LIB_DIR": str(tmp_path),
            "ABXPKG_BINPROVIDERS": "pip",
        },
    )

    assert proc.returncode == 0, proc.stderr
    # stdout must be *only* the black --version output, nothing abxpkg-ish.
    stdout_lines = proc.stdout.strip().splitlines()
    assert stdout_lines
    assert stdout_lines[0].startswith("black"), stdout_lines
    for line in stdout_lines:
        assert "Installing" not in line
        assert "Loading" not in line
        assert "Binary.load" not in line


def test_run_pip_subcommand_uses_pip_provider_exec(tmp_path):
    """`abxpkg --binproviders=pip run pip show X` exercises PipProvider.exec."""

    # Prime a fresh pip venv so we control what's inside.
    install_proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "install",
        "black",
        timeout=900,
    )
    assert install_proc.returncode == 0, install_proc.stderr

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "run",
        "pip",
        "show",
        "black",
        timeout=300,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Name: black" in proc.stdout
    # Ensure the pip that ran was from our isolated venv, not the system pip:
    # pip show always prints a `Location:` line, so we must verify it points
    # *inside* the tmp_path rather than just that the header is present.
    location_lines = [
        line for line in proc.stdout.splitlines() if line.startswith("Location:")
    ]
    assert location_lines, (
        f"pip show did not emit a Location line; stdout was:\n{proc.stdout}"
    )
    assert str(tmp_path) in location_lines[0], (
        f"pip show reported {location_lines[0]!r}, which is outside the "
        f"isolated venv under {tmp_path}. The `run` subcommand probably "
        f"exec'd the system pip instead of the PipProvider's pip."
    )


@pytest.mark.parametrize(
    ("extra_args", "expected_exit", "expected_stdout"),
    [
        (("-c", "print('zero')"), 0, "zero"),
        (
            ("-c", "print('one'); import sys; sys.exit(0)"),
            0,
            "one",
        ),
        (
            ("-c", "import sys; sys.exit(3)"),
            3,
            "",
        ),
    ],
)
def test_run_forwards_variadic_positional_args_to_binary(
    extra_args,
    expected_exit,
    expected_stdout,
):
    proc = _run_abxpkg_cli(
        "--binproviders=env",
        "run",
        "python3",
        *extra_args,
    )

    assert proc.returncode == expected_exit, proc.stderr
    assert proc.stdout.strip() == expected_stdout


def test_env_command_emits_quoted_dotenv_lines_for_installable_pip_binary(tmp_path):
    lib_dir = tmp_path / "abx lib"
    proc = _run_abxpkg_cli(
        f"--lib={lib_dir}",
        "--binproviders=pip",
        "env",
        "--install",
        "black",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    stdout_lines = proc.stdout.strip().splitlines()
    assert stdout_lines
    assert any(
        line.startswith('VIRTUAL_ENV="') and str(lib_dir / "pip" / "venv") in line
        for line in stdout_lines
    ), stdout_lines
    assert any(
        line.startswith('PATH="') and str(lib_dir / "pip" / "venv" / "bin") in line
        for line in stdout_lines
    ), stdout_lines
    assert all(not line.startswith("apply_exec_env ") for line in stdout_lines)
    assert all(not line.startswith("export ") for line in stdout_lines)
    assert any((lib_dir / "pip").rglob("black"))


def test_env_command_deps_from_uses_real_required_binary_exec_env(tmp_path):
    lib = tmp_path / "lib"
    hook_runtime = lib / "uv" / "packages" / "hook-env"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "properties": {
                    "NODE_BINARY": {"default": "node"},
                },
                "required_binaries": [
                    {
                        "name": "{NODE_BINARY}",
                        "binproviders": "env",
                        "min_version": "18.0.0",
                    },
                    {
                        "name": "humanize",
                        "binproviders": "uv",
                        "install_root": "{ABXPKG_LIB_DIR}/uv/packages/hook-env",
                        "install_args": ["humanize>=4.0.0"],
                        "postinstall_scripts": False,
                        "min_release_age": 3,
                    },
                ],
            },
            indent=2,
        ),
    )

    proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "env",
        "--install",
        "--json",
        f"--deps-from={config}:required_binaries",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert Path(payload["NODE_BINARY"]).is_file()
    assert payload["VIRTUAL_ENV"] == str(hook_runtime / "venv")
    assert str(hook_runtime / "venv" / "bin") in payload["PATH"].split(os.pathsep)
    assert any((hook_runtime / "venv").rglob("humanize"))


def test_env_command_installs_forum_dl_style_uv_required_binary(tmp_path):
    lib = tmp_path / "lib"
    package_root = lib / "uv" / "packages" / "forum-dl"
    config = tmp_path / "forumdl.json"
    config.write_text(
        json.dumps(
            {
                "properties": {
                    "FORUMDL_BINARY": {"default": "forum-dl"},
                },
                "required_binaries": [
                    {
                        "name": "{FORUMDL_BINARY}",
                        "binproviders": "env,uv",
                        "min_release_age": 3,
                        "overrides": {
                            "uv": {
                                "install_root": str(package_root),
                                "install_args": ["--no-deps", "forum-dl"],
                                "postinstall_scripts": True,
                            },
                        },
                    },
                ],
            },
            indent=2,
        ),
    )

    proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "env",
        "--install",
        "--json",
        f"--deps-from={config}:required_binaries",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    entrypoint = package_root / "venv" / "bin" / "forum-dl"
    projected_entrypoint = lib / "env" / "bin" / "forum-dl"
    assert payload["FORUMDL_BINARY"] == str(projected_entrypoint)
    assert projected_entrypoint.is_symlink()
    assert projected_entrypoint.resolve() == entrypoint
    assert os.access(entrypoint, os.X_OK)


def test_env_command_installs_uv_binary_under_sudo_uid_without_root_owned_state(
    tmp_path,
):
    sudo = shutil.which("sudo")
    assert sudo, "sudo is required to verify root-invoked user-owned installs"

    home = tmp_path / "home"
    lib = home / ".config" / "abx" / "lib"
    package_root = lib / "uv" / "packages" / "forum-dl"
    config = tmp_path / "forumdl.json"
    home.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "properties": {
                    "FORUMDL_BINARY": {"default": "forum-dl"},
                },
                "required_binaries": [
                    {
                        "name": "{FORUMDL_BINARY}",
                        "binproviders": "env,uv",
                        "min_release_age": 3,
                        "overrides": {
                            "uv": {
                                "install_args": [
                                    "--no-deps",
                                    "forum-dl",
                                    "chardet==5.2.0",
                                    "pydantic==2.12.3",
                                    "pydantic-core==2.41.4",
                                    "typing-extensions>=4.14.1",
                                    "annotated-types>=0.6.0",
                                    "typing-inspection>=0.4.2",
                                    "beautifulsoup4",
                                    "soupsieve",
                                    "lxml",
                                    "requests",
                                    "urllib3",
                                    "certifi",
                                    "idna",
                                    "charset-normalizer",
                                    "tenacity",
                                    "python-dateutil",
                                    "six",
                                    "html2text",
                                    "warcio",
                                ],
                                "postinstall_scripts": True,
                            },
                        },
                    },
                ],
            },
            indent=2,
        ),
    )

    proc = subprocess.run(
        [
            sudo,
            "-n",
            "env",
            f"HOME={home}",
            f"XDG_CONFIG_HOME={home / '.config'}",
            str(_abxpkg_executable()),
            "--no-cache",
            "env",
            "--install",
            "--json",
            f"--lib={lib}",
            f"--deps-from={config}:required_binaries",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    entrypoint = package_root / "venv" / "bin" / "forum-dl"
    projected_entrypoint = lib / "env" / "bin" / "forum-dl"
    assert payload["FORUMDL_BINARY"] == str(projected_entrypoint)
    assert projected_entrypoint.is_symlink()
    assert projected_entrypoint.resolve() == entrypoint
    assert os.access(entrypoint, os.X_OK)

    abx_config = home / ".config" / "abx"
    root_owned = [
        path
        for path in [abx_config, *abx_config.rglob("*")]
        if path.lstat().st_uid == 0
    ]
    assert root_owned == []


def test_env_command_exports_and_runs_projected_host_brew(
    tmp_path,
    test_machine,
):
    host_brew = Path(test_machine.require_tool("brew")).absolute()
    while (
        host_brew.is_symlink()
        and host_brew.parent.name == "bin"
        and host_brew.parent.parent.name == "env"
    ):
        target = host_brew.readlink()
        host_brew = target if target.is_absolute() else host_brew.parent / target
    host_prefix_result = subprocess.run(
        [str(host_brew), "--prefix"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert host_prefix_result.returncode == 0, host_prefix_result.stderr
    assert host_prefix_result.stderr == ""
    host_prefix = Path(host_prefix_result.stdout.strip())

    lib = tmp_path / "lib"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "properties": {"CI_BREW_BIN": {"default": "brew"}},
                "required_binaries": [
                    {"name": "{CI_BREW_BIN}", "binproviders": ["env"]},
                ],
            },
        ),
    )
    managed_prefix = lib / "brew"
    proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "env",
        "--install",
        "--json",
        f"--deps-from={config}:required_binaries",
        env_overrides={
            "HOMEBREW_PREFIX": str(managed_prefix),
            "HOMEBREW_CELLAR": str(managed_prefix / "Cellar"),
        },
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    projected = lib / "env" / "bin" / "brew"
    assert projected.is_symlink()
    assert projected.samefile(host_brew)
    assert Path(payload["CI_BREW_BIN"]) == projected

    result = _run_abxpkg_cli(
        f"--lib={lib}",
        "run",
        "--install",
        "--binproviders=env",
        payload["CI_BREW_BIN"],
        "--prefix",
        env_overrides={
            **os.environ,
            "HOMEBREW_PREFIX": str(managed_prefix),
            "HOMEBREW_CELLAR": str(managed_prefix / "Cellar"),
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert Path(result.stdout.strip()) == host_prefix


def test_env_command_layers_dependency_config_defaults(tmp_path):
    lib = tmp_path / "lib"
    runtime_config = tmp_path / "runtime.json"
    plugin_config = tmp_path / "plugin.json"
    runtime_config.write_text(
        json.dumps(
            {
                "properties": {"PYTHON_BINARY": {"default": "python3"}},
                "required_binaries": [],
            },
        ),
    )
    plugin_config.write_text(
        json.dumps(
            {
                "required_binaries": [
                    {"name": "{PYTHON_BINARY}", "binproviders": "env"},
                ],
            },
        ),
    )

    proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "env",
        "--json",
        f"--deps-from={runtime_config}:required_binaries",
        f"--deps-from={plugin_config}:required_binaries",
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert Path(payload["PYTHON_BINARY"]).is_file()


def test_render_env_assignment_lines_uses_shell_safe_double_quotes():
    lines = cli_module.render_env_assignment_lines(
        base_env={},
        final_env={"TEST_ENV": 'a"b$c`d\\e'},
    )

    assert lines == ['TEST_ENV="a\\"b\\$c\\`d\\\\e"']


def test_render_env_assignment_lines_leaves_safe_values_unquoted():
    lines = cli_module.render_env_assignment_lines(
        base_env={},
        final_env={"TEST_ENV": "localhost,127.0.0.1:/tmp/bin"},
    )

    assert lines == ["TEST_ENV=localhost,127.0.0.1:/tmp/bin"]


def test_render_activate_lines_uses_fish_set_syntax():
    lines = cli_module.render_activate_lines(
        base_env={},
        final_env={"TEST_ENV": "/tmp/abx lib/bin"},
        shell="fish",
    )

    assert lines == ['set -x TEST_ENV "/tmp/abx lib/bin"']


def test_render_activate_comment_is_shell_specific():
    assert (
        cli_module.render_activate_comment(
            shell="bash",
            binary_names=("npm", "uv", "pip", "yt-dlp"),
        )
        == '# eval "$(abxpkg activate npm uv pip yt-dlp)"'
    )
    assert (
        cli_module.render_activate_comment(
            shell="zsh",
            binary_names=("npm", "uv", "pip", "yt-dlp"),
        )
        == '# eval "$(abxpkg activate --zsh npm uv pip yt-dlp)"'
    )
    assert (
        cli_module.render_activate_comment(
            shell="fish",
            binary_names=("npm", "uv", "pip", "yt-dlp"),
        )
        == "# abxpkg activate --fish npm uv pip yt-dlp | source"
    )


def test_parse_activate_shell_rejects_multiple_modes():
    with pytest.raises(click.BadParameter):
        cli_module.parse_activate_shell(bash=True, zsh=True, fish=False)


def test_build_command_exec_env_without_names_includes_installers_and_cached_binaries(
    tmp_path,
    test_machine,
):
    lib_dir = tmp_path / "abxlib"
    npm_binary = test_machine.require_tool("npm")
    test_machine.require_tool("node")
    old_lib_dir = os.environ.get("ABXPKG_LIB_DIR")
    old_npm_binary = os.environ.get("NPM_BINARY")
    old_path = os.environ.get("PATH", "")
    os.environ["ABXPKG_LIB_DIR"] = str(lib_dir)
    os.environ["NPM_BINARY"] = npm_binary
    os.environ["PATH"] = "/usr/bin:/bin"
    try:
        provider = PnpmProvider(
            install_root=lib_dir / "pnpm",
            postinstall_scripts=True,
            min_release_age=3,
        )
        installed = provider.install("zx")
        assert installed is not None
        assert installed.loaded_abspath is not None

        options = cli_module.CliOptions(
            lib_dir=lib_dir,
            provider_names=["pnpm"],
            dry_run=False,
            debug=False,
            no_cache=False,
        )
        final_env = cli_module.build_command_exec_env(
            (),
            options=options,
            base_env={},
        )
    finally:
        if old_lib_dir is None:
            os.environ.pop("ABXPKG_LIB_DIR", None)
        else:
            os.environ["ABXPKG_LIB_DIR"] = old_lib_dir
        if old_npm_binary is None:
            os.environ.pop("NPM_BINARY", None)
        else:
            os.environ["NPM_BINARY"] = old_npm_binary
        os.environ["PATH"] = old_path

    path_entries = final_env["PATH"].split(os.pathsep)
    assert str(lib_dir / "env" / "bin") in path_entries
    assert str(provider.bin_dir) in path_entries
    assert final_env["NODE_MODULES_DIR"] == str(lib_dir / "pnpm" / "node_modules")


def test_activate_command_can_be_evaled_for_installable_pip_binary(tmp_path):
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("ABXPKG_")
    }
    command = (
        f'eval "$({shlex.quote(str(_abxpkg_executable()))} '
        f"--lib={shlex.quote(str(tmp_path))} "
        '--binproviders=pip activate --install black)"; '
        "black --version"
    )
    proc = subprocess.run(
        ["bash", "-lc", command],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("black"), proc.stdout


def test_activate_command_emits_comment_and_fish_lines(tmp_path):
    proc = _run_abxpkg_cli(
        f"--lib={tmp_path / 'abx lib'}",
        "--binproviders=pip",
        "activate",
        "--fish",
        "--install",
        "black",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    stdout_lines = proc.stdout.strip().splitlines()
    assert stdout_lines[0] == "# abxpkg activate --fish black | source"
    assert any(line.startswith("set -x VIRTUAL_ENV ") for line in stdout_lines[1:])
    assert any(line.startswith("set -x PATH ") for line in stdout_lines[1:])


def test_activate_command_rejects_multiple_shell_modes():
    result = CliRunner().invoke(
        cli_module.cli,
        ["activate", "--bash", "--fish", "python3"],
    )

    assert result.exit_code != 0
    assert "choose only one of --bash, --zsh, or --fish" in click.unstyle(result.output)


def test_exec_command_hidden_alias_runs_like_run():
    proc = _run_abxpkg_cli(
        "--binproviders=env",
        "exec",
        "python3",
        "-c",
        "print('abx-exec-ok')",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "abx-exec-ok"


# ---------------------------------------------------------------------------
# `abx` — thin alias for `abxpkg run --install ...` (argv-rewriting wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected_pre", "expected_rest"),
    [
        (["yt-dlp", "--help"], [], ["yt-dlp", "--help"]),
        (["--update", "yt-dlp"], ["--update"], ["yt-dlp"]),
        (["--upgrade", "yt-dlp"], ["--upgrade"], ["yt-dlp"]),
        (
            ["--binproviders=env,uv,pip,apt,brew", "yt-dlp"],
            ["--binproviders=env,uv,pip,apt,brew"],
            ["yt-dlp"],
        ),
        (
            ["--lib", "/tmp/abx-lib", "--dry-run", "yt-dlp", "--help"],
            ["--lib", "/tmp/abx-lib", "--dry-run"],
            ["yt-dlp", "--help"],
        ),
        (
            ["--binproviders", "pip,brew", "black", "-v"],
            ["--binproviders", "pip,brew"],
            ["black", "-v"],
        ),
        (
            ["--install-args", '["black==24.2.0"]', "black", "--version"],
            ["--install-args", '["black==24.2.0"]'],
            ["black", "--version"],
        ),
        (["--version"], ["--version"], []),
        ([], [], []),
        # POSIX `--` option terminator: the `--` itself is consumed and
        # everything after it is treated as the binary name + its args,
        # regardless of whether the first token looks like an option.
        (["--", "yt-dlp", "--help"], [], ["yt-dlp", "--help"]),
        (
            ["--update", "--", "--weird-binary-name", "--help"],
            ["--update"],
            ["--weird-binary-name", "--help"],
        ),
        (
            ["--binproviders=env", "--", "python3", "--version"],
            ["--binproviders=env"],
            ["python3", "--version"],
        ),
        # `--` *after* the binary name is part of the binary's argv and
        # must be forwarded verbatim (not consumed by the splitter).
        (
            ["yt-dlp", "--", "-x"],
            [],
            ["yt-dlp", "--", "-x"],
        ),
    ],
)
def test_split_abx_argv_splits_options_from_binary(argv, expected_pre, expected_rest):
    pre, rest = cli_module._split_abx_argv(argv)
    assert pre == expected_pre
    assert rest == expected_rest


def test_abx_accepts_dash_dash_option_terminator_before_binary():
    """`abx --binproviders=env -- python3 --version` must still work."""

    proc = _run_abx_cli(
        "--binproviders=env",
        "--",
        "python3",
        "--version",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("Python "), proc.stdout


def test_abx_auto_installs_and_runs_preinstalled_env_binary():
    """`abx BIN` on an already-present binary resolves it and execs it."""

    proc = _run_abx_cli(
        "--binproviders=env",
        "python3",
        "-c",
        "print('abx-ok')",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "abx-ok"


def test_abx_passes_flag_args_through_to_underlying_binary():
    """Flags after the binary name must reach the binary, not abxpkg.

    Uses ``python3 --version`` because macOS ships BSD ``ls`` which does
    not recognise ``--help``.
    """

    proc = _run_abx_cli("--binproviders=env", "python3", "--version")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("Python "), proc.stdout
    assert proc.stderr == ""


def test_abx_debug_does_not_probe_later_providers_before_env_resolves():
    proc = _run_abx_cli(
        "--debug",
        "--binproviders=env,brew,apt",
        "python3",
        "--version",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("Python "), proc.stdout
    assert (
        "BinProvider.load(BrewProvider(name='brew'), bin_name='brew')"
        not in proc.stderr
    )
    assert (
        "BinProvider.load(AptProvider(name='apt'), bin_name='apt-get')"
        not in proc.stderr
    )


def test_abx_debug_env_provider_uses_derived_env_on_second_run(tmp_path):
    first = _run_abx_cli(
        "--debug",
        f"--lib={tmp_path}",
        "--binproviders=env",
        "python3",
        "--version",
    )
    derived_env = tmp_path / "env" / "derived.env"
    first_stat = derived_env.stat()
    second = _run_abx_cli(
        "--debug",
        f"--lib={tmp_path}",
        "--binproviders=env",
        "python3",
        "--version",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "EnvProvider.get_version('python3'" in first.stderr
    assert "EnvProvider.get_version('python3'" not in second.stderr
    assert "EnvProvider.get_sha256('python3'" in first.stderr
    assert "EnvProvider.get_sha256('python3'" not in second.stderr
    second_stat = derived_env.stat()
    assert second_stat.st_ino == first_stat.st_ino
    assert second_stat.st_mtime_ns == first_stat.st_mtime_ns


def test_list_command_reads_provider_local_derived_env(tmp_path):
    provider = EnvProvider(
        install_root=tmp_path / "env",
        postinstall_scripts=True,
        min_release_age=3,
    )
    loaded = provider.load("python3")

    assert loaded is not None
    assert loaded.loaded_version is not None
    assert loaded.loaded_abspath is not None
    assert provider.install_root is not None
    assert (provider.install_root / "derived.env").is_file()

    proc = _run_abxpkg_cli("list", f"--lib={tmp_path}", "--binproviders=env")

    assert proc.returncode == 0, proc.stderr
    expected_line = cli_module.format_loaded_binary_line(
        loaded.loaded_version,
        loaded.loaded_abspath,
        "env",
        "python3",
    )
    assert expected_line in proc.stdout.splitlines()
    assert proc.stderr == ""


def test_list_command_includes_installer_binaries_by_default(tmp_path):
    env_provider = EnvProvider(
        install_root=tmp_path / "env",
        postinstall_scripts=True,
        min_release_age=3,
    )
    loaded = env_provider.load("python3")

    uv_provider = cli_module.build_providers(
        ["uv"],
        dry_run=False,
        install_root=tmp_path / "uv",
    )[0]
    installer_binary = uv_provider.INSTALLER_BINARY()

    assert loaded is not None
    assert loaded.loaded_version is not None
    assert loaded.loaded_abspath is not None
    assert installer_binary is not None
    assert installer_binary.loaded_version is not None
    assert installer_binary.loaded_abspath is not None
    assert installer_binary.loaded_binprovider is not None

    proc = _run_abxpkg_cli("list", f"--lib={tmp_path}", "--binproviders=env,uv")

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert (
        cli_module.format_loaded_binary_line(
            loaded.loaded_version,
            loaded.loaded_abspath,
            "env",
            "python3",
        )
        in lines
    )
    assert (
        cli_module.format_loaded_binary_line(
            installer_binary.loaded_version,
            installer_binary.loaded_abspath,
            installer_binary.loaded_binprovider.name,
            "uv",
        )
        in lines
    )
    assert "" in lines
    assert lines.index("") == 1
    assert proc.stderr == ""


def test_version_report_includes_provider_local_cached_binary_list(tmp_path):
    provider = EnvProvider(
        install_root=tmp_path / "env",
        postinstall_scripts=True,
        min_release_age=3,
    )
    loaded = provider.load("python3")

    assert loaded is not None
    assert loaded.loaded_version is not None
    assert loaded.loaded_abspath is not None

    proc = _run_abxpkg_cli("version", f"--lib={tmp_path}", "--binproviders=env")

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    expected_line = cli_module.format_loaded_binary_line(
        loaded.loaded_version,
        loaded.loaded_abspath,
        "env",
        "python3",
    )
    assert "   installed_binaries=" in lines
    assert f"      {expected_line}" in lines
    assert proc.stderr == ""


def test_version_report_does_not_install_provider_dependencies(tmp_path):
    proc = _run_abxpkg_cli(
        "version",
        f"--lib={tmp_path}",
        "--binproviders=pnpm",
        env_overrides={"PATH": ""},
        timeout=15,
    )

    assert proc.returncode == 0, proc.stderr
    assert "PnpmProvider (pnpm)" in proc.stdout
    assert "INSTALLER_BINARY=None" in proc.stdout
    assert not (tmp_path / "pnpm").exists()
    assert proc.stderr == ""


def test_version_report_projects_existing_host_installer_through_env(
    tmp_path,
    test_machine,
):
    npm_abspath = test_machine.require_tool("npm")
    node_abspath = test_machine.require_tool("node")
    host_path = os.pathsep.join(
        dict.fromkeys((str(Path(npm_abspath).parent), str(Path(node_abspath).parent))),
    )

    proc = _run_abxpkg_cli(
        "version",
        f"--lib={tmp_path}",
        "--binproviders=npm",
        env_overrides={"PATH": host_path},
        timeout=15,
    )

    projected_npm = tmp_path / "env" / "bin" / "npm"
    assert proc.returncode == 0, proc.stderr
    assert projected_npm.is_symlink()
    assert projected_npm.resolve() == Path(npm_abspath).resolve()
    assert str(projected_npm) in proc.stdout
    assert "(npm) env" in proc.stdout
    assert not (tmp_path / "npm").exists()
    assert proc.stderr == ""


def test_list_command_filters_by_binary_name_and_provider_name(tmp_path):
    env_provider = EnvProvider(
        install_root=tmp_path / "env",
        postinstall_scripts=True,
        min_release_age=3,
    )
    loaded = env_provider.load("python3")

    uv_provider = cli_module.build_providers(
        ["uv"],
        dry_run=False,
        install_root=tmp_path / "uv",
    )[0]
    installer_binary = uv_provider.INSTALLER_BINARY()

    assert loaded is not None
    assert loaded.loaded_abspath is not None
    assert installer_binary is not None
    assert installer_binary.loaded_version is not None
    assert installer_binary.loaded_abspath is not None
    assert installer_binary.loaded_binprovider is not None

    proc = _run_abxpkg_cli(
        "list",
        "python3",
        "uv",
        f"--lib={tmp_path}",
        "--binproviders=env,uv",
    )

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert (
        cli_module.format_loaded_binary_line(
            loaded.loaded_version,
            loaded.loaded_abspath,
            "env",
            "python3",
        )
        in lines
    )
    assert (
        cli_module.format_loaded_binary_line(
            installer_binary.loaded_version,
            installer_binary.loaded_abspath,
            installer_binary.loaded_binprovider.name,
            "uv",
        )
        in lines
    )
    assert proc.stderr == ""


def test_abx_propagates_underlying_exit_code():
    proc = _run_abx_cli(
        "--binproviders=env",
        "python3",
        "-c",
        "import sys; sys.stderr.write('kaboom\\n'); sys.exit(5)",
    )

    assert proc.returncode == 5
    assert proc.stdout == ""
    assert "kaboom" in proc.stderr


def test_abx_respects_binproviders_flag_before_binary_name():
    """`abx --binproviders=LIST BIN ARGS` must forward LIST to abxpkg."""

    proc = _run_abx_cli(
        "--binproviders=env,uv,pip,apt,brew",
        "python3",
        "-c",
        "print('abx-binproviders-ok')",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "abx-binproviders-ok"


def test_abx_version_flag_is_forwarded_without_running_a_binary():
    proc = _run_abx_cli("--version")

    assert proc.returncode == 0, proc.stderr
    from abxpkg.cli import get_package_version

    assert proc.stdout.strip() == get_package_version()


def test_abxpkg_version_runs_without_error():
    proc = _run_abxpkg_cli(
        "--binproviders=env",
        "version",
        env_overrides={"ABXPKG_POSTINSTALL_SCRIPTS": "True"},
    )

    assert proc.returncode == 0, proc.stderr


def test_upgrade_command_is_hidden_from_help():
    result = CliRunner().invoke(cli_module.cli, ["--help"])

    assert result.exit_code == 0
    assert " add" not in result.output
    assert "│ exec" not in result.output
    assert " help" not in result.output
    assert " upgrade" not in result.output
    assert " remove" not in result.output


def test_abx_without_any_args_prints_usage_and_exits_two():
    proc = _run_abx_cli()

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "Usage: abx" in proc.stderr
    assert "run --install" in proc.stderr


def test_abx_installs_missing_binary_via_selected_provider(tmp_path):
    """Auto-install behaviour: `abx` installs into the isolated lib dir."""

    proc = _run_abx_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "black",
        "--version",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    # stdout must be *only* black --version output, not abxpkg's install logs.
    stdout_lines = proc.stdout.strip().splitlines()
    assert stdout_lines
    assert stdout_lines[0].startswith("black"), stdout_lines
    for line in stdout_lines:
        assert "Installing" not in line
        assert "Loading" not in line
    # Ensure black was actually installed under the isolated lib dir.
    installed = list((tmp_path / "pip").rglob("black"))
    assert installed, (
        f"Expected black to be installed under {tmp_path}/pip. "
        f"stderr was:\n{proc.stderr}"
    )


def test_abx_update_flag_is_forwarded_and_runs_after_update(tmp_path):
    """`abx --update BIN ARGS` must ensure the binary is available, then update it."""

    proc = _run_abx_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "--update",
        "black",
        "--version",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("black")
    installed = list((tmp_path / "pip").rglob("black"))
    assert installed


def test_abx_upgrade_flag_is_forwarded_and_runs_after_update(tmp_path):
    proc = _run_abx_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "--upgrade",
        "black",
        "--version",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("black")
    installed = list((tmp_path / "pip").rglob("black"))
    assert installed


# ---------------------------------------------------------------------------
# Full Binary/BinProvider option surface (--min-version, --postinstall-scripts,
# --min-release-age, --overrides, --install-root, --bin-dir, --euid,
# --install-timeout, --version-timeout) wired through shared_options.
# ---------------------------------------------------------------------------


def test_build_cli_options_passes_typed_values_through(tmp_path):
    """build_cli_options is called *after* click callbacks have parsed
    every raw string, so it only ever sees typed values — no parsing
    happens at this layer. Every field should land verbatim on CliOptions."""

    options = cli_module.build_cli_options(
        None,
        lib_dir=str(tmp_path),
        global_mode=None,
        binproviders="env,pip",
        dry_run=True,
        debug=False,
        no_cache=True,
        min_version="1.2.3",
        abspath_override="/tmp/custom-bin",
        version_override=["python3", "--version"],
        install_args_override=["black==24.2.0"],
        packages_override=["black==24.2.0"],
        postinstall_scripts=False,
        min_release_age=14.0,
        overrides={"pip": {"install_args": ["black==24.2.0"]}},
        install_root=tmp_path / "custom-root",
        bin_dir=tmp_path / "custom-bin",
        euid=1000,
        install_timeout=300,
        version_timeout=25,
    )

    assert options.lib_dir == tmp_path.resolve()
    assert options.provider_names == ["env", "pip"]
    assert options.dry_run is True
    assert options.debug is False
    assert options.no_cache is True
    assert options.min_version == "1.2.3"
    assert options.postinstall_scripts is False
    assert options.min_release_age == 14.0
    assert options.overrides == {
        "env": {
            "abspath": "/tmp/custom-bin",
            "version": ["python3", "--version"],
            "install_args": ["black==24.2.0"],
            "packages": ["black==24.2.0"],
        },
        "pip": {
            "abspath": "/tmp/custom-bin",
            "version": ["python3", "--version"],
            "install_args": ["black==24.2.0"],
            "packages": ["black==24.2.0"],
        },
    }
    assert options.install_root == tmp_path / "custom-root"
    assert options.bin_dir == tmp_path / "custom-bin"
    assert options.euid == 1000
    assert options.install_timeout == 300
    assert options.version_timeout == 25


def test_build_cli_options_nones_all_leave_fields_at_default(tmp_path):
    """Passing None for every typed value should leave CliOptions at its
    dataclass defaults (i.e. None, with dry_run resolving via env-var fallback)."""

    options = cli_module.build_cli_options(
        None,
        lib_dir=str(tmp_path),
        global_mode=None,
        binproviders="env",
        dry_run=None,
        debug=None,
        no_cache=None,
        min_version=None,
        abspath_override=None,
        version_override=None,
        install_args_override=None,
        packages_override=None,
        postinstall_scripts=None,
        min_release_age=None,
        overrides=None,
        install_root=None,
        bin_dir=None,
        euid=None,
        install_timeout=None,
        version_timeout=None,
    )

    assert options.debug is False
    assert options.no_cache is False
    assert options.min_version is None
    assert options.postinstall_scripts is None
    assert options.min_release_age is None
    assert options.overrides is None
    assert options.install_root is None
    assert options.bin_dir is None
    assert options.euid is None
    assert options.install_timeout is None
    assert options.version_timeout is None


def test_build_providers_passes_provider_level_flags_through(tmp_path):
    """Provider constructors should receive the configured knobs."""

    from abxpkg import PipProvider

    providers = cli_module.build_providers(
        ["pip", "env"],
        dry_run=True,
        install_root=tmp_path / "custom-root",
        bin_dir=tmp_path / "custom-bin",
        euid=1000,
        install_timeout=300,
        version_timeout=25,
    )

    pip_provider, env_provider = providers
    assert isinstance(pip_provider, PipProvider)
    assert pip_provider.dry_run is True
    assert pip_provider.euid == 1000
    assert pip_provider.install_timeout == 300
    assert pip_provider.version_timeout == 25
    assert pip_provider.install_root == (tmp_path / "custom-root").resolve()
    assert pip_provider.bin_dir == (tmp_path / "custom-bin").resolve()

    assert env_provider.dry_run is True
    assert env_provider.euid == 1000
    assert env_provider.install_timeout == 300
    assert env_provider.version_timeout == 25


def test_build_providers_constructs_every_builtin_provider(tmp_path):
    """Smoke-test: every builtin provider can be constructed with every CLI flag."""

    providers = cli_module.build_providers(
        list(cli_module.ALL_PROVIDER_NAMES),
        dry_run=True,
        install_root=tmp_path / "shared-root",
        bin_dir=tmp_path / "shared-bin",
        euid=1000,
        install_timeout=42,
        version_timeout=7,
    )
    assert len(providers) == len(cli_module.ALL_PROVIDER_NAMES)
    for provider in providers:
        assert provider.dry_run is True
        assert provider.euid == 1000
        assert provider.install_timeout == 42
        assert provider.version_timeout == 7
        assert provider.install_root == (tmp_path / "shared-root").resolve()
        assert provider.bin_dir == (tmp_path / "shared-bin").resolve()


def test_build_binary_forwards_binary_level_fields(tmp_path):
    """CliOptions.min_version / postinstall_scripts / min_release_age /
    overrides must land on the Binary instance."""

    options = cli_module.CliOptions(
        lib_dir=tmp_path,
        provider_names=["env", "pip"],
        dry_run=False,
        debug=False,
        no_cache=False,
        min_version="2.0.0",
        postinstall_scripts=False,
        min_release_age=30.0,
        overrides={"pip": {"install_args": ["custom==1.0"]}},
    )

    binary = cli_module.build_binary("black", options, dry_run=False)

    assert str(binary.min_version) == "2.0.0"
    assert binary.postinstall_scripts is False
    assert binary.min_release_age == 30.0
    assert binary.overrides == {"pip": {"install_args": ["custom==1.0"]}}


@pytest.mark.parametrize(
    "binary_name",
    [
        "brew",
        "bun",
        "cargo",
        "deno",
        "gem",
        "go",
        "playwright",
        "pnpm",
        "browsers",
        "uv",
    ],
)
def test_build_binary_uses_installer_provider_preferences_for_default_provider_set(
    tmp_path,
    binary_name,
):
    options = cli_module.CliOptions(
        lib_dir=tmp_path,
        provider_names=list(cli_module.DEFAULT_PROVIDER_NAMES),
        dry_run=False,
        debug=False,
        no_cache=False,
    )

    binary = cli_module.build_binary(binary_name, options, dry_run=False)
    provider_class = PROVIDER_CLASS_BY_INSTALLER_BIN[binary_name]
    assert provider_class.INSTALLER_BINPROVIDERS is not None
    expected_provider_names = [
        provider_name
        for provider_name in provider_class.INSTALLER_BINPROVIDERS
        if provider_name in cli_module.DEFAULT_PROVIDER_NAMES
    ]

    assert [
        provider.name for provider in binary.binproviders
    ] == expected_provider_names


@pytest.mark.parametrize(
    "provider_name",
    sorted(cli_module.PROVIDER_CLASS_BY_NAME),
)
def test_installer_provider_chains_are_host_first(provider_name):
    installer_providers = cli_module.PROVIDER_CLASS_BY_NAME[
        provider_name
    ].INSTALLER_BINPROVIDERS

    assert installer_providers
    assert installer_providers[0] == "env"
    if "apt" in installer_providers:
        apt_index = installer_providers.index("apt")
        for preferred_provider in ("node", "bash", "brew", "nix"):
            if preferred_provider in installer_providers:
                assert installer_providers.index(preferred_provider) < apt_index


@pytest.mark.parametrize("binary_name", ["go", "brew", "npm"])
def test_build_binary_loads_host_installer_with_owner_version_handler(
    tmp_path,
    binary_name,
):
    options = cli_module.CliOptions(
        lib_dir=tmp_path,
        provider_names=list(cli_module.DEFAULT_PROVIDER_NAMES),
        dry_run=False,
        debug=False,
        no_cache=True,
    )

    loaded = cli_module.build_binary(binary_name, options, dry_run=False).load(
        no_cache=True,
    )

    assert loaded.loaded_binprovider is not None
    assert loaded.loaded_binprovider.name == "env"
    assert loaded.loaded_abspath is not None
    assert loaded.loaded_version is not None


@pytest.mark.parametrize(
    ("binary_name", "expected_overrides"),
    [
        ("cargo", {"brew": {"install_args": ["rust"]}}),
        (
            "gem",
            {
                "apt": {"install_args": ["ruby"]},
                "brew": {"install_args": ["ruby"]},
            },
        ),
    ],
)
def test_build_binary_merges_provider_aliases_for_installer_binaries(
    tmp_path,
    binary_name,
    expected_overrides,
):
    options = cli_module.CliOptions(
        lib_dir=tmp_path,
        provider_names=list(cli_module.DEFAULT_PROVIDER_NAMES),
        dry_run=False,
        debug=False,
        no_cache=False,
    )

    binary = cli_module.build_binary(binary_name, options, dry_run=False)

    for provider_name, expected_override in expected_overrides.items():
        if provider_name not in cli_module.DEFAULT_PROVIDER_NAMES:
            continue
        assert binary.overrides[provider_name] == expected_override


def test_build_binary_preserves_explicit_provider_order_for_installer_binaries(
    tmp_path,
):
    options = cli_module.CliOptions(
        lib_dir=tmp_path,
        provider_names=["env", "brew", "cargo"],
        dry_run=False,
        debug=False,
        no_cache=False,
    )

    binary = cli_module.build_binary("cargo", options, dry_run=False)

    assert [provider.name for provider in binary.binproviders] == [
        "env",
        "brew",
        "cargo",
    ]


def test_build_cli_options_normalizes_override_flags_for_all_selected_providers(
    tmp_path,
):
    options = cli_module.build_cli_options(
        None,
        lib_dir=str(tmp_path),
        global_mode=None,
        binproviders="env,pip",
        dry_run=None,
        debug=None,
        no_cache=None,
        min_version=None,
        abspath_override=None,
        version_override=["python3", "--version"],
        install_args_override=["black==24.2.0"],
        packages_override=None,
        postinstall_scripts=None,
        min_release_age=None,
        overrides=None,
        install_root=None,
        bin_dir=None,
        euid=None,
        install_timeout=None,
        version_timeout=None,
    )

    binary = cli_module.build_binary("black", options, dry_run=False)

    assert binary.overrides == {
        "env": {
            "version": ["python3", "--version"],
            "install_args": ["black==24.2.0"],
        },
        "pip": {
            "version": ["python3", "--version"],
            "install_args": ["black==24.2.0"],
        },
    }


def test_build_cli_options_explicit_overrides_deepmerge_over_flag_defaults(tmp_path):
    options = cli_module.build_cli_options(
        None,
        lib_dir=str(tmp_path),
        global_mode=None,
        binproviders="env,pip",
        dry_run=None,
        debug=None,
        no_cache=None,
        min_version=None,
        abspath_override=None,
        version_override=["python3", "--version"],
        install_args_override=["black==24.2.0"],
        packages_override=None,
        postinstall_scripts=None,
        min_release_age=None,
        overrides={
            "pip": {
                "install_args": ["black==25.0.0"],
                "version_timeout": 99,
            },
        },
        install_root=None,
        bin_dir=None,
        euid=None,
        install_timeout=None,
        version_timeout=None,
    )

    binary = cli_module.build_binary("black", options, dry_run=False)

    assert binary.overrides == {
        "env": {
            "version": ["python3", "--version"],
            "install_args": ["black==24.2.0"],
        },
        "pip": {
            "version": ["python3", "--version"],
            "install_args": ["black==25.0.0"],
            "version_timeout": 99,
        },
    }


def test_upgrade_command_dispatches_to_update():
    result = CliRunner().invoke(
        cli_module.cli,
        ["upgrade", "--binproviders=env", "python"],
    )

    assert result.exit_code != 0
    assert "Unable to update binary python via providers env" in result.output


@pytest.mark.parametrize(
    ("argv", "lib_subdir"),
    [
        (
            [
                "--binproviders=pip",
                '--install-args=["black==25.0.0"]',
                '--overrides={"pip":{"install_args":["black==24.2.0"]}}',
                "--min-release-age=3",
                "upgrade",
                "black",
            ],
            "before-subcommand",
        ),
        (
            [
                "upgrade",
                "--binproviders=pip",
                '--install-args=["black==25.0.0"]',
                '--overrides={"pip":{"install_args":["black==24.2.0"]}}',
                "--min-release-age=3",
                "black",
            ],
            "after-subcommand",
        ),
    ],
)
def test_upgrade_command_accepts_binary_override_flags(tmp_path, argv, lib_subdir):
    """Binary override flags should work before or after the subcommand."""

    lib_dir = tmp_path / lib_subdir
    proc = _run_abxpkg_cli(
        f"--lib={lib_dir}",
        *argv,
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    assert "24.2.0" in proc.stdout
    assert list((lib_dir / "pip").rglob("black")), (
        f"Expected black under {lib_dir / 'pip'}, stderr was:\n{proc.stderr}"
    )


def test_add_command_dispatches_to_install():
    result = CliRunner().invoke(
        cli_module.cli,
        ["add", "--binproviders=env", "python"],
    )

    assert result.exit_code == 0
    assert "(env) python" in result.output


def test_remove_command_dispatches_to_uninstall():
    result = CliRunner().invoke(
        cli_module.cli,
        ["remove", "--binproviders=env", "python"],
    )

    assert result.exit_code != 0
    assert "Unable to uninstall binary python via providers env" in result.output


def test_help_command_matches_root_help_output():
    help_result = CliRunner().invoke(cli_module.cli, ["--help"])
    alias_result = CliRunner().invoke(cli_module.cli, ["help"])

    assert help_result.exit_code == 0
    assert alias_result.exit_code == 0
    assert click.unstyle(alias_result.output) == click.unstyle(help_result.output)


def test_install_postinstall_scripts_false_warns_on_unsupporting_providers(tmp_path):
    """Providers that can't enforce postinstall_scripts=False must emit a
    warning to stderr and continue (no hard-fail).
    """

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        "--postinstall-scripts=False",
        "--min-release-age=3",
        "--dry-run=True",
        "install",
        "python3",
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert (
        "EnvProvider.install ignoring unsupported postinstall_scripts=False"
        in proc.stderr
    ), proc.stderr


def test_install_min_version_too_high_fails_loudly(tmp_path):
    """--min-version should gate Binary.is_valid after install."""

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "--min-version=9999.0.0",
        "--min-release-age=3",
        "install",
        "black",
        timeout=900,
    )

    assert proc.returncode != 0
    assert "9999" in proc.stderr or "does not satisfy" in proc.stderr


def test_install_with_install_root_override_installs_there(tmp_path):
    """--install-root should pin pip_venv to the override directory."""

    custom_root = tmp_path / "custom-pip-root"
    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        f"--install-root={custom_root}",
        "--min-release-age=3",
        "install",
        "black",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    assert list(custom_root.rglob("black")), (
        f"Expected black under {custom_root}, stderr was:\n{proc.stderr}"
    )
    # And nothing under the lib_dir default location.
    assert not list((tmp_path / "pip").rglob("black"))


def test_install_with_overrides_json_uses_custom_install_args(tmp_path):
    """--overrides should thread through to Binary.overrides verbatim."""

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        '--overrides={"pip":{"install_args":["black==24.2.0"]}}',
        "--min-release-age=3",
        "install",
        "black",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    # The pinned version should win over pip's default resolution.
    assert "24.2.0" in proc.stdout


def test_parse_overrides_rejects_invalid_json():
    with pytest.raises(click.BadParameter):
        cli_module._parse_overrides("not-json")


def test_parse_overrides_rejects_non_dict_json():
    with pytest.raises(click.BadParameter):
        cli_module._parse_overrides("[1, 2, 3]")


def test_parse_cli_bool_rejects_garbage():
    with pytest.raises(click.BadParameter):
        cli_module._parse_cli_bool("maybe")


def test_parse_cli_float_rejects_garbage():
    with pytest.raises(click.BadParameter):
        cli_module._parse_cli_float("not-a-number")


def test_parse_cli_int_accepts_int_and_exact_float_strings():
    assert cli_module._parse_cli_int("10") == 10
    assert cli_module._parse_cli_int("10.0") == 10
    assert cli_module._parse_cli_int("None") is None
    assert cli_module._parse_cli_int("null") is None
    assert cli_module._parse_cli_int(None) is None


def test_parse_cli_int_rejects_non_integer_floats_and_garbage():
    with pytest.raises(click.BadParameter):
        cli_module._parse_cli_int("3.5")
    with pytest.raises(click.BadParameter):
        cli_module._parse_cli_int("abc")


# ---------------------------------------------------------------------------
# Bare bool flag expansion: `--dry-run` → `--dry-run=True`, same for
# `--postinstall-scripts`. Value forms are left alone so click parses them
# as a string value.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["--binproviders=env", "--dry-run", "install", "python3"],
            ["--binproviders=env", "--dry-run=True", "install", "python3"],
        ),
        (
            ["--dry-run=False", "install", "python3"],
            ["--dry-run=False", "install", "python3"],
        ),
        (
            ["--dry-run=None", "install", "python3"],
            ["--dry-run=None", "install", "python3"],
        ),
        (
            ["--postinstall-scripts", "install", "python3"],
            ["--postinstall-scripts=True", "install", "python3"],
        ),
        (
            ["--postinstall-scripts=False", "install", "python3"],
            ["--postinstall-scripts=False", "install", "python3"],
        ),
        (
            ["--no-cache", "install", "python3"],
            ["--no-cache=True", "install", "python3"],
        ),
        (
            ["--dry-run", "--postinstall-scripts", "--no-cache", "install", "python3"],
            [
                "--dry-run=True",
                "--postinstall-scripts=True",
                "--no-cache=True",
                "install",
                "python3",
            ],
        ),
    ],
)
def test_expand_bare_bool_flags_rewrites_bare_forms_in_place(argv, expected):
    assert cli_module._expand_bare_bool_flags(argv) == expected


# ---------------------------------------------------------------------------
# Real-live coverage of every supported flag via `install` (short-running).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra_flag",),
    [
        ("--min-version=0.0.0",),
        ("--min-version=None",),
        ("--postinstall-scripts=True",),
        ("--postinstall-scripts=False",),
        ("--postinstall-scripts=1",),
        ("--postinstall-scripts=0",),
        ("--postinstall-scripts=None",),
        ("--min-release-age=3",),
        ("--min-release-age=3.5",),
        ("--min-release-age=None",),
        ("--install-timeout=60",),
        ("--install-timeout=60.0",),
        ("--install-timeout=None",),
        ("--version-timeout=10",),
        ("--version-timeout=10.0",),
        ("--version-timeout=None",),
        ("--euid=None",),
        ("--overrides=None",),
        ('--overrides={"env":{}}',),
        ("--bin-dir=None",),
        ("--install-root=None",),
        ("--dry-run=True",),
        ("--dry-run=False",),
        ("--dry-run=None",),
        ("--no-cache=True",),
        ("--no-cache=False",),
        ("--no-cache=None",),
    ],
)
def test_install_command_accepts_every_supported_flag_form(extra_flag, tmp_path):
    """Live smoke-test: every flag form resolves python3 via env without raising."""

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        extra_flag,
        "install",
        "python3",
    )

    assert proc.returncode == 0, (
        f"--lib={tmp_path} --binproviders=env {extra_flag} install python3 "
        f"failed with exit {proc.returncode}\nstderr:\n{proc.stderr}"
    )


@pytest.mark.parametrize(
    "subcommand",
    ["install", "load"],
)
def test_every_subcommand_accepts_the_full_option_surface(subcommand, tmp_path):
    """Every subcommand honours every option by reusing shared_options."""

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        "--min-version=0.0.0",
        "--postinstall-scripts=False",
        "--min-release-age=3",
        "--no-cache=False",
        "--install-timeout=60",
        "--version-timeout=10",
        "--dry-run=False",
        subcommand,
        "python3",
    )

    assert proc.returncode == 0, proc.stderr
    assert "python3" in proc.stdout


def test_update_subcommand_accepts_the_full_option_surface(tmp_path):
    """`update` must still parse every option even when the provider cannot update."""

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        "--min-version=0.0.0",
        "--postinstall-scripts=False",
        "--min-release-age=3",
        "--install-timeout=60",
        "--version-timeout=10",
        "--dry-run=False",
        "update",
        "python3",
    )

    assert proc.returncode != 0
    assert "Unable to update binary python3 via providers env" in proc.stderr


def test_subcommand_level_option_overrides_group_level():
    """A subcommand-level flag should override the group-level flag field-by-field."""

    proc = _run_abxpkg_cli(
        "--binproviders=apt",  # group-level: would match nothing useful
        "install",
        "--binproviders=env",  # subcommand-level: wins
        "python3",
    )

    assert proc.returncode == 0, proc.stderr
    assert "env" in proc.stdout
    assert "python3" in proc.stdout


# ---------------------------------------------------------------------------
# Real-live coverage of every supported flag via `run` (uses group_options).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    [
        "--min-version=0.0.0",
        "--postinstall-scripts=False",
        "--min-release-age=3",
        "--global",
        "--install-timeout=60",
        "--version-timeout=10",
        '--overrides={"env":{}}',
        "--install-root=None",
        "--bin-dir=None",
        "--euid=None",
    ],
)
def test_run_command_honours_group_level_options(flag, tmp_path):
    """`run` reads its options off the group-level CliOptions, so every
    abxpkg group flag must survive the round-trip through build_binary."""

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        flag,
        "run",
        "python3",
        "-c",
        "print('run-ok')",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "run-ok"


# ---------------------------------------------------------------------------
# Real-live coverage: `abx` forwards every option to abxpkg unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    [
        "--min-version=0.0.0",
        "--postinstall-scripts=True",
        "--postinstall-scripts=False",
        "--min-release-age=3",
        "--global",
        "--install-timeout=60",
        "--version-timeout=10",
        '--overrides={"env":{}}',
        "--install-root=None",
        "--bin-dir=None",
        "--euid=None",
        "--dry-run=False",
    ],
)
def test_abx_forwards_every_option_to_abxpkg(flag, tmp_path):
    proc = _run_abx_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        flag,
        "python3",
        "-c",
        "print('abx-ok')",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "abx-ok"


def test_abx_dry_run_value_form_is_forwarded_to_abxpkg(tmp_path):
    """`abx --dry-run=True BIN ...` must propagate as dry_run=True."""

    proc = _run_abx_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        "--dry-run=True",
        "python3",
        "-c",
        "print('should-not-print')",
    )

    # Dry-run short-circuits without execing the binary.
    assert proc.returncode == 0, proc.stderr
    assert "should-not-print" not in proc.stdout


# ---------------------------------------------------------------------------
# parse_script_metadata unit tests
# ---------------------------------------------------------------------------


class TestParseScriptMetadata:
    """Unit tests for ``parse_script_metadata``."""

    def test_hash_comment_prefix(self, tmp_path):
        script = tmp_path / "test.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "# /// script\n"
            '# dependencies = ["requests"]\n'
            "# ///\n"
            'print("hello")\n',
        )
        meta = cli_module.parse_script_metadata(script)
        assert meta is not None
        assert meta["dependencies"] == ["requests"]

    def test_double_slash_comment_prefix(self, tmp_path):
        script = tmp_path / "test.js"
        script.write_text(
            "#!/usr/bin/env node\n"
            "// /// script\n"
            '// dependencies = ["node"]\n'
            "// ///\n"
            'console.log("hello");\n',
        )
        meta = cli_module.parse_script_metadata(script)
        assert meta is not None
        assert meta["dependencies"] == ["node"]

    def test_dash_dash_comment_prefix(self, tmp_path):
        script = tmp_path / "test.lua"
        script.write_text(
            '-- /// script\n-- dependencies = ["lua"]\n-- ///\n',
        )
        meta = cli_module.parse_script_metadata(script)
        assert meta is not None
        assert meta["dependencies"] == ["lua"]

    def test_semicolon_comment_prefix(self, tmp_path):
        script = tmp_path / "test.el"
        script.write_text(
            '; /// script\n; dependencies = ["emacs"]\n; ///\n',
        )
        meta = cli_module.parse_script_metadata(script)
        assert meta is not None
        assert meta["dependencies"] == ["emacs"]

    def test_no_metadata_returns_none(self, tmp_path):
        script = tmp_path / "plain.py"
        script.write_text('print("no metadata here")\n')
        assert cli_module.parse_script_metadata(script) is None

    def test_unclosed_block_returns_none(self, tmp_path):
        script = tmp_path / "bad.py"
        script.write_text(
            '# /// script\n# dependencies = ["x"]\n# no closing marker\n',
        )
        assert cli_module.parse_script_metadata(script) is None

    def test_indentation_preserved(self, tmp_path):
        script = tmp_path / "nested.py"
        script.write_text(
            "# /// script\n# [tool.abxpkg]\n# postinstall_scripts = true\n# ///\n",
        )
        meta = cli_module.parse_script_metadata(script)
        assert meta is not None
        assert meta["tool"]["abxpkg"]["postinstall_scripts"] is True

    def test_tool_section(self, tmp_path):
        script = tmp_path / "tool.py"
        script.write_text(
            "# /// script\n"
            '# dependencies = ["python3"]\n'
            "# [tool.abxpkg]\n"
            "# ABXPKG_MIN_RELEASE_AGE = 14\n"
            "# ABXPKG_POSTINSTALL_SCRIPTS = true\n"
            "# ///\n",
        )
        meta = cli_module.parse_script_metadata(script)
        assert meta is not None
        assert meta["tool"]["abxpkg"]["ABXPKG_MIN_RELEASE_AGE"] == 14
        assert meta["tool"]["abxpkg"]["ABXPKG_POSTINSTALL_SCRIPTS"] is True

    def test_dict_dependencies(self, tmp_path):
        script = tmp_path / "deps.py"
        script.write_text(
            "# /// script\n"
            "# [[dependencies]]\n"
            '# name = "node"\n'
            '# binproviders = ["env", "apt"]\n'
            '# min_version = "22.0.0"\n'
            "# ///\n",
        )
        meta = cli_module.parse_script_metadata(script)
        assert meta is not None
        assert meta["dependencies"][0]["name"] == "node"
        assert meta["dependencies"][0]["binproviders"] == ["env", "apt"]

    def test_max_lines_limit(self, tmp_path):
        script = tmp_path / "late.py"
        # Put the metadata beyond max_lines=5
        lines = ["# line\n"] * 10 + [
            "# /// script\n",
            '# dependencies = ["x"]\n',
            "# ///\n",
        ]
        script.write_text("".join(lines))
        assert cli_module.parse_script_metadata(script, max_lines=5) is None
        # But it works with a higher limit
        meta = cli_module.parse_script_metadata(script, max_lines=15)
        assert meta is not None

    def test_blank_lines_in_block(self, tmp_path):
        script = tmp_path / "blanks.py"
        script.write_text(
            '# /// script\n#\n# dependencies = ["x"]\n#\n# ///\n',
        )
        meta = cli_module.parse_script_metadata(script)
        assert meta is not None
        assert meta["dependencies"] == ["x"]


# ---------------------------------------------------------------------------
# --script integration tests
# ---------------------------------------------------------------------------


def test_run_script_with_interpreter_on_cli(tmp_path):
    """abxpkg run --script python3 <script> should parse metadata and run."""

    script = tmp_path / "hello.py"
    script.write_text(
        '# /// script\n# dependencies = ["python3"]\n# ///\nprint("script-ok")\n',
    )

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path / 'lib'}",
        "--binproviders=env",
        "run",
        "--script",
        "--install",
        "python3",
        str(script),
    )
    assert proc.returncode == 0, proc.stderr
    assert "script-ok" in proc.stdout


def test_run_script_passes_args_to_script(tmp_path):
    """Arguments after the script path are forwarded to the script."""

    script = tmp_path / "args.py"
    script.write_text(
        "# /// script\n"
        '# dependencies = ["python3"]\n'
        "# ///\n"
        "import sys\n"
        'print(" ".join(sys.argv[1:]))\n',
    )

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path / 'lib'}",
        "--binproviders=env",
        "run",
        "--script",
        "--install",
        "python3",
        str(script),
        "arg1",
        "arg2",
    )
    assert proc.returncode == 0, proc.stderr
    assert "arg1 arg2" in proc.stdout


def test_run_script_no_metadata_exits_with_error(tmp_path):
    """--script with no /// metadata should exit 1."""

    script = tmp_path / "plain.py"
    script.write_text('print("no metadata")\n')

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path / 'lib'}",
        "--binproviders=env",
        "run",
        "--script",
        "--install",
        "python3",
        str(script),
    )
    assert proc.returncode != 0
    assert "no /// script metadata" in proc.stderr


def test_run_script_missing_script_path_exits_with_error(tmp_path):
    """--script with no script path arg should exit 1."""

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path / 'lib'}",
        "--binproviders=env",
        "run",
        "--script",
        "--install",
        "python3",
    )
    assert proc.returncode != 0
    assert "--script requires a script path" in proc.stderr


def test_run_script_nonexistent_file_exits_with_error(tmp_path):
    """--script pointing at a nonexistent file should exit 1."""

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path / 'lib'}",
        "--binproviders=env",
        "run",
        "--script",
        "--install",
        "python3",
        str(tmp_path / "does_not_exist.py"),
    )
    assert proc.returncode != 0
    assert "script not found" in proc.stderr


def test_run_script_cli_interpreter_overrides_metadata(tmp_path):
    """The CLI binary name (python3) is used even if metadata names a different dep."""

    script = tmp_path / "override.py"
    script.write_text(
        '# /// script\n# dependencies = ["python3"]\n# ///\nprint("override-ok")\n',
    )

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path / 'lib'}",
        "--binproviders=env",
        "run",
        "--script",
        "--install",
        "python3",
        str(script),
    )
    assert proc.returncode == 0, proc.stderr
    assert "override-ok" in proc.stdout


def test_run_script_propagates_exit_code(tmp_path):
    """The exit code from the script should propagate through."""

    script = tmp_path / "exitcode.py"
    script.write_text(
        '# /// script\n# dependencies = ["python3"]\n# ///\nimport sys\nsys.exit(42)\n',
    )

    proc = _run_abxpkg_cli(
        f"--lib={tmp_path / 'lib'}",
        "--binproviders=env",
        "run",
        "--script",
        "--install",
        "python3",
        str(script),
    )
    assert proc.returncode == 42


def test_run_script_dependency_provider_path_is_available_inside_script(tmp_path):
    """Dependency provider PATH should be merged into the script runtime env."""

    lib = tmp_path / "lib"
    script = tmp_path / "black_check.py"
    script.write_text(
        "# /// script\n"
        "# [[dependencies]]\n"
        '# name = "black"\n'
        '# binproviders = ["pip"]\n'
        "# ///\n"
        "import subprocess\n"
        "import sys\n"
        "proc = subprocess.run(['black', '--version'], capture_output=True, text=True)\n"
        "sys.stdout.write((proc.stdout or proc.stderr).strip())\n"
        "sys.exit(proc.returncode)\n",
    )

    proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=env,pip",
        "--postinstall-scripts=True",
        "--min-release-age=3",
        "run",
        "--script",
        "--install",
        "python3",
        str(script),
    )

    assert proc.returncode == 0, proc.stderr
    assert "black" in proc.stdout.lower()


def test_run_script_applies_install_args_to_side_dependency(tmp_path):
    """Shebang metadata should configure non-interpreter dependencies fully."""

    lib = tmp_path / "lib"
    install_root = tmp_path / "black-pip-root"
    script = tmp_path / "black_pinned.py"
    script.write_text(
        "# /// script\n"
        "# [[dependencies]]\n"
        '# name = "black"\n'
        '# binproviders = ["pip"]\n'
        '# install_args = ["black==24.2.0"]\n'
        f'# install_root = "{install_root}"\n'
        "# ///\n"
        "from abxpkg import Binary\n"
        "import sys\n"
        "black = Binary(name='black').load(no_cache=True)\n"
        "assert black.loaded_abspath is not None\n"
        "print(f'black_path={black.loaded_abspath}')\n"
        "proc = black.exec(cmd=('--version',), quiet=True)\n"
        "sys.stdout.write(proc.stdout or proc.stderr)\n"
        "sys.exit(proc.returncode)\n",
    )

    proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=env,pip",
        "--postinstall-scripts=True",
        "--min-release-age=3",
        "run",
        "--script",
        "--install",
        "python3",
        str(script),
        timeout=240,
    )

    assert proc.returncode == 0, proc.stderr
    projected_black = lib / "env" / "bin" / "black"
    installed_black = install_root / "venv" / "bin" / "black"
    assert f"black_path={projected_black}" in proc.stdout
    assert projected_black.is_symlink()
    assert projected_black.samefile(installed_black)
    assert "24.2.0" in proc.stdout


def test_run_env_linked_python3_executes_active_venv_target(tmp_path):
    """EnvProvider-linked python3 should run with active venv semantics intact."""

    lib = tmp_path / "lib"

    load_proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=env",
        "load",
        "python3",
    )
    assert load_proc.returncode == 0, load_proc.stderr
    linked_python = lib / "env" / "bin" / "python3"
    assert linked_python.is_symlink()
    assert linked_python.samefile(sys.executable)

    proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=env",
        "run",
        "python3",
        "-c",
        (
            "import abxpkg, json, sys; "
            "print(json.dumps({"
            "'abxpkg_file': abxpkg.__file__, "
            "'executable': sys.executable, "
            "'prefix': sys.prefix"
            "}))"
        ),
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert (
        Path(payload["abxpkg_file"]).resolve()
        == Path(cli_module.__file__).parents[0] / "__init__.py"
    )
    assert Path(payload["executable"]).samefile(sys.executable)
    assert Path(payload["prefix"]).resolve() == Path(sys.prefix).resolve()


def test_concurrent_script_runs_reuse_host_python_before_managed_fallback(tmp_path):
    """Cold parallel hooks must not race past EnvProvider into PipProvider."""
    lib = tmp_path / "lib"
    for provider_name in ("bash", "brew", "chromewebstore", "docker", "pip"):
        (lib / provider_name).mkdir(parents=True)
    deps_config = tmp_path / "config.json"
    deps_config.write_text(json.dumps({"required_binaries": []}))
    script = tmp_path / "host_python.py"
    script.write_text(
        "# /// script\n"
        '# requires-python = ">=3.12"\n'
        "# ///\n"
        "import json, os, sys\n"
        'print(json.dumps({"executable": sys.executable, '
        '"providers": os.environ.get("ABXPKG_BINPROVIDERS")}))\n',
    )

    def run_script(_index: int) -> subprocess.CompletedProcess[str]:
        return _run_abxpkg_cli(
            f"--lib={lib}",
            "run",
            "--script",
            f"--deps-from={deps_config}:required_binaries",
            "python3",
            str(script),
            timeout=30,
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(run_script, range(24)))

    for result in results:
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout.strip())
        assert Path(payload["executable"]).resolve() == Path(sys.executable).resolve()
        assert payload["providers"] is None
    managed_provider_dirs = [
        path for path in lib.iterdir() if path.name not in {"env", "bin"}
    ]
    assert all(not any(path.iterdir()) for path in managed_provider_dirs)


def test_env_dependency_does_not_expand_derived_defaults_into_installer_fallbacks(
    tmp_path,
):
    """A dependency's pnpm provider must not probe unrelated default providers."""
    lib = tmp_path / "lib"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "required_binaries": [
                    {
                        "name": "playwright",
                        "binproviders": "pnpm",
                        "overrides": {
                            "pnpm": {
                                "install_root": str(
                                    lib / "pnpm" / "packages" / "playwright",
                                ),
                                "install_args": ["playwright@next"],
                            },
                        },
                    },
                ],
            },
        ),
    )

    result = _run_abxpkg_cli(
        f"--lib={lib}",
        "env",
        "--json",
        f"--deps-from={config}:required_binaries",
        "node",
        timeout=15,
    )

    assert result.returncode != 0
    assert "Unable to load binary playwright via providers pnpm" in result.stderr
    provider_dirs = {path.name for path in lib.iterdir()} if lib.exists() else set()
    assert not {"bash", "brew", "docker", "pip"}.intersection(provider_dirs)


def test_run_script_dependency_uses_explicit_host_abspath(tmp_path):
    """Absolute dependency names must resolve through env before any fallback."""
    lib = tmp_path / "lib"
    host_python = tmp_path / "host-python"
    host_python.symlink_to(Path(sys.executable).absolute())
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "required_binaries": [
                    {
                        "name": str(host_python),
                        "binproviders": "env",
                    },
                ],
            },
        ),
    )
    script = tmp_path / "show_runtime.py"
    script.write_text(
        "# /// script\n"
        "# ///\n"
        "import json, sys\n"
        "print(json.dumps({'executable': sys.executable}))\n",
    )

    result = _run_abxpkg_cli(
        f"--lib={lib}",
        "run",
        "--script",
        f"--deps-from={config}:required_binaries",
        "python3",
        str(script),
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert Path(payload["executable"]).samefile(sys.executable)
    linked_host = lib / "env" / "bin" / host_python.name
    assert linked_host.is_symlink()
    assert linked_host.readlink() == host_python.absolute()

    env_lib = tmp_path / "env-lib"
    env_result = _run_abxpkg_cli(
        f"--lib={env_lib}",
        "--binproviders=env",
        "env",
        "--install",
        "--json",
        f"--deps-from={config}:required_binaries",
        timeout=15,
    )
    assert env_result.returncode == 0, env_result.stderr
    env_linked_host = env_lib / "env" / "bin" / host_python.name
    assert env_linked_host.is_symlink()
    assert env_linked_host.readlink() == host_python.absolute()


def test_run_with_apt_fallback_is_instant_on_non_linux(tmp_path):
    """Considering apt as a fallback provider must not resolve/install apt off Linux."""

    script = tmp_path / "apt_fallback.py"
    script.write_text(
        "# /// script\n"
        '# dependencies = ["python3"]\n'
        "# ///\n"
        "import json, sys\n"
        "print(json.dumps({'executable': sys.executable}))\n",
    )

    started_at = time.perf_counter()
    proc = _run_abxpkg_cli(
        f"--lib={tmp_path / 'lib'}",
        "--binproviders=env,apt",
        "run",
        "--script",
        "python3",
        str(script),
    )
    elapsed = time.perf_counter() - started_at

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert Path(payload["executable"]).samefile(sys.executable)
    if sys.platform != "linux":
        assert elapsed < 1


def test_load_cache_context_includes_binary_overrides(tmp_path):
    lib = tmp_path / "lib"
    first = _abxpkg_executable()
    second = Path(sys.executable)

    def loaded_path(proc: subprocess.CompletedProcess[str]) -> Path:
        displayed_path = Path(proc.stdout.split()[1]).expanduser()
        return displayed_path.resolve()

    first_proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=env",
        "load",
        "--abspath",
        str(first),
        "--version",
        "1.0.0",
        "demo",
    )
    assert first_proc.returncode == 0, first_proc.stderr
    assert "1.0.0" in first_proc.stdout
    assert loaded_path(first_proc).samefile(first)
    flag_cache_keys = set(load_derived_cache(lib / "env" / "derived.env"))

    first_json_proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=env",
        f'--overrides={{"env":{{"abspath":"{first}","version":"1.0.0"}}}}',
        "load",
        "demo",
    )
    assert first_json_proc.returncode == 0, first_json_proc.stderr
    assert "1.0.0" in first_json_proc.stdout
    assert loaded_path(first_json_proc).samefile(first)
    assert set(load_derived_cache(lib / "env" / "derived.env")) == flag_cache_keys

    second_proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=env",
        f'--overrides={{"env":{{"abspath":"{second}","version":"2.0.0"}}}}',
        "load",
        "demo",
    )
    assert second_proc.returncode == 0, second_proc.stderr
    assert "2.0.0" in second_proc.stdout
    assert loaded_path(second_proc).samefile(second)
    second_cache_keys = set(load_derived_cache(lib / "env" / "derived.env"))
    assert second_cache_keys != flag_cache_keys

    first_again_proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=env",
        f'--overrides={{"env":{{"abspath":"{first}","version":"1.0.0"}}}}',
        "load",
        "demo",
    )
    assert first_again_proc.returncode == 0, first_again_proc.stderr
    assert "1.0.0" in first_again_proc.stdout
    assert loaded_path(first_again_proc).samefile(first)


def test_run_script_without_declared_dependency_keeps_target_runtime_env_clean(
    tmp_path,
):
    """Selected fallback providers must not leak undeclared Python packages into scripts."""

    lib = tmp_path / "lib"

    install_proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=uv",
        "--postinstall-scripts=False",
        "--min-release-age=3",
        '--overrides={"uv":{"install_args":["imagesize>=2.0.0"]}}',
        "install",
        "imagesize",
    )
    assert install_proc.returncode == 0, install_proc.stderr

    script = tmp_path / "import_imagesize.py"
    script.write_text(
        "#!/usr/bin/env -S abxpkg run --script python3\n"
        "# /// script\n"
        '# requires-python = ">=3.12"\n'
        "# ///\n"
        "import json, os, sys\n"
        "print(json.dumps({\n"
        "    'executable': sys.executable,\n"
        "    'prefix': sys.prefix,\n"
        "    'path': os.environ.get('PATH', ''),\n"
        "    'pythonpath': os.environ.get('PYTHONPATH', ''),\n"
        "    'virtual_env': os.environ.get('VIRTUAL_ENV', ''),\n"
        "}))\n",
    )
    script.chmod(0o755)

    proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=env,uv",
        "run",
        "--script",
        "python3",
        str(script),
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert Path(payload["executable"]).samefile(sys.executable)
    assert Path(payload["prefix"]).resolve() == Path(sys.prefix).resolve()
    assert str(lib / "uv" / "venv") not in payload["virtual_env"]
    assert str(lib / "uv" / "venv") not in payload["pythonpath"]
    assert str(lib / "uv" / "venv" / "bin") not in payload["path"].split(os.pathsep)


def test_run_script_honors_lib_dir_env_and_uv_provider_cache(tmp_path):
    """Script execution should use caller ABXPKG_LIB_DIR provider env."""

    lib = tmp_path / "lib"

    install_proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=uv",
        "--postinstall-scripts=False",
        "--min-release-age=3",
        '--overrides={"uv":{"install_args":["imagesize>=2.0.0"]}}',
        "install",
        "imagesize",
    )
    assert install_proc.returncode == 0, install_proc.stderr

    script = tmp_path / "import_imagesize.py"
    script.write_text(
        "#!/usr/bin/env -S abxpkg run --script python3\n"
        "# /// script\n"
        '# requires-python = ">=3.12"\n'
        '# dependencies = [{name = "imagesize", binproviders = "uv", install_args = ["imagesize>=2.0.0"], postinstall_scripts = false, min_release_age = 3}]\n'
        "# ///\n"
        "import imagesize, json, os, sys\n"
        "print(json.dumps({\n"
        "    'lib_dir': os.environ.get('ABXPKG_LIB_DIR'),\n"
        "    'abxpkg_lib_dir': os.environ.get('ABXPKG_LIB_DIR'),\n"
        "    'path': os.environ.get('PATH', ''),\n"
        "    'virtual_env': os.environ.get('VIRTUAL_ENV'),\n"
        "    'uv_cache_dir': os.environ.get('UV_CACHE_DIR'),\n"
        "    'runtime_python': f'{sys.version_info.major}.{sys.version_info.minor}',\n"
        "    'imagesize_file': imagesize.__file__,\n"
        "}))\n",
    )
    script.chmod(0o755)

    proc = _run_abxpkg_cli(
        "run",
        "--script",
        "python3",
        str(script),
        env_overrides={
            "ABXPKG_LIB_DIR": str(lib),
            "ABXPKG_BINPROVIDERS": "env,uv",
            "ACTIVE_PY_ENV": str(Path(sys.executable).parent.parent),
            "VIRTUAL_ENV": str(lib / "uv" / "packages" / "hook-runtime" / "venv"),
        },
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert Path(payload["lib_dir"]) == lib
    assert Path(payload["abxpkg_lib_dir"]) == lib.resolve()
    assert Path(payload["virtual_env"]) == (lib / "uv" / "venv")
    assert Path(payload["uv_cache_dir"]) == lib.resolve() / "cache" / "uv"
    assert str(lib.resolve() / "bin") not in payload["path"].split(os.pathsep)
    assert (
        str(lib / "uv" / "packages" / "imagesize" / "venv") in payload["imagesize_file"]
    )
    assert f"python{payload['runtime_python']}" in payload["imagesize_file"]


def test_run_script_keeps_active_runtime_imports_with_uv_provider_cache(
    tmp_path,
):
    """Managed uv script execution must not hide packages from the caller runtime."""

    lib = tmp_path / "lib"
    hook_runtime = lib / "uv" / "packages" / "hook-runtime"

    install_proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=uv",
        "--postinstall-scripts=False",
        "--min-release-age=3",
        '--overrides={"uv":{"install_args":["imagesize>=2.0.0"]}}',
        "install",
        "imagesize",
    )
    assert install_proc.returncode == 0, install_proc.stderr
    package_install_proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=uv",
        "--postinstall-scripts=False",
        "--min-release-age=3",
        f'--overrides={{"uv":{{"install_root":"{hook_runtime}","install_args":["humanize>=4.0.0"]}}}}',
        "install",
        "humanize",
    )
    assert package_install_proc.returncode == 0, package_install_proc.stderr

    script = tmp_path / "import_runtime_and_uv_packages.py"
    script.write_text(
        "#!/usr/bin/env -S abxpkg run --script python3\n"
        "# /// script\n"
        '# requires-python = ">=3.12"\n'
        "# dependencies = [\n"
        '#   {name = "imagesize", binproviders = "uv", install_args = ["imagesize>=2.0.0"], postinstall_scripts = false, min_release_age = 3},\n'
        f'#   {{name = "humanize", binproviders = "uv", install_root = "{hook_runtime}", install_args = ["humanize>=4.0.0"], postinstall_scripts = false, min_release_age = 3}},\n'
        "# ]\n"
        "# ///\n"
        "import abxpkg, humanize, imagesize, json, os, rich_click, sys\n"
        "print(json.dumps({\n"
        "    'abxpkg_file': abxpkg.__file__,\n"
        "    'executable': sys.executable,\n"
        "    'humanize_file': humanize.__file__,\n"
        "    'imagesize_file': imagesize.__file__,\n"
        "    'prefix': sys.prefix,\n"
        "    'rich_click_file': rich_click.__file__,\n"
        "}))\n",
    )
    script.chmod(0o755)

    proc = _run_abxpkg_cli(
        "run",
        "--script",
        "python3",
        str(script),
        env_overrides={
            "ABXPKG_LIB_DIR": str(lib),
            "ABXPKG_BINPROVIDERS": "env,uv",
        },
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert (
        Path(payload["abxpkg_file"]).resolve()
        == Path(cli_module.__file__).parents[0] / "__init__.py"
    )
    assert Path(payload["executable"]).samefile(sys.executable)
    assert Path(payload["prefix"]).resolve() == Path(sys.prefix).resolve()
    assert (
        str(lib / "uv" / "packages" / "imagesize" / "venv") in payload["imagesize_file"]
    )
    assert str(hook_runtime / "venv") in payload["humanize_file"]
    assert Path(payload["rich_click_file"]).resolve() == Path(click.__file__).resolve()


def test_run_script_uses_default_lib_dir_without_env_override(tmp_path):
    config_home = tmp_path / "xdg-config"
    default_lib = config_home / "abx" / "lib"

    install_proc = _run_abxpkg_cli(
        "--binproviders=uv",
        "--postinstall-scripts=False",
        "--min-release-age=3",
        f'--overrides={{"uv":{{"install_root":"{default_lib / "uv" / "packages" / "hook-runtime"}","install_args":["humanize>=4.0.0"]}}}}',
        "install",
        "humanize",
        env_overrides={
            "XDG_CONFIG_HOME": str(config_home),
        },
    )
    assert install_proc.returncode == 0, install_proc.stderr

    script = tmp_path / "import_default_uv_package.py"
    script.write_text(
        "#!/usr/bin/env -S abxpkg run --script python3\n"
        "# /// script\n"
        '# requires-python = ">=3.12"\n'
        "# ///\n"
        "import json, os, sys\n"
        "print(json.dumps({\n"
        "    'abxpkg_lib_dir': os.environ.get('ABXPKG_LIB_DIR'),\n"
        "    'executable': sys.executable,\n"
        "    'pythonpath': os.environ.get('PYTHONPATH', ''),\n"
        "    'virtual_env': os.environ.get('VIRTUAL_ENV', ''),\n"
        "}))\n",
    )
    script.chmod(0o755)

    proc = _run_abxpkg_cli(
        "run",
        "--script",
        "python3",
        str(script),
        env_overrides={
            "XDG_CONFIG_HOME": str(config_home),
        },
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert Path(payload["abxpkg_lib_dir"]) == default_lib
    assert Path(payload["executable"]).samefile(sys.executable)
    assert str(default_lib / "uv") not in payload["pythonpath"]
    assert str(default_lib / "uv") not in payload["virtual_env"]


def test_run_script_deps_from_uses_real_node_python_and_puppeteer(tmp_path):
    lib = tmp_path / "lib"
    install_root = lib / "pnpm" / "packages" / "hook-deps"
    node_modules_dir = install_root / "node_modules"
    shared_config = tmp_path / "shared_config.json"
    shared_config.write_text(
        json.dumps(
            {
                "properties": {
                    "NODE_BINARY": {"default": "node"},
                    "PYTHON_BINARY": {"default": "python3"},
                    "PUPPETEER_PACKAGE_ROOT": {"default": "hook-deps"},
                },
                "required_binaries": [
                    {
                        "name": "{NODE_BINARY}",
                        "binproviders": "env,npm,apt,brew",
                        "min_version": "22.12.0",
                        "overrides": {
                            "npm": {
                                "install_root": "{ABXPKG_LIB_DIR}/npm/packages/node",
                                "install_args": ["node@22.23.1"],
                                "postinstall_scripts": True,
                            },
                            "apt": {
                                "install_args": ["nodejs", "npm"],
                            },
                            "brew": {
                                "install_args": ["node"],
                            },
                        },
                    },
                    {
                        "name": "{PYTHON_BINARY}",
                        "binproviders": "env",
                        "min_version": "3.10.0",
                    },
                ],
            },
            indent=2,
        ),
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "required_binaries": [
                    {
                        "name": "puppeteer",
                        "binproviders": "pnpm",
                        "postinstall_scripts": False,
                        "min_release_age": 3,
                        "overrides": {
                            "pnpm": {
                                "install_root": "{ABXPKG_LIB_DIR}/pnpm/packages/{PUPPETEER_PACKAGE_ROOT}",
                                "install_args": ["puppeteer"],
                            },
                        },
                    },
                ],
            },
            indent=2,
        ),
    )

    script = tmp_path / "hook.js"
    script.write_text(
        "#!/usr/bin/env -S abxpkg run --script --deps-from=./shared_config.json:required_binaries,./config.json:required_binaries node\n"
        "\n"
        "// /// script\n"
        "// ///\n"
        "\n"
        "const startedAtMs = Date.now();\n"
        "const childProcess = require('child_process');\n"
        "const fs = require('fs');\n"
        "const path = require('path');\n"
        "\n"
        "const python = childProcess.execFileSync(process.env.PYTHON_BINARY, [\n"
        "  '-c',\n"
        '  \'import json, sys; print(json.dumps({"executable": sys.executable, "version": list(sys.version_info[:3])}))\',\n'
        "], {encoding: 'utf8'}).trim();\n"
        "const puppeteerPackage = require.resolve('puppeteer/package.json');\n"
        "const payload = {\n"
        "  startedAtMs,\n"
        "  nodeVersion: process.versions.node,\n"
        "  execPath: process.execPath,\n"
        "  nodeBinary: process.env.NODE_BINARY,\n"
        "  pythonBinary: process.env.PYTHON_BINARY,\n"
        "  python: JSON.parse(python),\n"
        "  puppeteerPackage,\n"
        "  puppeteerName: JSON.parse(fs.readFileSync(puppeteerPackage, 'utf8')).name,\n"
        "  puppeteerVersion: JSON.parse(fs.readFileSync(puppeteerPackage, 'utf8')).version,\n"
        "  nodePath: process.env.NODE_PATH,\n"
        "  nodeModulesDir: process.env.NODE_MODULES_DIR,\n"
        "  pnpmHome: process.env.PNPM_HOME,\n"
        "  path: process.env.PATH,\n"
        "};\n"
        "console.log(JSON.stringify(payload));\n",
    )
    script.chmod(0o755)

    script_env = {
        "ABXPKG_LIB_DIR": str(lib),
        "NODE_MODULES_DIR": str(tmp_path / "stale" / "node_modules"),
        "NODE_MODULE_DIR": str(tmp_path / "stale" / "node_modules"),
        # abx-dl starts hooks with the resolved provider environments already
        # projected. These additions must not invalidate the same exact cached
        # binaries when the shebang asks abxpkg to assemble its exec env.
        "PATH": os.pathsep.join(
            (str(node_modules_dir / ".bin"), os.environ.get("PATH", "")),
        ),
        "PYTHONPATH": str(tmp_path / "active-runtime"),
    }

    async def resolve_hook_dependencies() -> None:
        import abxbus

        from abxpkg.binary_service import BinaryRequestEvent, BinaryService

        bus = abxbus.EventBus(name="first_node_script_from_binary_cache")
        BinaryService(bus, auto_install=True, lib_dir=lib)
        requests = (
            BinaryRequestEvent(
                name="node",
                binproviders="env,npm,apt,brew",
                min_version="22.12.0",
                overrides={
                    "npm": {
                        "install_root": str(lib / "npm" / "packages" / "node"),
                        "install_args": ["node@22.23.1"],
                        "postinstall_scripts": True,
                    },
                    "apt": {"install_args": ["nodejs", "npm"]},
                    "brew": {"install_args": ["node"]},
                },
            ),
            BinaryRequestEvent(
                name="python3",
                binproviders="env",
                min_version="3.10.0",
                overrides={},
            ),
            BinaryRequestEvent(
                name="puppeteer",
                binproviders="pnpm",
                postinstall_scripts=False,
                min_release_age=3,
                overrides={
                    "pnpm": {
                        "install_root": str(install_root),
                        "install_args": ["puppeteer"],
                    },
                },
            ),
        )
        for request in requests:
            await bus.emit(request).now()
        await bus.wait_until_idle()

    import asyncio

    asyncio.run(resolve_hook_dependencies())
    proc = _run_cli(
        script,
        env_overrides={**script_env, "PYTHONPROFILEIMPORTTIME": "1"},
        timeout=900,
    )
    first_hook_started_at = time.perf_counter()
    first_hook_started_wall = time.time()
    timed_proc = _run_cli(script, env_overrides=script_env, timeout=60)
    first_hook_elapsed = time.perf_counter() - first_hook_started_at
    warm_proc = _run_cli(script, env_overrides=script_env, timeout=60)

    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert timed_proc.returncode == 0, timed_proc.stderr
    assert warm_proc.returncode == 0, warm_proc.stderr
    assert "rich_click" not in proc.stderr
    assert "pydantic" not in proc.stderr
    payload = json.loads(timed_proc.stdout.strip().splitlines()[-1])
    first_hook_launch_elapsed = payload["startedAtMs"] / 1000 - first_hook_started_wall
    direct_env = {
        key: value for key, value in os.environ.items() if not key.startswith("ABXPKG_")
    }
    direct_env.update(
        {
            "NODE_BINARY": payload["nodeBinary"],
            "PYTHON_BINARY": payload["pythonBinary"],
            "NODE_PATH": payload["nodePath"],
            "NODE_MODULES_DIR": payload["nodeModulesDir"],
            "NODE_MODULE_DIR": payload["nodeModulesDir"],
            "PNPM_HOME": payload["pnpmHome"],
            "PATH": payload["path"],
        },
    )
    direct_started_wall = time.time()
    direct_proc = subprocess.run(
        [payload["execPath"], str(script)],
        capture_output=True,
        check=False,
        text=True,
        env=direct_env,
        timeout=60,
    )
    assert direct_proc.returncode == 0, direct_proc.stderr
    direct_payload = json.loads(direct_proc.stdout.strip().splitlines()[-1])
    direct_launch_elapsed = direct_payload["startedAtMs"] / 1000 - direct_started_wall
    assert first_hook_launch_elapsed - direct_launch_elapsed < 0.1
    assert first_hook_elapsed < 1.0
    warm_payload = json.loads(warm_proc.stdout.strip().splitlines()[-1])
    payload.pop("startedAtMs")
    warm_payload.pop("startedAtMs")
    assert warm_payload == payload
    assert int(payload["nodeVersion"].split(".", 1)[0]) >= 22
    assert payload["python"]["version"][:2] >= [3, 10]
    assert Path(payload["nodeBinary"]).is_file()
    assert Path(payload["pythonBinary"]).is_file()
    assert Path(payload["nodeModulesDir"]) == node_modules_dir
    assert Path(payload["pnpmHome"]) == node_modules_dir / ".bin"
    puppeteer_package = Path(payload["puppeteerPackage"])
    assert puppeteer_package.is_file()
    assert puppeteer_package.relative_to(node_modules_dir)
    assert payload["puppeteerName"] == "puppeteer"
    assert payload["puppeteerVersion"]
    assert str(lib / "bin") not in payload["path"].split(os.pathsep)
    assert str(lib / "env" / "bin") in payload["path"].split(os.pathsep)
    assert str(node_modules_dir / ".bin") in payload["path"].split(os.pathsep)
    assert (node_modules_dir / "puppeteer" / "package.json").is_file()

    alternate_npm_dir = tmp_path / "alternate-npm"
    alternate_npm_dir.mkdir()
    alternate_npm = alternate_npm_dir / "npm"
    alternate_npm.symlink_to(
        shutil.which("npm") or shutil.which("env") or "/usr/bin/env",
    )
    changed_provider_env = _run_cli(
        script,
        env_overrides={
            **script_env,
            "NPM_BINARY": str(alternate_npm),
            "PYTHONPROFILEIMPORTTIME": "1",
        },
        timeout=60,
    )

    assert changed_provider_env.returncode == 0, changed_provider_env.stderr
    assert "rich_click" in changed_provider_env.stderr


@pytest.fixture()
def abx_e2e_lib():
    """Provide a lib dir with playwright + chromium pre-installed.

    Uses a shared cache at ``/tmp/abx-e2e-lib`` so the ~370 MB browser
    download only happens once.

    Install order matters: npm playwright first (provides the CLI),
    then playwright provider installs the chromium browser.
    """

    lib = Path("/tmp/abx-e2e-lib")
    npm_prefix = lib / "npm"
    playwright_root = lib / "playwright"

    # 1. install playwright npm package (provides the CLI + require('playwright'))
    if not (npm_prefix / "node_modules" / "playwright").is_dir():
        proc = _run_abxpkg_cli(
            f"--lib={lib}",
            "--binproviders=npm",
            "--postinstall-scripts=True",
            "--min-release-age=3",
            "install",
            "playwright",
            timeout=900,
        )
        assert proc.returncode == 0, (
            f"failed to install playwright:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )

    # 2. install chromium via the playwright binprovider
    chromium_installed = (playwright_root / "bin" / "chromium").exists()
    if not chromium_installed:
        proc = _run_abxpkg_cli(
            f"--lib={lib}",
            "--binproviders=playwright",
            "--postinstall-scripts=True",
            "--min-release-age=3",
            "--install-timeout=600",
            "install",
            "chromium",
            timeout=900,
        )
        assert proc.returncode == 0, (
            f"failed to install chromium:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
        assert (playwright_root / "bin" / "chromium").exists(), (
            "chromium symlink not found after install"
        )

    return lib


def test_run_script_node_playwright_chromium_end_to_end(abx_e2e_lib, tmp_path):
    """Full end-to-end: resolve node, playwright (npm), chromium (playwright),
    launch a browser with explicit executablePath, and verify everything came
    from abxpkg's lib dir — not system binaries."""

    script = tmp_path / "e2e.js"
    script.write_text(
        "#!/usr/bin/env -S abxpkg run --script node\n"
        "\n"
        "// /// script\n"
        "// dependencies = [\n"
        '//     {name = "node", binproviders = ["env", "npm", "apt", "brew"], min_version = "22.12.0"},\n'
        '//     {name = "playwright", binproviders = ["npm", "pnpm"], install_args = ["playwright@next"]},\n'
        '//     {name = "chromium", binproviders = ["playwright", "puppeteer", "apt"], min_version = "131.0.0"},\n'
        "// ]\n"
        "// [tool.abxpkg]\n"
        "// ABXPKG_POSTINSTALL_SCRIPTS = true\n"
        "// ///\n"
        "\n"
        "const path = require('path');\n"
        "const { chromium } = require('playwright');\n"
        "const { execSync } = require('child_process');\n"
        "const fs = require('fs');\n"
        "\n"
        "const errors = [];\n"
        "\n"
        "// 1. node >= 22\n"
        "const nodeMajor = parseInt(process.versions.node.split('.')[0], 10);\n"
        "if (nodeMajor < 22) errors.push('node major ' + nodeMajor + ' < 22');\n"
        "\n"
        "// 2. playwright loaded from node_modules inside lib dir\n"
        "const pwPath = require.resolve('playwright');\n"
        "if (!pwPath.includes('node_modules'))\n"
        "    errors.push('playwright not from node_modules: ' + pwPath);\n"
        "\n"
        "// 3. find chromium on PATH (provided by abxpkg, not system)\n"
        "const chromiumPath = execSync('which chromium', {encoding: 'utf-8'}).trim();\n"
        "if (!chromiumPath || chromiumPath.startsWith('/usr/bin') || chromiumPath.startsWith('/usr/local/bin'))\n"
        "    errors.push('chromium looks like system binary: ' + chromiumPath);\n"
        "const chromiumReal = fs.realpathSync(chromiumPath);\n"
        "if (!chromiumReal.includes('/playwright/'))\n"
        "    errors.push('chromium does not resolve into ABXPKG_LIB_DIR/playwright: ' + chromiumPath + ' -> ' + chromiumReal);\n"
        "\n"
        "// 4. chromium version >= 131\n"
        "try {\n"
        '    const ver = execSync(`"${chromiumPath}" --version`, {encoding: "utf-8"}).trim();\n'
        "    const m = ver.match(/(\\d+)\\.\\d+\\.\\d+/);\n"
        "    if (!m || parseInt(m[1], 10) < 131)\n"
        "        errors.push('chromium version too low: ' + ver);\n"
        "} catch(e) { errors.push('chromium --version failed: ' + e.message); }\n"
        "\n"
        "// 5. launch browser with the chromium binary from PATH\n"
        "(async () => {\n"
        "    const browser = await chromium.launch({headless: true, executablePath: chromiumPath});\n"
        "    const page = await browser.newPage();\n"
        "    await page.setContent('<html><head><title>Test</title></head>'\n"
        "        + '<body><h1>Hello</h1><p>abxpkg e2e</p></body></html>');\n"
        "    const title = await page.title();\n"
        "    if (title !== 'Test') errors.push('title was: ' + title);\n"
        "    const h1 = await page.textContent('h1');\n"
        "    if (h1 !== 'Hello') errors.push('h1 was: ' + h1);\n"
        "    await browser.close();\n"
        "\n"
        "    if (errors.length) {\n"
        "        errors.forEach(e => console.error(e));\n"
        "        process.exit(1);\n"
        "    }\n"
        "    console.log('e2e-ok');\n"
        "})();\n",
    )

    proc = _run_abxpkg_cli(
        f"--lib={abx_e2e_lib}",
        "--binproviders=env,npm,playwright",
        "--postinstall-scripts=True",
        "--min-release-age=3",
        "--install-timeout=600",
        "--install",
        "run",
        "--script",
        "node",
        str(script),
        timeout=900,
    )

    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert proc.stdout.strip().endswith("e2e-ok"), (
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )

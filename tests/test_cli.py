from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import logging
from pathlib import Path

import pytest
import rich_click as click
from click.testing import CliRunner

from abxpkg import BinName, BinProviderName, EnvProvider, SemVer
import abxpkg.cli as cli_module


def _abxpkg_executable() -> Path:
    """Locate the installed abxpkg console script for subprocess-based tests."""

    candidate = Path(sys.executable).parent / "abxpkg"
    if candidate.exists():
        return candidate
    resolved = shutil.which("abxpkg")
    assert resolved, "abxpkg console script must be installed in the active venv"
    return Path(resolved)


def _abx_executable() -> Path:
    """Locate the installed `abx` console script for subprocess-based tests."""

    candidate = Path(sys.executable).parent / "abx"
    if candidate.exists():
        return candidate
    resolved = shutil.which("abx")
    assert resolved, "abx console script must be installed in the active venv"
    return Path(resolved)


def _run_cli(
    script: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 600,
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
        text=True,
        env=env,
        timeout=timeout,
    )


def _run_abxpkg_cli(
    *args: str,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real `abxpkg` console script with a clean env."""

    return _run_cli(
        _abxpkg_executable(),
        *args,
        env_overrides=env_overrides,
        timeout=timeout,
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


def test_build_providers_uses_managed_lib_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("ABXPKG_LIB_DIR", str(tmp_path))
    providers = cli_module.build_providers(
        ["uv", "pip", "pnpm", "cargo", "env"],
        dry_run=True,
    )

    assert providers[0].install_root == tmp_path / "uv"
    assert providers[1].install_root == tmp_path / "pip"
    assert providers[2].install_root == tmp_path / "pnpm"
    assert providers[3].install_root == tmp_path / "cargo"
    assert providers[4].name == "env"
    assert all(provider.dry_run for provider in providers)


def test_parse_provider_names_uses_preferred_default_cli_order(monkeypatch):
    monkeypatch.delenv("ABXPKG_BINPROVIDERS", raising=False)

    assert cli_module.parse_provider_names(None) == list(
        cli_module.DEFAULT_PROVIDER_NAMES,
    )


def test_default_cli_sets_managed_lib_dir(monkeypatch):
    monkeypatch.delenv("ABXPKG_LIB_DIR", raising=False)
    captured = {}

    def fake_run_binary_command(binary_name, *, action, options):
        captured["binary_name"] = binary_name
        captured["action"] = action
        captured["options"] = options
        captured["env_lib_dir"] = os.environ.get("ABXPKG_LIB_DIR")
        captured["install_root"] = cli_module.build_providers(
            ["pip"],
            dry_run=True,
        )[0].install_root

    monkeypatch.setattr(cli_module, "run_binary_command", fake_run_binary_command)

    result = CliRunner().invoke(
        cli_module.cli,
        ["load", "python"],
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "python"
    assert captured["action"] == "load"
    assert captured["options"].lib_dir == cli_module.DEFAULT_LIB_DIR.resolve()
    assert captured["env_lib_dir"] == str(cli_module.DEFAULT_LIB_DIR.resolve())
    assert captured["install_root"] == cli_module.DEFAULT_LIB_DIR.resolve() / "pip"


def test_cli_lib_none_disables_managed_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("ABXPKG_LIB_DIR", str(tmp_path))
    captured = {}

    def fake_run_binary_command(binary_name, *, action, options):
        captured["binary_name"] = binary_name
        captured["action"] = action
        captured["options"] = options
        captured["env_lib_dir"] = os.environ.get("ABXPKG_LIB_DIR")
        captured["install_root"] = cli_module.build_providers(
            ["pip"],
            dry_run=True,
        )[0].install_root

    monkeypatch.setattr(cli_module, "run_binary_command", fake_run_binary_command)

    result = CliRunner().invoke(
        cli_module.cli,
        ["--lib=None", "load", "python"],
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "python"
    assert captured["action"] == "load"
    assert captured["options"].lib_dir == cli_module.DEFAULT_LIB_DIR.resolve()
    assert captured["env_lib_dir"] is None
    assert captured["install_root"] is None


def test_cli_global_flag_disables_managed_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("ABXPKG_LIB_DIR", str(tmp_path))
    captured = {}

    def fake_run_binary_command(binary_name, *, action, options):
        captured["binary_name"] = binary_name
        captured["action"] = action
        captured["options"] = options
        captured["env_lib_dir"] = os.environ.get("ABXPKG_LIB_DIR")
        captured["install_root"] = cli_module.build_providers(
            ["pip"],
            dry_run=True,
        )[0].install_root

    monkeypatch.setattr(cli_module, "run_binary_command", fake_run_binary_command)

    result = CliRunner().invoke(
        cli_module.cli,
        ["--global", "load", "python"],
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "python"
    assert captured["action"] == "load"
    assert captured["options"].lib_dir == cli_module.DEFAULT_LIB_DIR.resolve()
    assert captured["env_lib_dir"] is None
    assert captured["install_root"] is None


def test_env_lib_none_disables_managed_mode(monkeypatch):
    monkeypatch.setenv("ABXPKG_LIB_DIR", "None")

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

    assert options.lib_dir == cli_module.DEFAULT_LIB_DIR.resolve()
    assert os.environ.get("ABXPKG_LIB_DIR") is None
    assert cli_module.build_providers(["pip"], dry_run=True)[0].install_root is None


def test_install_command_uses_env_defaults(monkeypatch, tmp_path):
    captured = {}

    def fake_run_binary_command(binary_name, *, action, options):
        captured["binary_name"] = binary_name
        captured["action"] = action
        captured["options"] = options

    monkeypatch.setattr(cli_module, "run_binary_command", fake_run_binary_command)

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
    assert captured["binary_name"] == "prettier"
    assert captured["action"] == "install"
    assert captured["options"].lib_dir == tmp_path.resolve()
    assert captured["options"].provider_names == ["pnpm", "uv"]
    assert captured["options"].dry_run is True
    assert captured["options"].debug is False
    assert captured["options"].no_cache is False


def test_build_cli_options_exports_resolved_provider_names(monkeypatch):
    monkeypatch.delenv("ABXPKG_BINPROVIDERS", raising=False)

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


def test_install_command_uses_debug_env_default(monkeypatch, tmp_path):
    captured = {}

    def fake_run_binary_command(binary_name, *, action, options):
        captured["binary_name"] = binary_name
        captured["action"] = action
        captured["options"] = options

    monkeypatch.setattr(cli_module, "run_binary_command", fake_run_binary_command)

    result = CliRunner().invoke(
        cli_module.cli,
        ["install", "prettier"],
        env={
            "ABXPKG_LIB_DIR": str(tmp_path),
            "ABXPKG_DEBUG": "1",
        },
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "prettier"
    assert captured["action"] == "install"
    assert captured["options"].debug is True


def test_install_command_uses_debug_flag(monkeypatch, tmp_path):
    captured = {}

    def fake_run_binary_command(binary_name, *, action, options):
        captured["binary_name"] = binary_name
        captured["action"] = action
        captured["options"] = options

    monkeypatch.setattr(cli_module, "run_binary_command", fake_run_binary_command)

    result = CliRunner().invoke(
        cli_module.cli,
        ["--debug=True", "install", "prettier"],
        env={"ABXPKG_LIB_DIR": str(tmp_path)},
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "prettier"
    assert captured["action"] == "install"
    assert captured["options"].debug is True


def test_install_command_uses_no_cache_env_default(monkeypatch, tmp_path):
    captured = {}

    def fake_run_binary_command(binary_name, *, action, options):
        captured["binary_name"] = binary_name
        captured["action"] = action
        captured["options"] = options

    monkeypatch.setattr(cli_module, "run_binary_command", fake_run_binary_command)

    result = CliRunner().invoke(
        cli_module.cli,
        ["install", "prettier"],
        env={
            "ABXPKG_LIB_DIR": str(tmp_path),
            "ABXPKG_NO_CACHE": "1",
        },
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "prettier"
    assert captured["action"] == "install"
    assert captured["options"].no_cache is True


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


def test_version_command_with_binary_aliases_load(monkeypatch, tmp_path):
    captured = {}

    def fake_run_binary_command(binary_name, *, action, options):
        captured["binary_name"] = binary_name
        captured["action"] = action
        captured["options"] = options

    monkeypatch.setattr(cli_module, "run_binary_command", fake_run_binary_command)

    result = CliRunner().invoke(
        cli_module.cli,
        ["version", f"--lib={tmp_path}", "--binproviders=env", "python3"],
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "python3"
    assert captured["action"] == "load"
    assert captured["options"].dry_run is False


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


def test_run_update_skips_env_for_the_update_step(monkeypatch, tmp_path):
    calls: list[tuple[str, object]] = []

    class FakeLoadedProvider:
        def __init__(self, name: str):
            self.name = name

        def exec(self, bin_name, cmd=(), capture_output=False):
            calls.append(
                ("exec", (self.name, str(bin_name), tuple(cmd), capture_output)),
            )
            return subprocess.CompletedProcess(
                [str(bin_name), *cmd],
                0,
                "",
                "",
            )

    class FakeRunBinary:
        def __init__(self):
            self.loaded_abspath = Path("/tmp/fake-bin")
            self.loaded_version = SemVer("1.2.3")
            self.loaded_binprovider = FakeLoadedProvider("env")
            self.binproviders = []
            self.is_valid = True

        def load(self, no_cache=None):
            calls.append(("load", (no_cache,)))
            return self

        def install(self, dry_run=None, no_cache=None):
            calls.append(("install", (dry_run, no_cache)))
            return self

        def update(self, binproviders=None, dry_run=None, no_cache=None):
            calls.append(("update", (tuple(binproviders or ()), dry_run, no_cache)))
            self.loaded_binprovider = FakeLoadedProvider("brew")
            return self

    monkeypatch.setattr(
        cli_module,
        "build_binary",
        lambda *args, **kwargs: FakeRunBinary(),
    )

    result = CliRunner().invoke(
        cli_module.cli,
        [
            f"--lib={tmp_path}",
            "--binproviders=env,brew",
            "run",
            "--update",
            "python3",
            "--version",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        ("load", (False,)),
        ("update", (("brew",), False, False)),
        ("exec", ("brew", "/tmp/fake-bin", ("--version",), False)),
    ]


def test_run_stdout_stderr_are_separated_and_not_buffered(tmp_path):
    """stdout and stderr from the underlying binary must stream separately."""

    # Drop a tiny shim script into a fresh PATH directory that the env
    # provider will pick up. The script must respond to --version so
    # EnvProvider can .load() it, then return a non-zero exit code with
    # output split across stdout/stderr.
    script = tmp_path / "abxpkg-run-shim"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        '  echo "abxpkg-run-shim 1.2.3"\n'
        "  exit 0\n"
        "fi\n"
        "echo 'this goes to stdout'\n"
        "echo 'this goes to stderr' >&2\n"
        "exit 7\n",
    )
    script.chmod(0o755)

    # Use an ad-hoc PATH that exposes the custom script as a "binary".
    proc = _run_abxpkg_cli(
        "--binproviders=env",
        "run",
        script.name,
        env_overrides={"PATH": f"{tmp_path}:{os.environ['PATH']}"},
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
    monkeypatch,
    tmp_path,
):
    class ExtraEnvProvider(EnvProvider):
        name: BinProviderName = "extra_env"
        INSTALLER_BIN: BinName = "python3"

        @property
        def ENV(self) -> dict[str, str]:
            return {"EXTRA_ENV": str(tmp_path / "extra")}

    class InstallerEnvProvider(EnvProvider):
        name: BinProviderName = "installer_env"
        INSTALLER_BIN: BinName = "python3"

        @property
        def ENV(self) -> dict[str, str]:
            return {"INSTALLER_ENV": str(tmp_path / "installer")}

    class CacheOwnerProvider(EnvProvider):
        name: BinProviderName = "cache_owner"
        INSTALLER_BIN: BinName = "python3"

    installer_provider = InstallerEnvProvider(
        install_root=tmp_path / "installer-provider",
        postinstall_scripts=True,
        min_release_age=0,
    )
    installed_provider = ExtraEnvProvider(
        install_root=tmp_path / "installed-provider",
        postinstall_scripts=True,
        min_release_age=0,
    )
    cache_owner = CacheOwnerProvider(
        install_root=tmp_path / "cache-owner",
        postinstall_scripts=True,
        min_release_age=0,
    )

    installer_binary = installer_provider.load("python3")
    installed_binary = installed_provider.load("python3")
    assert installer_binary is not None
    assert installer_binary.loaded_binprovider is not None
    assert installed_binary is not None
    assert installed_binary.loaded_binprovider is not None

    monkeypatch.setattr(
        CacheOwnerProvider,
        "INSTALLER_BINARY",
        lambda self, no_cache=False: installer_binary,
    )
    monkeypatch.setattr(
        CacheOwnerProvider,
        "installed_binaries",
        lambda self: [installed_binary],
    )
    monkeypatch.setattr(
        cli_module,
        "build_providers",
        lambda *args, **kwargs: [cache_owner],
    )

    options = cli_module.CliOptions(
        lib_dir=tmp_path / "abxlib",
        provider_names=["cache_owner"],
        dry_run=False,
        debug=False,
        no_cache=False,
    )
    final_env = cli_module.build_command_exec_env((), options=options, base_env={})

    assert final_env["INSTALLER_ENV"] == str(tmp_path / "installer")
    assert final_env["EXTRA_ENV"] == str(tmp_path / "extra")


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
    assert "choose only one of --bash, --zsh, or --fish" in result.output


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


def test_list_command_reads_provider_local_derived_env(tmp_path):
    provider = EnvProvider(
        install_root=tmp_path / "env",
        postinstall_scripts=True,
        min_release_age=0,
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
        min_release_age=0,
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
        min_release_age=0,
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


def test_list_command_filters_by_binary_name_and_provider_name(tmp_path):
    env_provider = EnvProvider(
        install_root=tmp_path / "env",
        postinstall_scripts=True,
        min_release_age=0,
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
    assert options.handler_overrides == {
        "abspath": "/tmp/custom-bin",
        "version": ["python3", "--version"],
        "install_args": ["black==24.2.0"],
        "packages": ["black==24.2.0"],
    }
    assert options.postinstall_scripts is False
    assert options.min_release_age == 14.0
    assert options.overrides == {"pip": {"install_args": ["black==24.2.0"]}}
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
    assert options.handler_overrides is None
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


def test_build_binary_merges_cli_handler_overrides_into_all_selected_providers(
    tmp_path,
):
    options = cli_module.CliOptions(
        lib_dir=tmp_path,
        provider_names=["env", "pip"],
        dry_run=False,
        debug=False,
        no_cache=False,
        handler_overrides={
            "version": ["python3", "--version"],
            "install_args": ["black==24.2.0"],
        },
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


def test_build_binary_explicit_overrides_deepmerge_over_cli_handler_defaults(tmp_path):
    options = cli_module.CliOptions(
        lib_dir=tmp_path,
        provider_names=["env", "pip"],
        dry_run=False,
        debug=False,
        no_cache=False,
        handler_overrides={
            "version": ["python3", "--version"],
            "install_args": ["black==24.2.0"],
        },
        overrides={
            "pip": {
                "install_args": ["black==25.0.0"],
                "version_timeout": 99,
            },
        },
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


def test_upgrade_command_dispatches_to_update(monkeypatch):
    captured = {}

    def fake_run_binary_command(binary_name, *, action, options):
        captured["binary_name"] = binary_name
        captured["action"] = action
        captured["options"] = options

    monkeypatch.setattr(cli_module, "run_binary_command", fake_run_binary_command)

    result = CliRunner().invoke(
        cli_module.cli,
        ["upgrade", "--binproviders=env", "python"],
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "python"
    assert captured["action"] == "update"


@pytest.mark.parametrize(
    ("argv", "lib_subdir"),
    [
        (
            [
                "--binproviders=pip",
                '--install-args=["black==25.0.0"]',
                '--overrides={"pip":{"install_args":["black==24.2.0"]}}',
                "--min-release-age=0",
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
                "--min-release-age=0",
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


def test_add_command_dispatches_to_install(monkeypatch):
    captured = {}

    def fake_run_binary_command(binary_name, *, action, options):
        captured["binary_name"] = binary_name
        captured["action"] = action
        captured["options"] = options

    monkeypatch.setattr(cli_module, "run_binary_command", fake_run_binary_command)

    result = CliRunner().invoke(
        cli_module.cli,
        ["add", "--binproviders=env", "python"],
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "python"
    assert captured["action"] == "install"


def test_remove_command_dispatches_to_uninstall(monkeypatch):
    captured = {}

    def fake_run_binary_command(binary_name, *, action, options):
        captured["binary_name"] = binary_name
        captured["action"] = action
        captured["options"] = options

    monkeypatch.setattr(cli_module, "run_binary_command", fake_run_binary_command)

    result = CliRunner().invoke(
        cli_module.cli,
        ["remove", "--binproviders=env", "python"],
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "python"
    assert captured["action"] == "uninstall"


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
        "--min-release-age=0",
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
        "--min-release-age=0",
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
        "--min-release-age=0",
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
        "--min-release-age=0",
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
        ("--min-release-age=0",),
        ("--min-release-age=0.5",),
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
        "--min-release-age=0",
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
        "--min-release-age=0",
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
        "--min-release-age=0",
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
        "--min-release-age=0",
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
        "--min-release-age=0",
        "run",
        "--script",
        "--install",
        "python3",
        str(script),
    )

    assert proc.returncode == 0, proc.stderr
    assert "black" in proc.stdout.lower()


def test_run_merges_selected_provider_runtime_env_without_script(tmp_path):
    """Plain run should merge runtime PATH/ENV from all selected providers."""

    lib = tmp_path / "lib"

    install_proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=pip",
        "--postinstall-scripts=True",
        "--min-release-age=0",
        "install",
        "black",
    )
    assert install_proc.returncode == 0, install_proc.stderr

    proc = _run_abxpkg_cli(
        f"--lib={lib}",
        "--binproviders=env,pip",
        "run",
        "python3",
        "-c",
        (
            "import os, shutil, sys; "
            "black = shutil.which('black'); "
            "sys.stdout.write((black or '') + '\\n' + os.environ.get('PATH','')); "
            "sys.exit(0 if black else 1)"
        ),
    )

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines
    assert lines[0].startswith(str(lib / "pip" / "venv" / "bin"))
    assert str(lib / "env" / "bin") in lines[1]
    assert str(lib / "pip" / "venv" / "bin") in lines[1]


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
            "--min-release-age=0",
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
            "--min-release-age=0",
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
        '//     {name = "node", binproviders = ["env", "apt", "brew"], min_version = "22.0.0"},\n'
        '//     {name = "playwright", binproviders = ["npm", "pnpm"]},\n'
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
        "    errors.push('chromium does not resolve into LIB_DIR/playwright: ' + chromiumPath + ' -> ' + chromiumReal);\n"
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
        "--min-release-age=0",
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

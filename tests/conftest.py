from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import tempfile
import traceback
import threading
from contextlib import contextmanager
import os
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from abxpkg import (
    AptProvider,
    Binary,
    BrewProvider,
    EnvProvider,
    GemProvider,
    NpmProvider,
    PnpmProvider,
    PlaywrightProvider,
    YarnProvider,
    SemVer,
)
from abxpkg.exceptions import BinaryLoadError


def _ensure_test_machine_dependencies() -> None:
    # Fail loudly if the test env is missing pyinfra / ansible_runner
    # rather than silently ``pip install``ing them at test-collection
    # time, which would hide a broken CI ``uv sync --all-extras``.
    missing: list[str] = []
    for module_name in ("ansible_runner", "pyinfra"):
        try:
            __import__(module_name)
        except ModuleNotFoundError:
            missing.append(module_name)
    if missing:
        raise RuntimeError(
            f"test-machine dependencies are missing from the active venv: {missing}. "
            f"Install them via `uv sync --all-extras` (or `pip install -e '.[ansible,pyinfra]'`).",
        )


class TestMachine:
    def assert_npm_release_age_gate(
        self,
        package: str,
        installed_version: SemVer,
        min_days: int,
    ) -> None:
        with urllib.request.urlopen(
            f"https://registry.npmjs.org/{package}",
            timeout=30,
        ) as response:
            metadata = json.load(response)

        cutoff = datetime.now(timezone.utc) - timedelta(days=min_days)
        installed = str(installed_version)
        latest = metadata["dist-tags"]["latest"]
        published = {
            version: datetime.fromisoformat(
                metadata["time"][version].replace("Z", "+00:00"),
            )
            for version in (installed, latest)
        }
        assert installed != latest
        assert published[installed] <= cutoff
        assert published[latest] > cutoff

    def require_tool(self, tool_name: str) -> str:
        # Use the CLI's normal resolver and activation policy, including its
        # provider-owned installer dependencies and validated caches.
        from abxpkg.click_cli import (
            CliOptions,
            build_command_exec_env,
            parse_provider_names,
            resolve_lib_dir,
            resolve_runtime_binary,
        )

        options = CliOptions(
            lib_dir=resolve_lib_dir(None),
            provider_names=parse_provider_names(None),
            dry_run=False,
            debug=False,
            no_cache=False,
        )
        loaded, _ = resolve_runtime_binary(
            tool_name,
            options=options,
            install_before_run=True,
        )
        os.environ.update(
            build_command_exec_env(
                [tool_name, "python"],
                options=options,
                install_before_run=True,
            ),
        )
        self.assert_shallow_binary_loaded(loaded, assert_version_command=False)
        assert loaded.loaded_abspath is not None, (
            f"{tool_name} is required on this host for test-machine integration tests",
        )
        return str(loaded.loaded_abspath)

    def require_docker_daemon(self) -> str:
        docker = self.require_tool("docker")
        proc = subprocess.run([docker, "info"], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        return docker

    def command_version(
        self,
        executable: Path,
        version_args: tuple[str, ...] = ("--version",),
    ) -> tuple[subprocess.CompletedProcess[str], SemVer | None]:
        proc = subprocess.run(
            [str(executable), *version_args],
            capture_output=True,
            text=True,
        )
        combined_output = "\n".join(
            part.strip() for part in (proc.stdout, proc.stderr) if part.strip()
        )
        return proc, SemVer.parse(combined_output)

    def assert_shallow_binary_loaded(
        self,
        loaded,
        *,
        version_args: tuple[str, ...] = ("--version",),
        assert_version_command: bool = False,
        expected_version: SemVer | None = None,
    ) -> None:
        assert loaded is not None
        assert loaded.is_valid
        assert loaded.loaded_binprovider is not None
        assert loaded.loaded_abspath is not None
        assert loaded.loaded_version is not None
        assert loaded.loaded_sha256 is not None
        assert loaded.loaded_mtime is not None
        assert loaded.loaded_euid is not None
        assert loaded.is_executable
        assert bool(str(loaded))

        provider = loaded.loaded_binprovider
        assert (
            provider.get_abspath(loaded.name, quiet=True, no_cache=True)
            == loaded.loaded_abspath
        )
        assert (
            provider.get_version(loaded.name, quiet=True, no_cache=True)
            == loaded.loaded_version
        )
        assert (
            provider.get_sha256(
                loaded.name,
                abspath=loaded.loaded_abspath,
                no_cache=True,
            )
            == loaded.loaded_sha256
        )
        assert loaded.loaded_mtime == loaded.loaded_abspath.resolve().stat().st_mtime_ns
        assert loaded.loaded_euid == loaded.loaded_abspath.resolve().stat().st_uid
        if provider.bin_dir is not None and not (
            provider.name == "env" and loaded.name in {"python", "python3"}
        ):
            expected_abspath = provider.bin_dir / loaded.name
            assert expected_abspath.exists()
            assert expected_abspath.is_relative_to(provider.bin_dir)
            assert loaded.loaded_respath is not None
            assert expected_abspath.resolve() == loaded.loaded_respath

        if expected_version is not None:
            assert loaded.loaded_version >= expected_version

        if assert_version_command:
            proc, parsed_version = self.command_version(
                loaded.loaded_abspath,
                version_args,
            )
            assert proc.returncode == 0, proc.stderr or proc.stdout
            if parsed_version is not None:
                assert loaded.loaded_version == parsed_version

    def assert_provider_missing(self, provider, bin_name: str) -> None:
        assert provider.load(bin_name, quiet=True, no_cache=True) is None
        assert provider.get_abspath(bin_name, quiet=True, no_cache=True) is None

    def assert_binary_missing(self, binary: Binary) -> None:
        with pytest.raises(BinaryLoadError):
            self.unloaded_binary(binary).load(no_cache=True)

    def unloaded_binary(self, binary: Binary) -> Binary:
        return binary.model_copy(
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

    def exercise_provider_lifecycle(
        self,
        provider,
        *,
        bin_name: str,
        version_args: tuple[str, ...] = ("--version",),
        install_kwargs: dict | None = None,
        update_kwargs: dict | None = None,
        assert_version_command: bool = True,
        expect_uninstall_result: bool = True,
    ):
        install_kwargs = install_kwargs or {}
        update_kwargs = update_kwargs or install_kwargs

        provider.setup(**install_kwargs)
        install_args = provider.get_install_args(bin_name)
        assert tuple(install_args)
        assert provider.get_packages(bin_name) == install_args

        self.assert_provider_missing(provider, bin_name)

        installed = provider.install(bin_name, no_cache=True, **install_kwargs)
        self.assert_shallow_binary_loaded(
            installed,
            version_args=version_args,
            assert_version_command=assert_version_command,
        )

        loaded = provider.load(bin_name, no_cache=True)
        self.assert_shallow_binary_loaded(
            loaded,
            version_args=version_args,
            assert_version_command=assert_version_command,
        )

        loaded_or_installed = provider.install(
            bin_name,
            no_cache=True,
            **install_kwargs,
        )
        self.assert_shallow_binary_loaded(
            loaded_or_installed,
            version_args=version_args,
            assert_version_command=assert_version_command,
        )

        updated = provider.update(bin_name, no_cache=True, **update_kwargs)
        self.assert_shallow_binary_loaded(
            updated,
            version_args=version_args,
            assert_version_command=assert_version_command,
        )

        uninstall_result = provider.uninstall(bin_name, no_cache=True, **install_kwargs)
        assert uninstall_result is expect_uninstall_result
        if expect_uninstall_result:
            self.assert_provider_missing(provider, bin_name)
        else:
            self.assert_shallow_binary_loaded(
                provider.load(bin_name, no_cache=True),
                version_args=version_args,
                assert_version_command=assert_version_command,
            )

        return installed, updated

    def exercise_binary_lifecycle(
        self,
        binary: Binary,
        *,
        version_args: tuple[str, ...] = ("--version",),
        assert_version_command: bool = True,
    ) -> None:
        fresh = self.unloaded_binary(binary)
        self.assert_binary_missing(fresh)

        installed = fresh.install()
        self.assert_shallow_binary_loaded(
            installed,
            version_args=version_args,
            assert_version_command=assert_version_command,
        )

        loaded = self.unloaded_binary(binary).load(no_cache=True)
        self.assert_shallow_binary_loaded(
            loaded,
            version_args=version_args,
            assert_version_command=assert_version_command,
        )

        loaded_or_installed = self.unloaded_binary(binary).install(no_cache=True)
        self.assert_shallow_binary_loaded(
            loaded_or_installed,
            version_args=version_args,
            assert_version_command=assert_version_command,
        )

        updated = installed.update()
        self.assert_shallow_binary_loaded(
            updated,
            version_args=version_args,
            assert_version_command=assert_version_command,
        )

        removed = updated.uninstall()
        assert not removed.is_valid
        assert removed.loaded_binprovider is None
        assert removed.loaded_abspath is None
        assert removed.loaded_version is None
        assert removed.loaded_sha256 is None
        assert removed.loaded_mtime is None
        assert removed.loaded_euid is None
        self.assert_binary_missing(binary)

    def exercise_provider_dry_run(
        self,
        provider,
        *,
        bin_name: str,
        expect_present_before: bool = False,
        stale_min_version: SemVer | None = None,
    ) -> None:
        before = provider.load(bin_name, quiet=True, no_cache=True)
        if expect_present_before:
            self.assert_shallow_binary_loaded(before, assert_version_command=False)
        else:
            assert before is None

        dry_run_provider = provider.get_provider_with_overrides(dry_run=True)
        if before is None or stale_min_version is not None:
            try:
                dry_loaded_or_installed = dry_run_provider.install(
                    bin_name,
                    no_cache=True,
                    min_version=stale_min_version,
                )
            except ValueError:
                assert before is not None
                assert stale_min_version is not None
                dry_loaded_or_installed = None
            if dry_loaded_or_installed is not None:
                assert dry_loaded_or_installed.loaded_version == SemVer("999.999.999")
                assert dry_loaded_or_installed.loaded_sha256 is not None
                assert dry_loaded_or_installed.loaded_mtime is not None
                assert dry_loaded_or_installed.loaded_euid is not None
            else:
                assert expect_present_before

        dry_installed = dry_run_provider.install(bin_name, no_cache=True)
        if dry_installed is not None:
            if expect_present_before and dry_installed.loaded_version != SemVer(
                "999.999.999",
            ):
                self.assert_shallow_binary_loaded(
                    dry_installed,
                    assert_version_command=False,
                )
            else:
                assert dry_installed.loaded_version == SemVer("999.999.999")
                assert dry_installed.loaded_sha256 is not None
                assert dry_installed.loaded_mtime is not None
                assert dry_installed.loaded_euid is not None
        else:
            assert expect_present_before

        dry_updated = dry_run_provider.update(bin_name, no_cache=True)
        if dry_updated is not None:
            if expect_present_before and dry_updated.loaded_version != SemVer(
                "999.999.999",
            ):
                self.assert_shallow_binary_loaded(
                    dry_updated,
                    assert_version_command=False,
                )
            else:
                assert dry_updated.loaded_version == SemVer("999.999.999")
                assert dry_updated.loaded_sha256 is not None
                assert dry_updated.loaded_mtime is not None
                assert dry_updated.loaded_euid is not None
        else:
            assert expect_present_before

        dry_removed = dry_run_provider.uninstall(bin_name, no_cache=True)
        assert isinstance(dry_removed, bool)

        after = provider.load(bin_name, quiet=True, no_cache=True)
        if expect_present_before:
            assert before is not None
            assert after is not None
            self.assert_shallow_binary_loaded(after, assert_version_command=False)
            assert after.loaded_abspath == before.loaded_abspath
            assert after.loaded_version == before.loaded_version
        else:
            assert after is None

    def pick_missing_brew_formula(self) -> str:
        probe = BrewProvider(postinstall_scripts=True, min_release_age=3)
        assert probe.is_valid
        brew_bin = probe.INSTALLER_BINARY().loaded_abspath
        candidates = ("hello", "tree", "rename", "jq", "watch", "fzy")
        for formula in candidates:
            proc = subprocess.run(
                [str(brew_bin), "list", "--formula", formula],
                capture_output=True,
                text=True,
            )
            if (
                proc.returncode != 0
                and probe.get_abspath(formula, quiet=True, no_cache=True) is None
            ):
                return formula
        for formula in candidates:
            probe.uninstall(formula, no_cache=True)
            proc = subprocess.run(
                [str(brew_bin), "list", "--formula", formula],
                capture_output=True,
                text=True,
            )
            if (
                proc.returncode != 0
                and probe.get_abspath(formula, quiet=True, no_cache=True) is None
            ):
                return formula
        raise AssertionError(
            "Unable to find a brew formula candidate that can be installed on the test machine",
        )

    def provider_for_host(self, provider_class, installer_name):
        self.require_tool(installer_name)
        apt_get = EnvProvider(install_root=None, bin_dir=None).load(
            "apt-get",
            no_cache=True,
        )
        if apt_get is None:
            self.require_tool("brew")
        provider = provider_class(
            postinstall_scripts=True,
            min_release_age=3,
        )
        return provider, self.pick_missing_provider_binary(
            provider,
            (
                "tree",
                "rename",
                "jq",
                "screen",
                "toilet",
                "btop",
                "ranger",
                "mc",
            )
            if apt_get is not None
            else (
                "hello",
                "jq",
                "watch",
                "fzy",
                "tree",
                "toilet",
                "btop",
                "ranger",
                "nnn",
            ),
        )

    def pick_missing_provider_binary(
        self,
        provider,
        candidates: tuple[str, ...],
    ) -> str:
        for candidate in candidates:
            if provider.load(candidate, quiet=True, no_cache=True) is not None:
                continue
            return candidate
        for candidate in candidates:
            try:
                provider.uninstall(candidate, quiet=True, no_cache=True)
            except Exception:
                continue
            if provider.load(candidate, quiet=True, no_cache=True) is not None:
                continue
            return candidate
        raise AssertionError(
            "No safe missing provider binary candidates were available for a test-machine lifecycle test",
        )

    def pick_missing_apt_package(self) -> str:
        provider = AptProvider(min_release_age=3)
        for package in ("tree", "rename", "jq", "tmux", "screen"):
            if provider.load(package, quiet=True, no_cache=True) is not None:
                continue
            return package
        for package in ("tree", "rename", "jq", "tmux", "screen"):
            try:
                provider.uninstall(package, quiet=True, no_cache=True)
            except Exception:
                continue
            if provider.load(package, quiet=True, no_cache=True) is not None:
                continue
            return package
        raise AssertionError(
            "No safe missing apt package candidates were available for a test-machine lifecycle test",
        )

    def pick_missing_gem_package(self) -> str:
        provider = GemProvider(min_release_age=3)
        for package in ("lolcat", "cowsay"):
            if provider.load(package, quiet=True, no_cache=True) is not None:
                continue
            return package
        raise AssertionError(
            "No safe missing gem package candidates were available for a test-machine lifecycle test",
        )


@pytest.fixture(scope="session")
def test_machine_dependencies():
    _ensure_test_machine_dependencies()


@pytest.fixture
def test_machine() -> TestMachine:
    return TestMachine()


def _real_python_binary(lib_dir: Path) -> Binary:
    provider = EnvProvider(install_root=lib_dir / "env")
    binary = Binary(name="python", binproviders=[provider]).load(no_cache=True)
    assert binary.loaded_abspath is not None
    return binary


def _brew_formula_is_installed(provider: BrewProvider, formula: str) -> bool:
    brew_bin = provider.INSTALLER_BINARY(no_cache=True).loaded_abspath
    assert brew_bin
    proc = provider.exec(
        bin_name=brew_bin,
        cmd=["list", "--formula", formula],
    )
    return proc.returncode == 0


def _run_with_lib_dir(
    lib_dir_value: str,
    script: str,
    *,
    extra_env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = lib_dir_value
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )


def assert_extension_binary_loaded(loaded) -> None:
    assert loaded is not None
    assert loaded.is_valid
    assert loaded.loaded_binprovider is not None
    assert loaded.loaded_binprovider.name == "chromewebstore"
    assert loaded.loaded_abspath is not None
    assert loaded.loaded_abspath.name.endswith(".extension.json")
    assert loaded.loaded_abspath.exists()
    assert loaded.loaded_version is not None
    assert loaded.loaded_sha256 is not None

    metadata = json.loads(loaded.loaded_abspath.read_text(encoding="utf-8"))
    assert metadata["webstore_url"] == loaded.docs_url()
    unpacked_path = Path(metadata["unpacked_path"])
    assert unpacked_path.exists()
    assert not (unpacked_path / "_metadata").exists()
    manifest = json.loads((unpacked_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == str(loaded.loaded_version)


def restore_signed_store_metadata_from_real_crx(
    unzip: str,
    crx_path: Path,
    unpacked_path: Path,
) -> None:
    assert crx_path.exists(), crx_path
    proc = subprocess.run(
        [unzip, "-q", "-o", str(crx_path), "-d", str(unpacked_path)],
        capture_output=True,
        text=True,
    )
    assert (unpacked_path / "manifest.json").exists(), proc.stderr or proc.stdout
    assert (unpacked_path / "_metadata").exists(), (
        "The real Chrome Web Store CRX did not restore signed-store metadata; "
        f"stdout={proc.stdout} stderr={proc.stderr}"
    )


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


@pytest.fixture
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


@pytest.fixture()
def abx_e2e_lib():
    """Provide a lib dir with playwright + chromium pre-installed.

    Uses a shared cache at ``/tmp/abx-e2e-lib`` so the ~370 MB browser
    download only happens once.

    Install order matters: npm playwright first (provides the CLI),
    then playwright provider installs the chromium browser.
    """

    lib = Path("/tmp/abx-e2e-lib")
    playwright_root = lib / "playwright"

    # Always let abxpkg validate and reuse its cache; existence alone is not validity.
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


def _resolve_shim_target(shim: Path) -> Path:
    """Resolve a managed bin_dir shim to its real browser target.

    On Linux the shim is a symlink, so ``.resolve()`` naturally follows
    it. On macOS the shim is a shell script that ``exec``s the binary
    inside a ``.app`` bundle (a direct symlink breaks dyld's
    ``@executable_path``-relative Framework loading), so ``.resolve()``
    just returns the script path itself. Parse the ``exec <path>`` line
    to recover the target in that case. We key off ``is_symlink()``
    rather than comparing ``shim == shim.resolve()`` because macOS
    ``$TMPDIR`` lives under ``/var/folders/...`` → ``/private/var/...``,
    so ``resolve()`` always differs from the input even for plain files.
    """
    if shim.is_symlink():
        return shim.resolve()
    try:
        script = shim.read_text(encoding="utf-8")
    except OSError:
        return shim.resolve()
    match = re.search(r"exec '([^']+)'", script)
    if not match:
        return shim.resolve()
    return Path(match.group(1)).resolve()


@pytest.fixture(scope="module")
def seeded_playwright_root():
    with tempfile.TemporaryDirectory() as temp_dir:
        install_root = Path(temp_dir) / "seeded-playwright-root"
        provider = PlaywrightProvider(install_root=install_root)
        installed = provider.install("chromium", no_cache=True)
        assert installed is not None
        assert installed.loaded_abspath is not None
        assert installed.loaded_abspath.exists()
        yield install_root


def copy_seeded_playwright_root(
    seeded_playwright_root: Path,
    install_root: Path,
) -> None:
    shutil.copytree(
        seeded_playwright_root,
        install_root,
        symlinks=True,
        copy_function=os.link,
    )
    copied_bin_dir = install_root / "bin"
    if not copied_bin_dir.is_dir():
        return
    seeded_resolved = seeded_playwright_root.resolve()
    for link_path in copied_bin_dir.iterdir():
        if link_path.is_symlink():
            link_target = link_path.resolve(strict=False)
            if seeded_resolved not in link_target.parents:
                continue
            relative_target = link_target.relative_to(seeded_resolved)
            link_path.unlink()
            link_path.symlink_to(install_root / relative_target)
            continue
        # macOS chrome/chromium shims are shell scripts that hardcode
        # the seeded install_root path; rewrite them so they exec the
        # copy under this test's install_root instead.
        if not link_path.is_file():
            continue
        try:
            script = link_path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r"exec '([^']+)'", script)
        if not match:
            continue
        target_path = Path(match.group(1))
        if seeded_resolved not in target_path.resolve().parents:
            continue
        relative_target = target_path.resolve().relative_to(seeded_resolved)
        new_target = install_root / relative_target
        link_path.write_text(
            script.replace(str(target_path), str(new_target)),
            encoding="utf-8",
        )


def _concurrent_pnpm_bootstrap_worker(
    lib_dir: str,
    host_bin: str,
    worker_index: int,
    barrier,
    results,
) -> None:
    os.environ["ABXPKG_LIB_DIR"] = lib_dir
    os.environ["PATH"] = os.pathsep.join([host_bin, "/usr/bin", "/bin"])
    os.environ["NPM_BINARY"] = str(Path(host_bin) / "npm")
    os.environ.pop("PNPM_BINARY", None)
    os.environ.pop("ABXPKG_NPM_CACHE_DIR", None)
    os.environ["ABXPKG_TMP_CACHE_DIR"] = str(
        Path(lib_dir) / "worker-caches" / str(worker_index),
    )
    provider = PnpmProvider(
        install_root=Path(lib_dir) / "pnpm" / "packages" / f"worker-{worker_index}",
        postinstall_scripts=True,
        min_release_age=0,
    )
    try:
        barrier.wait()
        installer = provider.INSTALLER_BINARY(no_cache=True)
        version = installer.exec(cmd=("--version",), quiet=True)
        results.put(
            (
                version.returncode == 0,
                str(installer.loaded_abspath),
                version.stderr,
            ),
        )
    except (
        AssertionError,
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ):
        results.put((False, "", traceback.format_exc()))


def _yarn_provider_for_kind(kind: str, **kwargs) -> YarnProvider:
    assert kind in {"classic", "berry"}
    version_threshold = SemVer.parse("2.0.0")
    current_path = str(YarnProvider(**kwargs).PATH)
    if kind == "berry":
        yarn_install_root = kwargs.get("install_root")
        assert isinstance(yarn_install_root, Path)
        npm_root = yarn_install_root.parent / "npm-yarn-berry"
        npm_provider = NpmProvider(
            install_root=npm_root / "package",
            alias_bin_dir=npm_root / "alias" / "bin",
            min_release_age=0,
        ).get_provider_with_overrides(
            overrides={
                "yarn-berry": {
                    "install_args": ["@yarnpkg/cli-dist@4.13.0"],
                },
            },
        )
        berry = Binary(
            name="yarn-berry",
            binproviders=[EnvProvider(), npm_provider],
            min_version=SemVer("4.13.0"),
            min_release_age=0,
        ).install(no_cache=True)
        berry_alias = berry.loaded_abspath
        assert berry_alias is not None, (
            "abxpkg did not resolve or install the Yarn Berry runtime"
        )
        # Peel the managed EnvProvider projection before inspecting npm's
        # logical yarn-berry alias; the alias itself points at the real
        # `yarn` launcher directory YarnProvider needs.
        if (
            berry_alias.is_symlink()
            and berry_alias.parent.name == "bin"
            and berry_alias.parent.parent.name == "env"
        ):
            projection_target = berry_alias.readlink()
            berry_alias = (
                projection_target
                if projection_target.is_absolute()
                else berry_alias.parent / projection_target
            ).absolute()
        berry_link = berry_alias.readlink() if berry_alias.is_symlink() else None
        berry_bin_dir = (
            (berry_alias.parent / berry_link).parent
            if berry_link and not berry_link.is_absolute()
            else (berry_link or berry_alias).parent
        )
        candidate_path = ":".join(
            dict.fromkeys(
                [
                    str(berry_bin_dir),
                    *[entry for entry in current_path.split(":") if entry],
                ],
            ),
        )
        provider = YarnProvider(PATH=candidate_path, **kwargs)
        installer = provider.INSTALLER_BINARY()
        version = installer.loaded_version
        assert (
            version is not None
            and version_threshold is not None
            and (version >= version_threshold)
        ), "yarn-berry must resolve to a Yarn 2+ installer"
        return provider

    provider = YarnProvider(PATH=current_path, **kwargs)
    installer = provider.INSTALLER_BINARY()
    version = installer.loaded_version
    assert (
        version is not None
        and version_threshold is not None
        and (version < version_threshold)
    ), "ambient yarn on PATH must resolve to Yarn 1.x for classic coverage"
    return provider


def _berry_provider(**kwargs) -> YarnProvider:
    return _yarn_provider_for_kind("berry", **kwargs)


def _classic_provider(**kwargs) -> YarnProvider:
    return _yarn_provider_for_kind("classic", **kwargs)


@contextmanager
def count_calls(function):
    """Count real function entries without replacing or intercepting execution."""
    calls = [0]

    def record_call(frame, event, _arg):
        if event == "call" and frame.f_code is function.__code__:
            calls[0] += 1

    previous = sys.getprofile()
    previous_threads = threading.getprofile()
    sys.setprofile(record_call)
    threading.setprofile(record_call)
    try:
        yield calls
    finally:
        sys.setprofile(previous)
        threading.setprofile(previous_threads)

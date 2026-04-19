from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from abxpkg import AptProvider, Binary, BrewProvider, SemVer
from abxpkg.exceptions import BinaryLoadError
from abxpkg.windows_compat import IS_WINDOWS, UNIX_ONLY_PROVIDER_NAMES

# On Windows every test file targeting a Unix-only provider (apt / brew /
# nix / bash / ansible / pyinfra / docker) is skipped. We hook into
# ``pytest_collection_modifyitems`` (not ``collect_ignore`` /
# ``pytest_ignore_collect``) because pytest bypasses those for paths
# passed explicitly on the command line (the CI per-file jobs do exactly
# that), while ``modifyitems`` runs after collection regardless of how
# the items got there.
_UNIX_ONLY_TEST_FILENAMES = frozenset(
    f"test_{name}provider.py" for name in UNIX_ONLY_PROVIDER_NAMES
)


def pytest_collection_modifyitems(config, items):
    if not IS_WINDOWS:
        return
    skip_marker = pytest.mark.skip(
        reason="Unix-only provider (not available on Windows, see windows_compat.UNIX_ONLY_PROVIDER_NAMES)",
    )
    for item in items:
        if item.path.name in _UNIX_ONLY_TEST_FILENAMES:
            item.add_marker(skip_marker)


def _brew_formula_is_installed(package: str) -> bool:
    brew = shutil.which("brew")
    if not brew:
        return False
    return (
        subprocess.run(
            [brew, "list", "--formula", package],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def _apt_package_is_installed(package: str) -> bool:
    dpkg = shutil.which("dpkg")
    if not dpkg:
        return False
    return (
        subprocess.run([dpkg, "-s", package], capture_output=True, text=True).returncode
        == 0
    )


def _gem_package_is_installed(package: str) -> bool:
    gem = shutil.which("gem")
    if not gem:
        return False
    return bool(
        subprocess.run(
            [gem, "list", f"^{package}$", "-a"],
            capture_output=True,
            text=True,
        ).stdout.strip(),
    )


def _docker_daemon_is_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    return (
        subprocess.run([docker, "info"], capture_output=True, text=True).returncode == 0
    )


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
    def require_tool(self, tool_name: str) -> str:
        tool_path = shutil.which(tool_name)
        assert tool_path, (
            f"{tool_name} is required on this host for test-machine integration tests"
        )
        return tool_path

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
        assert_version_command: bool = True,
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
        if provider.bin_dir is not None:
            # ``loaded.loaded_abspath`` is the actual on-disk path of the
            # resolved binary, including any OS-specific executable suffix
            # (``.exe`` / ``.cmd`` / ``.bat`` on Windows) — rebuilding the
            # path from ``bin_dir / loaded.name`` would miss the suffix.
            expected_abspath = loaded.loaded_abspath
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
            self.assert_shallow_binary_loaded(after, assert_version_command=False)
            assert after.loaded_abspath == before.loaded_abspath
            assert after.loaded_version == before.loaded_version
        else:
            assert after is None

    def pick_missing_brew_formula(self) -> str:
        provider = BrewProvider(min_release_age=0)
        for formula in ("hello", "tree", "rename", "jq", "watch", "fzy"):
            if _brew_formula_is_installed(formula):
                continue
            if provider.load(formula, quiet=True, no_cache=True) is not None:
                continue
            return formula
        raise AssertionError(
            "No safe missing brew formula candidates were available for a test-machine lifecycle test",
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
        provider = AptProvider(min_release_age=0)
        for package in ("tree", "rename", "jq", "tmux", "screen"):
            if _apt_package_is_installed(package):
                continue
            if provider.load(package, quiet=True, no_cache=True) is not None:
                continue
            return package
        for package in ("tree", "rename", "jq", "tmux", "screen"):
            try:
                provider.uninstall(package, quiet=True, no_cache=True)
            except Exception:
                continue
            if _apt_package_is_installed(package):
                continue
            if provider.load(package, quiet=True, no_cache=True) is not None:
                continue
            return package
        raise AssertionError(
            "No safe missing apt package candidates were available for a test-machine lifecycle test",
        )

    def pick_missing_gem_package(self) -> str:
        for package in ("lolcat", "cowsay"):
            if _gem_package_is_installed(package):
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

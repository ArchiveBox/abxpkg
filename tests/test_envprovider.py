import os
import sys
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from abxpkg import Binary, BrewProvider, EnvProvider, PipProvider, PnpmProvider, SemVer
from abxpkg.base_types import bin_abspaths
from abxpkg.config import load_derived_cache, save_derived_cache
from abxpkg.exceptions import BinaryUninstallError


class TestEnvProvider:
    def test_installer_binary_uses_fixed_version_override(self):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=3)

        installer = provider.INSTALLER_BINARY(no_cache=True)

        assert installer.loaded_abspath is not None
        assert installer.loaded_version is not None
        assert installer.loaded_euid is not None
        assert installer.loaded_abspath.name.startswith("which")
        expected_version = SemVer.parse("1.0.0")
        assert expected_version is not None
        assert installer.loaded_version == expected_version

    def test_provider_direct_methods_use_real_host_binaries(self, test_machine):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=3)

        install_args = provider.get_install_args("python")
        assert install_args == ("python",)
        assert provider.get_packages("python") == install_args

        python_bin = provider.load("python")
        test_machine.assert_shallow_binary_loaded(python_bin)
        assert python_bin is not None
        assert python_bin.loaded_respath == Path(sys.executable).resolve()
        assert python_bin.loaded_version == SemVer(
            "{}.{}.{}".format(*sys.version_info[:3]),
        )

        installed = provider.install("python", min_version=SemVer("3.0.0"))
        updated = provider.update("python", min_version=SemVer("3.0.0"))
        loaded_or_installed = provider.install(
            "python",
            min_version=SemVer("3.0.0"),
        )

        test_machine.assert_shallow_binary_loaded(installed)
        assert updated is None
        test_machine.assert_shallow_binary_loaded(loaded_or_installed)

        assert provider.uninstall("python") is False
        test_machine.assert_shallow_binary_loaded(provider.load("python"))

    def test_provider_projects_first_valid_host_git_from_path(
        self,
        tmp_path,
        test_machine,
    ):
        test_machine.require_tool("git")
        host_path = os.pathsep.join(
            entry
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry
            and not (Path(entry).name == "bin" and Path(entry).parent.name == "env")
        )
        provider = EnvProvider(
            install_root=tmp_path / "lib" / "env",
            PATH=host_path,
            postinstall_scripts=True,
            min_release_age=0,
        )
        first_valid_git = next(
            candidate
            for candidate in bin_abspaths("git", PATH=host_path)
            if provider.get_version(
                "git",
                abspath=candidate,
                quiet=True,
                no_cache=True,
            )
            is not None
        )

        loaded = provider.load("git", no_cache=True)

        assert loaded is not None
        assert loaded.loaded_abspath == tmp_path / "lib" / "env" / "bin" / "git"
        assert loaded.loaded_abspath.is_symlink()
        assert loaded.loaded_abspath.readlink() == first_valid_git
        result = loaded.exec(cmd=("--version",))
        assert result.returncode == 0, result.stderr

    def test_provider_projects_host_python_atomically_under_concurrency(self, tmp_path):
        """Parallel hook launches must all reuse the same host Python link."""
        provider = EnvProvider(
            install_root=tmp_path / "lib" / "env",
            postinstall_scripts=True,
            min_release_age=0,
        )
        host_python = Path(sys.executable).absolute()
        provider.setup_PATH()
        worker_count = 32
        ready = Barrier(worker_count)

        def project_host_python() -> Path:
            ready.wait()
            return Path(provider._link_loaded_binary("python3", host_python))

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            projected = list(
                pool.map(lambda _index: project_host_python(), range(worker_count)),
            )

        expected_link = tmp_path / "lib" / "env" / "bin" / "python3"
        assert projected == [expected_link] * worker_count
        assert expected_link.is_symlink()
        assert expected_link.readlink() == host_python

    def test_projected_host_brew_executes_with_its_host_prefix(
        self,
        tmp_path,
        test_machine,
    ):
        host_brew = Path(test_machine.require_tool("brew")).resolve()
        host_prefix = host_brew.parent.parent
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"HOMEBREW_PREFIX", "HOMEBREW_CELLAR"}
        }
        env_provider = EnvProvider(
            install_root=tmp_path / "lib" / "env",
            PATH=str(host_brew.parent),
            postinstall_scripts=True,
            min_release_age=0,
        )
        loaded = env_provider.load("brew", no_cache=True)
        assert loaded is not None
        assert loaded.loaded_abspath == tmp_path / "lib" / "env" / "bin" / "brew"
        assert loaded.loaded_abspath.is_symlink()

        contaminated_env = {
            **clean_env,
            "HOMEBREW_PREFIX": str(tmp_path / "lib" / "brew"),
            "HOMEBREW_CELLAR": str(tmp_path / "lib" / "brew" / "Cellar"),
        }

        result = loaded.exec(cmd=("--prefix",), env=contaminated_env)
        assert result.returncode == 0, result.stderr
        assert Path(result.args[0]) == host_brew
        assert Path(result.stdout.strip()) == host_prefix

    def test_combined_dependency_env_does_not_retarget_projected_host_brew(
        self,
        tmp_path,
        test_machine,
    ):
        host_brew = Path(test_machine.require_tool("brew")).resolve()
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"HOMEBREW_PREFIX", "HOMEBREW_CELLAR"}
        }
        host_prefix_result = subprocess.run(
            [str(host_brew), "--prefix"],
            capture_output=True,
            text=True,
            check=True,
            env=clean_env,
        )
        host_prefix = Path(host_prefix_result.stdout.strip())

        env_provider = EnvProvider(
            install_root=tmp_path / "lib" / "env",
            PATH=str(host_brew.parent),
            postinstall_scripts=True,
            min_release_age=0,
        )
        loaded = env_provider.load("brew", no_cache=True)
        assert loaded is not None
        assert loaded.loaded_abspath is not None

        managed_brew = BrewProvider(
            install_root=tmp_path / "lib" / "brew",
            postinstall_scripts=True,
            min_release_age=0,
        )
        combined_env = BrewProvider.build_exec_env(
            providers=[env_provider, managed_brew],
            base_env=clean_env,
            include_exec_only_env=False,
        )
        assert "HOMEBREW_PREFIX" not in combined_env
        assert "HOMEBREW_CELLAR" not in combined_env
        assert "HOMEBREW_NO_INSTALL_CLEANUP" not in combined_env

        result = loaded.exec(cmd=("--prefix",), env=combined_env)
        assert result.returncode == 0, result.stderr
        assert Path(result.stdout.strip()) == host_prefix

        scoped_env = managed_brew.build_exec_env(
            providers=[managed_brew],
            base_env=clean_env,
        )
        assert scoped_env["HOMEBREW_PREFIX"] == str(managed_brew.install_root)
        assert scoped_env["HOMEBREW_CELLAR"] == str(
            managed_brew.install_root / "Cellar",
        )
        assert scoped_env["HOMEBREW_NO_INSTALL_CLEANUP"] == "1"

    def test_brew_provider_executes_projected_installer_through_env_provider(
        self,
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
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"HOMEBREW_PREFIX", "HOMEBREW_CELLAR"}
        }
        host_prefix_result = subprocess.run(
            [str(host_brew), "--prefix"],
            capture_output=True,
            text=True,
            check=True,
            env=clean_env,
        )
        host_prefix = Path(host_prefix_result.stdout.strip())
        env_provider = EnvProvider(
            install_root=tmp_path / "lib" / "env",
            PATH=str(host_brew.parent),
            postinstall_scripts=True,
            min_release_age=0,
        )
        installer = env_provider.load("brew", no_cache=True)
        assert installer is not None
        assert installer.loaded_abspath is not None
        # EnvProvider may unwrap its own env/bin projection, but Homebrew's
        # launcher is itself a symlink on Linux. That host-owned hop must be
        # preserved because brew derives HOMEBREW_PREFIX from argv[0].
        assert env_provider._exec_bin_abspath(host_brew) == host_brew

        provider = BrewProvider(
            install_root=tmp_path / "lib" / "brew",
            postinstall_scripts=True,
            min_release_age=0,
        )
        provider._INSTALLER_BINARY = installer
        contaminated_env = {
            **clean_env,
            "HOMEBREW_PREFIX": str(provider.install_root),
            "HOMEBREW_CELLAR": str(provider.install_root / "Cellar"),
        }

        result = provider.exec(
            installer.loaded_abspath,
            cmd=("--prefix",),
            env=contaminated_env,
        )
        assert result.returncode == 0, result.stderr
        assert Path(result.args[0]) == host_brew
        assert Path(result.stdout.strip()) == host_prefix

    def test_nested_env_projection_keeps_the_original_host_brew_launcher(
        self,
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
        first_provider = EnvProvider(
            install_root=tmp_path / "first" / "env",
            PATH=str(host_brew.parent),
            postinstall_scripts=True,
            min_release_age=0,
        )
        first = first_provider.load("brew", no_cache=True)
        assert first is not None
        assert first.loaded_abspath is not None

        second_provider = EnvProvider(
            install_root=tmp_path / "second" / "env",
            PATH=str(first.loaded_abspath.parent),
            postinstall_scripts=True,
            min_release_age=0,
        )
        second = second_provider.load("brew", no_cache=True)
        assert second is not None
        assert second.loaded_abspath is not None
        assert second.loaded_abspath.readlink() == host_brew

        result = second.exec(cmd=("--prefix",))
        assert result.returncode == 0, result.stderr
        assert Path(result.args[0]) == host_brew

    def test_provider_direct_min_version_rejection_keeps_binary_available(
        self,
        test_machine,
    ):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=3)

        with pytest.raises(ValueError):
            provider.install("python", min_version=SemVer("999.0.0"))

        test_machine.assert_shallow_binary_loaded(provider.load("python"))

    def test_binary_direct_methods_use_env_provider(self, test_machine):
        binary = Binary(
            name="python",
            binproviders=[
                EnvProvider(postinstall_scripts=True, min_release_age=3),
            ],
            min_version=SemVer("3.0.0"),
            postinstall_scripts=True,
            min_release_age=3,
        )

        installed = binary.install()
        loaded = test_machine.unloaded_binary(binary).install()

        test_machine.assert_shallow_binary_loaded(installed)
        test_machine.assert_shallow_binary_loaded(loaded)
        with pytest.raises(BinaryUninstallError):
            installed.uninstall()
        test_machine.assert_shallow_binary_loaded(binary.load())

    def test_provider_dry_run_does_not_change_host_python(self, test_machine):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=3)
        before = provider.load("python", quiet=True, no_cache=True)
        test_machine.assert_shallow_binary_loaded(
            before,
            assert_version_command=False,
        )

        dry_run_provider = provider.get_provider_with_overrides(dry_run=True)

        with pytest.raises(ValueError):
            dry_run_provider.install(
                "python",
                no_cache=True,
                min_version=SemVer("999.0.0"),
            )

        dry_installed = dry_run_provider.install("python", no_cache=True)
        test_machine.assert_shallow_binary_loaded(
            dry_installed,
            assert_version_command=False,
        )

        assert dry_run_provider.update("python", no_cache=True) is None
        assert isinstance(dry_run_provider.uninstall("python", no_cache=True), bool)

        after = provider.load("python", quiet=True, no_cache=True)
        test_machine.assert_shallow_binary_loaded(after, assert_version_command=False)
        assert after is not None
        assert before is not None
        assert after.loaded_abspath == before.loaded_abspath
        assert after.loaded_version == before.loaded_version

    def test_provider_with_install_root_links_loaded_binary_and_writes_derived_env(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            install_root = Path(tmpdir) / "env"
            provider = EnvProvider(
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=3,
            )

            loaded = provider.load("python3")

            assert loaded is not None
            assert loaded.loaded_abspath is not None
            assert loaded.loaded_version is not None
            assert provider.bin_dir == install_root / "bin"
            assert provider.bin_dir is not None
            assert provider.bin_dir.exists()
            assert loaded.loaded_respath == Path(sys.executable).resolve()
            linked_binary = provider.bin_dir / "python3"
            assert linked_binary.is_symlink()
            assert linked_binary.resolve() == Path(sys.executable).resolve()

            derived_env_path = install_root / "derived.env"
            cache = load_derived_cache(derived_env_path)
            assert cache
            cache_key, cached_record = next(iter(cache.items()))
            assert f'"{provider.name}","python3"' in cache_key
            assert cached_record["provider_name"] == provider.name
            assert cached_record["bin_name"] == "python3"
            assert cached_record["abspath"] == str(loaded.loaded_abspath)
            assert cached_record["install_args"] == ["python3"]
            stat_result = loaded.loaded_abspath.stat()
            assert cached_record["inode"] == stat_result.st_ino
            assert cached_record["mtime"] == stat_result.st_mtime_ns

            assert provider.uninstall("python3") is False
            assert linked_binary.is_symlink()
            assert load_derived_cache(derived_env_path) == {}
            assert provider.load("python3", no_cache=True) is not None

    def test_provider_load_recovers_when_cached_context_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            install_root = Path(tmpdir) / "env"
            provider = EnvProvider(
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=3,
            )
            loaded = provider.load("python3")

            assert loaded is not None
            assert loaded.loaded_abspath is not None
            derived_env_path = install_root / "derived.env"
            cache = load_derived_cache(derived_env_path)
            assert cache
            cache_key, cached_record = next(iter(cache.items()))
            assert isinstance(cached_record["cache_context"], str)
            assert cached_record["install_args"] == ["python3"]
            cached_record["cache_context"] = "old-cache-context"
            save_derived_cache(derived_env_path, cache)

            reloaded = provider.load("python3")

            assert reloaded is not None
            assert reloaded.loaded_abspath == loaded.loaded_abspath
            refreshed = load_derived_cache(derived_env_path)
            assert refreshed[cache_key]["cache_context"] != "old-cache-context"

    def test_provider_does_not_claim_binaries_managed_by_other_providers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_dir = Path(tmpdir)
            pip_provider = PipProvider(
                install_root=lib_dir / "pip",
                postinstall_scripts=True,
                min_release_age=3,
            )
            installed = pip_provider.install("black")

            assert installed is not None
            assert installed.loaded_abspath is not None
            assert pip_provider.bin_dir is not None

            env_provider = EnvProvider(
                install_root=lib_dir / "env",
                PATH=str(pip_provider.bin_dir),
                postinstall_scripts=True,
                min_release_age=3,
            )
            loaded = env_provider.load("black", no_cache=True)

            assert loaded is None
            assert env_provider.install_root is not None
            assert load_derived_cache(env_provider.install_root / "derived.env") == {}
            assert env_provider.has_cached_binary("black") is False
            assert env_provider.bin_dir is not None
            assert not (env_provider.bin_dir / "black").exists()

            assert pip_provider.uninstall("black") is True

    def test_binary_uses_pnpm_provider_without_env_relinking_relative_launcher(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_dir = Path(tmpdir)
            pnpm_provider = PnpmProvider(
                install_root=lib_dir / "pnpm" / "packages" / "zx",
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = pnpm_provider.install("zx")

            assert installed is not None
            assert installed.loaded_abspath is not None
            assert pnpm_provider.bin_dir is not None

            env_bin_dir = lib_dir / "env" / "bin"
            # pnpm bootstrap projects its host node/npm dependencies here
            # before this stale package-launcher case is exercised.
            env_bin_dir.mkdir(parents=True, exist_ok=True)
            stale_env_link = env_bin_dir / "zx"
            stale_env_link.symlink_to(installed.loaded_abspath)

            env_provider = EnvProvider(
                install_root=lib_dir / "env",
                PATH=str(pnpm_provider.bin_dir),
                postinstall_scripts=True,
                min_release_age=0,
            )
            binary = Binary(
                name="zx",
                binproviders=[env_provider, pnpm_provider],
                postinstall_scripts=True,
                min_release_age=0,
            ).load(no_cache=True)

            assert binary.loaded_binprovider is not None
            assert binary.loaded_binprovider.name == "pnpm"
            assert binary.loaded_abspath == installed.loaded_abspath
            assert not stale_env_link.exists()
            assert not stale_env_link.is_symlink()

            result = binary.exec(cmd=("--version",), quiet=True)
            assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        ("package", "requested_bin", "projected_bin"),
        [
            ("@anthropic-ai/claude-code", "claude", "claude"),
            ("defuddle", "defuddle", "defuddle"),
            ("@llamaindex/liteparse", "lit", "liteparse"),
            (
                "readability-extractor",
                "readability-extractor",
                "readability-extractor",
            ),
        ],
    )
    def test_projected_host_pnpm_launcher_keeps_its_package_runtime(
        self,
        tmp_path,
        package,
        requested_bin,
        projected_bin,
    ):
        host_provider = PnpmProvider(
            install_root=tmp_path / "host-pnpm",
            postinstall_scripts=True,
            min_release_age=0,
        ).get_provider_with_overrides(
            overrides={requested_bin: {"install_args": [package]}},
        )
        host_binary = host_provider.install(requested_bin)
        assert host_binary is not None
        assert host_provider.install_root is not None
        assert host_provider.bin_dir is not None
        host_launcher = host_provider.bin_dir / projected_bin
        assert host_launcher.is_file()

        env_provider = EnvProvider(
            install_root=tmp_path / "lib" / "env",
            PATH=str(host_provider.bin_dir),
            postinstall_scripts=True,
            min_release_age=0,
        )
        loaded = env_provider.load(projected_bin, no_cache=True)

        assert loaded is not None
        assert loaded.loaded_abspath is not None
        projected = tmp_path / "lib" / "env" / "bin" / projected_bin
        assert loaded.loaded_abspath == projected
        assert projected.is_symlink()
        assert projected.readlink().is_absolute()
        assert projected.resolve().is_relative_to(
            host_provider.install_root / "node_modules" / ".pnpm",
        )
        assert not (tmp_path / "lib" / "env" / ".pnpm").exists()
        result = subprocess.run(
            [str(projected), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert (result.stdout + result.stderr).strip()

    def test_provider_does_not_reverse_link_shared_lib_bin_shims(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_dir = Path(tmpdir)
            env_bin_dir = lib_dir / "env" / "bin"
            lib_bin_dir = lib_dir / "bin"
            env_bin_dir.mkdir(parents=True)
            lib_bin_dir.mkdir()
            env_binary = env_bin_dir / "demo"
            env_binary.write_text("#!/bin/sh\nexit 0\n")
            env_binary.chmod(0o755)
            shared_shim = lib_bin_dir / "demo"
            shared_shim.symlink_to(env_binary)
            monkeypatch.setenv("LIB_BIN_DIR", str(lib_bin_dir))

            provider = EnvProvider(
                install_root=lib_dir / "env",
                postinstall_scripts=True,
                min_release_age=3,
            )

            linked_path = provider._link_loaded_binary("demo", shared_shim)

            assert linked_path == shared_shim
            assert shared_shim.is_symlink()
            assert shared_shim.readlink() == env_binary
            assert env_binary.is_file()
            assert not env_binary.is_symlink()

    def test_provider_never_discovers_human_convenience_lib_bin(
        self,
        monkeypatch,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_dir = Path(tmpdir)
            lib_bin_dir = lib_dir / "bin"
            lib_bin_dir.mkdir()
            convenience_link = lib_bin_dir / "human-only-git"
            convenience_link.symlink_to(test_machine.require_tool("git"))
            monkeypatch.setenv("ABXPKG_LIB_DIR", str(lib_dir))

            provider = EnvProvider(
                install_root=lib_dir / "env",
                PATH=str(lib_bin_dir),
                postinstall_scripts=True,
                min_release_age=3,
            )

            assert provider.load("human-only-git", no_cache=True) is None
            assert provider.bin_dir is not None
            assert not (provider.bin_dir / "human-only-git").exists()

    def test_search_returns_empty_for_env_provider(self):
        # EnvProvider has no package index — it just exposes ambient PATH —
        # so search must be an empty list rather than a crash or fallback.
        assert EnvProvider().search("python") == []
        assert EnvProvider().search("nonexistent-binary-xyz") == []

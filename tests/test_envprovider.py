import sys
import tempfile
from pathlib import Path

import pytest

from abxpkg import Binary, EnvProvider, PipProvider, SemVer
from abxpkg.config import load_derived_cache
from abxpkg.exceptions import BinaryUninstallError
from abxpkg.windows_compat import IS_WINDOWS


class TestEnvProvider:
    def test_installer_binary_uses_fixed_version_override(self):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=0)

        installer = provider.INSTALLER_BINARY(no_cache=True)

        assert installer.loaded_abspath is not None
        assert installer.loaded_version is not None
        assert installer.loaded_euid is not None
        assert installer.loaded_abspath.name.startswith("which")
        expected_version = SemVer.parse("1.0.0")
        assert expected_version is not None
        assert installer.loaded_version == expected_version

    def test_provider_direct_methods_use_real_host_binaries(self, test_machine):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=0)

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

    def test_provider_direct_min_version_rejection_keeps_binary_available(
        self,
        test_machine,
    ):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=0)

        with pytest.raises(ValueError):
            provider.install("python", min_version=SemVer("999.0.0"))

        test_machine.assert_shallow_binary_loaded(provider.load("python"))

    def test_binary_direct_methods_use_env_provider(self, test_machine):
        binary = Binary(
            name="python",
            binproviders=[
                EnvProvider(postinstall_scripts=True, min_release_age=0),
            ],
            min_version=SemVer("3.0.0"),
            postinstall_scripts=True,
            min_release_age=0,
        )

        installed = binary.install()
        loaded = test_machine.unloaded_binary(binary).install()

        test_machine.assert_shallow_binary_loaded(installed)
        test_machine.assert_shallow_binary_loaded(loaded)
        with pytest.raises(BinaryUninstallError):
            installed.uninstall()
        test_machine.assert_shallow_binary_loaded(binary.load())

    def test_provider_dry_run_does_not_change_host_python(self, test_machine):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=0)
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
                min_release_age=0,
            )

            loaded = provider.load("python3")

            assert loaded is not None
            assert loaded.loaded_abspath is not None
            assert loaded.loaded_version is not None
            assert provider.bin_dir == install_root / "bin"
            assert provider.bin_dir is not None
            assert provider.bin_dir.exists()
            assert loaded.loaded_respath == Path(sys.executable).resolve()
            # Unix: ``_link_loaded_binary`` creates a managed symlink in
            # ``bin_dir`` pointing to ``sys.executable``. Windows: venv-
            # rooted ``python.exe`` is returned unchanged by
            # ``link_binary`` because CPython's ``pyvenv.cfg`` discovery
            # can't follow the linked path — no shim is written, and
            # ``loaded_abspath`` is the real venv python.
            if not IS_WINDOWS:
                linked_binary = provider.bin_dir / "python3"
                assert linked_binary.is_symlink()
                assert linked_binary.resolve() == Path(sys.executable).resolve()
            else:
                assert loaded.loaded_abspath == Path(sys.executable)

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

    def test_provider_does_not_cache_binaries_managed_by_other_providers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_dir = Path(tmpdir)
            pip_provider = PipProvider(
                install_root=lib_dir / "pip",
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = pip_provider.install("black")

            assert installed is not None
            assert installed.loaded_abspath is not None
            assert pip_provider.bin_dir is not None

            env_provider = EnvProvider(
                install_root=lib_dir / "env",
                PATH=str(pip_provider.bin_dir),
                postinstall_scripts=True,
                min_release_age=0,
            )
            loaded = env_provider.load("black", no_cache=True)

            assert loaded is not None
            assert loaded.loaded_abspath is not None
            assert loaded.loaded_abspath.resolve() == installed.loaded_abspath.resolve()
            assert env_provider.install_root is not None
            assert load_derived_cache(env_provider.install_root / "derived.env") == {}
            assert env_provider.has_cached_binary("black") is False

            assert pip_provider.uninstall("black") is True

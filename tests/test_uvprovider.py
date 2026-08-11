import logging
import os
import pwd
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest

from abxpkg import Binary, EnvProvider, SemVer, UvProvider
from abxpkg.config import load_derived_cache
from abxpkg.exceptions import BinaryInstallError, BinProviderInstallError


class TestUvProvider:
    def test_uv_cache_is_enabled_unless_no_cache_is_explicit(self, tmp_path):
        provider = UvProvider(install_root=tmp_path / "uv")

        assert provider._cache_args() == []
        assert provider._cache_args(no_cache=True) == ["--no-cache"]

    def test_managed_root_access_does_not_modify_ancestors(self, tmp_path):
        lib_dir = tmp_path / "lib"
        install_root = lib_dir / "uv"
        package_root = install_root / "packages" / "demo"
        package_root.mkdir(parents=True)
        lib_dir.chmod(0o700)
        original_mode = lib_dir.stat().st_mode
        provider = UvProvider(install_root=install_root)

        provider._ensure_managed_root_access(package_root)

        assert lib_dir.stat().st_mode == original_mode

    def test_self_bootstrap_installs_uv_when_host_uv_is_not_on_path(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "uv-root"
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = "/usr/bin:/bin"
            try:
                assert (
                    EnvProvider(
                        PATH=os.environ["PATH"],
                        install_root=None,
                        bin_dir=None,
                    ).load(
                        "uv",
                        no_cache=True,
                    )
                    is None
                )
                provider = UvProvider(
                    install_root=install_root,
                    postinstall_scripts=True,
                    min_release_age=3,
                )

                installer = provider.INSTALLER_BINARY(no_cache=True)
                installed = provider.install("cowsay")
            finally:
                os.environ["PATH"] = old_path

            assert installer.loaded_abspath is not None
            assert installer.loaded_abspath.is_relative_to(
                install_root / "pip",
            )
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath == install_root / "venv" / "bin" / "cowsay"

    def test_installer_binary_is_cached_in_provider_local_derived_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            install_root = Path(tmpdir) / "uv-root"
            provider = UvProvider(
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=3,
            )

            installer = provider.INSTALLER_BINARY(no_cache=True)

            assert installer.loaded_abspath is not None
            cache = load_derived_cache(install_root / "derived.env")
            assert any(
                isinstance(record, dict)
                and record.get("provider_name") == provider.name
                and record.get("bin_name") == provider.INSTALLER_BIN
                for record in cache.values()
            )

            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ""
            try:
                reloaded_provider = UvProvider(
                    install_root=install_root,
                    postinstall_scripts=True,
                    min_release_age=3,
                )
                cached_installer = reloaded_provider.INSTALLER_BINARY()
            finally:
                os.environ["PATH"] = old_path

            assert cached_installer.loaded_abspath == installer.loaded_abspath
            assert cached_installer.loaded_version == installer.loaded_version

    def test_parent_provider_loads_package_scoped_venv_binaries(self, test_machine):
        with tempfile.TemporaryDirectory() as tmpdir:
            install_root = Path(tmpdir) / "uv-root"
            package_root = install_root / "packages" / "cowsay"
            package_provider = UvProvider(
                install_root=package_root,
                postinstall_scripts=True,
                min_release_age=3,
            )
            installed = package_provider.install("cowsay")
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert installed.loaded_abspath == package_root / "venv" / "bin" / "cowsay"

            parent_provider = UvProvider(
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=3,
            )
            reloaded = parent_provider.load("cowsay", quiet=True, no_cache=True)

            assert reloaded is not None
            assert reloaded.loaded_abspath == installed.loaded_abspath
            assert reloaded.is_valid
            assert reloaded.loaded_binprovider is not None
            assert reloaded.loaded_binprovider.name == "uv"
            assert reloaded.loaded_version is not None
            assert reloaded.loaded_sha256 is not None
            assert (
                parent_provider.get_abspath("cowsay", quiet=True, no_cache=True)
                == installed.loaded_abspath
            )

    def test_package_scoped_venvs_precede_shared_venv_on_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            install_root = Path(tmpdir) / "uv"
            shared_bin = install_root / "venv" / "bin"
            package_bin = install_root / "packages" / "forum-dl" / "venv" / "bin"
            shared_bin.mkdir(parents=True)
            package_bin.mkdir(parents=True)

            provider = UvProvider(
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=3,
            )
            provider.setup_PATH(no_cache=True)
            path_entries = provider.PATH.split(os.pathsep)

            assert path_entries.index(str(package_bin)) < path_entries.index(
                str(shared_bin),
            )

    def test_env_excludes_site_packages_from_foreign_python_minors(self, tmp_path):
        install_root = tmp_path / "uv"
        python_lib = f"python{sys.version_info.major}.{sys.version_info.minor}"
        foreign_python_lib = (
            f"python{sys.version_info.major}.{sys.version_info.minor + 1}"
        )
        active_site_packages = (
            install_root / "venv" / "lib" / python_lib / "site-packages"
        )
        foreign_site_packages = (
            install_root
            / "packages"
            / "forum-dl"
            / "venv"
            / "lib"
            / foreign_python_lib
            / "site-packages"
        )
        package_bin = foreign_site_packages.parents[2] / "bin"
        active_site_packages.mkdir(parents=True)
        foreign_site_packages.mkdir(parents=True)
        package_bin.mkdir(parents=True)

        env = UvProvider(
            install_root=install_root,
            postinstall_scripts=True,
            min_release_age=3,
        ).ENV

        assert str(active_site_packages) in env["PYTHONPATH"].split(os.pathsep)
        assert str(foreign_site_packages) not in env["PYTHONPATH"].split(os.pathsep)
        assert str(package_bin) in env["PATH"].split(os.pathsep)

    def test_managed_package_venv_repair_restores_entrypoint_access(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            install_root = Path(tmpdir) / "uv"
            provider = UvProvider(
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=3,
            )

            installed = provider.install("cowsay")

            assert installed is not None
            assert installed.loaded_abspath is not None
            entrypoint = installed.loaded_abspath
            entrypoint.chmod(
                entrypoint.stat().st_mode
                & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH),
            )
            assert not os.access(entrypoint, os.X_OK)

            package_root = install_root / "packages" / "cowsay"
            provider._ensure_managed_root_access(package_root)
            reloaded = provider.load("cowsay", quiet=True, no_cache=True)

            assert os.access(entrypoint, os.X_OK)
            assert reloaded is not None
            assert reloaded.loaded_abspath == entrypoint

    def test_root_managed_install_uses_sudo_invoking_uid(self, tmp_path):
        invoking_uid = next(
            entry.pw_uid for entry in pwd.getpwall() if entry.pw_uid > 0
        )
        provider = UvProvider(
            install_root=tmp_path / "uv",
            postinstall_scripts=True,
            min_release_age=3,
        )

        assert (
            provider._sudo_managed_install_euid(
                current_euid=0,
                environ={"SUDO_UID": str(invoking_uid)},
            )
            == invoking_uid
        )
        assert (
            provider._sudo_managed_install_euid(
                current_euid=invoking_uid,
                environ={"SUDO_UID": str(invoking_uid)},
            )
            is None
        )
        assert (
            provider._sudo_managed_install_euid(
                current_euid=0,
                environ={"SUDO_UID": "not-a-uid"},
            )
            is None
        )

    def test_managed_uv_root_installs_packages_into_isolated_venvs(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            install_root = Path(tmpdir) / "uv"
            provider = UvProvider(
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=3,
            )

            cowsay = provider.install("cowsay")
            pyfiglet = provider.install("pyfiglet")

            assert cowsay is not None
            assert pyfiglet is not None
            assert cowsay.loaded_abspath is not None
            assert pyfiglet.loaded_abspath is not None
            assert cowsay.loaded_abspath == (
                install_root / "packages" / "cowsay" / "venv" / "bin" / "cowsay"
            )
            assert pyfiglet.loaded_abspath == (
                install_root / "packages" / "pyfiglet" / "venv" / "bin" / "pyfiglet"
            )
            assert cowsay.loaded_abspath.exists()
            assert pyfiglet.loaded_abspath.exists()
            assert cowsay.loaded_version is not None
            assert pyfiglet.loaded_version is not None
            assert cowsay.loaded_sha256 is not None
            assert pyfiglet.loaded_sha256 is not None
            cowsay_cache = load_derived_cache(install_root / "derived.env")
            cowsay_record = cast(
                dict[str, Any],
                next(
                    record
                    for record in cowsay_cache.values()
                    if record.get("bin_name") == "cowsay"
                    and record.get("cache_kind") == "binary"
                ),
            )
            fingerprint_paths = {
                Path(fingerprint["path"])
                for fingerprint in cowsay_record["fingerprint"]
            }
            assert (
                install_root / "packages" / "cowsay" / "venv" / "bin" / "python"
            ).resolve() in fingerprint_paths
            assert any(path.name == "METADATA" for path in fingerprint_paths)
            assert not (install_root / "venv" / "bin" / "cowsay").exists()
            assert not (install_root / "venv" / "bin" / "pyfiglet").exists()

            reloaded_provider = UvProvider(
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=3,
            )
            assert (
                reloaded_provider.load(
                    "cowsay",
                    quiet=True,
                    no_cache=True,
                ).loaded_abspath
                == cowsay.loaded_abspath
            )
            assert (
                reloaded_provider.load(
                    "pyfiglet",
                    quiet=True,
                    no_cache=True,
                ).loaded_abspath
                == pyfiglet.loaded_abspath
            )

    def test_version_falls_back_to_uv_metadata_when_console_script_rejects_flags(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = UvProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=3,
            )
            installed = provider.install("saws")

            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_version is not None
            installer_binary = provider.INSTALLER_BINARY()
            assert installer_binary and installer_binary.loaded_abspath
            assert provider.install_root is not None

            metadata_proc = provider.exec(
                bin_name=installer_binary.loaded_abspath,
                cmd=[
                    "pip",
                    "show",
                    "--python",
                    str(provider.install_root / "venv" / "bin" / "python"),
                    "saws",
                ],
                timeout=provider.version_timeout,
                quiet=True,
            )
            assert metadata_proc.returncode == 0, (
                metadata_proc.stderr or metadata_proc.stdout
            )
            metadata_version = next(
                (
                    SemVer.parse(line.split("Version: ", 1)[1])
                    for line in metadata_proc.stdout.splitlines()
                    if line.startswith("Version: ")
                ),
                None,
            )
            assert metadata_version is not None

            failing_version_cmd = provider.exec(
                bin_name=installed.loaded_abspath,
                cmd=["--version"],
                quiet=True,
            )
            assert failing_version_cmd.returncode != 0

            assert installed.loaded_version == metadata_version
            assert (
                provider.get_version(
                    "saws",
                    abspath=installed.loaded_abspath,
                    quiet=True,
                    no_cache=True,
                )
                == metadata_version
            )

    def test_install_args_win_for_exclude_newer_flag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            venv_path = Path(temp_dir) / "venv"
            provider = UvProvider(
                install_root=venv_path,
                postinstall_scripts=True,
                min_release_age=36500,
            ).get_provider_with_overrides(
                overrides={
                    "cowsay": {
                        "install_args": [
                            "cowsay",
                            "--exclude-newer=2100-01-01T00:00:00Z",
                        ],
                    },
                },
            )

            installed = provider.install("cowsay")

            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.exists()
            # The provider-level 100yr ``min_release_age`` was overridden by
            # the explicit ``--exclude-newer=2100-01-01`` in install_args so
            # the resolver was able to pick a real version.
            assert installed.loaded_abspath == venv_path / "venv" / "bin" / "cowsay"

    def test_install_root_alias_installs_into_the_requested_venv(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "uv-venv"
            provider = UvProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            installed = provider.install("cowsay")

            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "venv" / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            # Real on-disk side effects: ``uv venv`` created a real venv.
            assert (install_root / "venv" / "pyvenv.cfg").exists()
            assert (install_root / "venv" / "bin" / "python").exists()
            # And the cowsay CLI got wired up inside the venv.
            assert (install_root / "venv" / "bin" / "cowsay").exists()

    def test_install_root_module_dependency_loads_without_console_script(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "uv-venv"
            provider = UvProvider(
                install_root=install_root,
                postinstall_scripts=False,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={"imagesize": {"install_args": ["imagesize>=2.0.0"]}},
            )

            installed = provider.install("imagesize")

            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.name in {"__init__.py", "imagesize.py"}
            assert "site-packages" in str(installed.loaded_abspath)
            assert installed.loaded_version is not None

    def test_explicit_venv_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = UvProvider(
                install_root=temp_dir_path / "ambient-venv",
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={"cowsay": {"install_args": ["cowsay==6.0"]}},
            )
            ambient_installed = ambient_provider.install("cowsay")
            assert ambient_installed is not None
            assert ambient_installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath.parent == ambient_provider.bin_dir
            assert ambient_installed.loaded_version == SemVer("6.0.0")

            install_root = temp_dir_path / "uv-venv"
            provider = UvProvider(
                PATH=str(ambient_provider.bin_dir),
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={"cowsay": {"install_args": ["cowsay==6.1"]}},
            )

            installed = provider.install("cowsay")

            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "venv" / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert installed.loaded_abspath != ambient_installed.loaded_abspath
            assert installed.loaded_version == SemVer("6.1.0")
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_setup_falls_back_to_no_cache_when_cache_dir_is_not_a_directory(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            cache_file = tmp_path / "uv-cache-file"
            cache_file.write_text("not-a-directory", encoding="utf-8")

            provider = UvProvider(
                install_root=tmp_path / "venv",
                postinstall_scripts=True,
                min_release_age=3,
            )

            installed = provider.install("cowsay")
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = UvProvider(
                install_root=Path(temp_dir) / "venv",
                postinstall_scripts=True,
                min_release_age=3,
            )
            installed, _ = test_machine.exercise_provider_lifecycle(
                provider,
                bin_name="cowsay",
                assert_version_command=False,
            )
            assert installed.loaded_abspath is not None
            assert provider.install_root is not None
            assert installed.loaded_abspath.is_relative_to(provider.install_root)

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_path = Path(tmpdir) / "venv"
            old_provider = UvProvider(
                install_root=venv_path,
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={"black": {"install_args": ["black==23.1.0"]}},
            )
            old_installed = old_provider.install("black", min_version=SemVer("1.0.0"))
            assert old_installed is not None
            assert old_installed.loaded_version is not None
            required_version = SemVer.parse("24.0.0")
            assert required_version is not None
            assert tuple(old_installed.loaded_version) < tuple(required_version)

            upgraded = UvProvider(
                install_root=venv_path,
                postinstall_scripts=True,
                min_release_age=3,
            ).install("black", min_version=SemVer("24.0.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("24.0.0"),
            )
            assert upgraded is not None
            assert upgraded.loaded_version is not None
            assert old_installed.loaded_version is not None
            assert upgraded.loaded_version > old_installed.loaded_version

            updated = UvProvider(
                install_root=venv_path,
                postinstall_scripts=True,
                min_release_age=3,
            ).update("black", min_version=SemVer("24.0.0"))
            test_machine.assert_shallow_binary_loaded(
                updated,
                expected_version=SemVer("24.0.0"),
            )

    def test_provider_defaults_and_binary_overrides_enforce_min_release_age(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = UvProvider(
                install_root=Path(tmpdir) / "strict-venv",
                postinstall_scripts=True,
                min_release_age=36500,
            )
            assert strict_provider.supports_min_release_age("install") is True

            with pytest.raises(BinProviderInstallError):
                strict_provider.install("cowsay")
            test_machine.assert_provider_missing(strict_provider, "cowsay")

            direct_override = strict_provider.install("cowsay", min_release_age=3)
            test_machine.assert_shallow_binary_loaded(
                direct_override,
                assert_version_command=False,
            )
            assert strict_provider.uninstall("cowsay", min_release_age=3)

            binary = Binary(
                name="cowsay",
                binproviders=[
                    UvProvider(
                        install_root=Path(tmpdir) / "binary-venv",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=3,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )

    def test_provider_defaults_and_binary_overrides_enforce_postinstall_scripts(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = UvProvider(
                install_root=Path(tmpdir) / "strict-venv",
                postinstall_scripts=False,
                min_release_age=3,
            )
            assert strict_provider.supports_postinstall_disable("install") is True

            # ``saws`` is a pip-only sdist, so strict wheel-only mode
            # (``--no-build``) can't install it.
            with pytest.raises(BinProviderInstallError):
                strict_provider.install("saws")
            test_machine.assert_provider_missing(strict_provider, "saws")

            direct_override = strict_provider.install(
                "saws",
                postinstall_scripts=True,
            )
            test_machine.assert_shallow_binary_loaded(
                direct_override,
                assert_version_command=False,
            )
            assert strict_provider.uninstall("saws", postinstall_scripts=True)

            binary = Binary(
                name="saws",
                binproviders=[
                    UvProvider(
                        install_root=Path(tmpdir) / "binary-venv",
                        postinstall_scripts=False,
                        min_release_age=3,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=3,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )

            failing_binary = Binary(
                name="saws",
                binproviders=[
                    UvProvider(
                        install_root=Path(tmpdir) / "failing-venv",
                        postinstall_scripts=False,
                        min_release_age=3,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=3,
            )
            with pytest.raises(BinaryInstallError):
                failing_binary.install()

    def test_install_rolls_back_package_when_no_runnable_binary_is_produced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = UvProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=False,
                min_release_age=7,
            )

            with pytest.raises(BinProviderInstallError):
                provider.install("chromium")

            installer_bin = provider.INSTALLER_BINARY().loaded_abspath
            assert installer_bin is not None
            assert provider.install_root is not None
            proc = provider.exec(
                bin_name=installer_bin,
                cmd=[
                    "pip",
                    "show",
                    "--python",
                    str(provider.install_root / "venv" / "bin" / "python"),
                    "chromium",
                ],
                quiet=True,
            )
            assert proc.returncode != 0
            assert provider.load("chromium", quiet=True, no_cache=True) is None

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="cowsay",
                binproviders=[
                    UvProvider(
                        install_root=Path(temp_dir) / "venv",
                        postinstall_scripts=True,
                        min_release_age=3,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=3,
            )
            test_machine.exercise_binary_lifecycle(
                binary,
                assert_version_command=False,
            )

    def test_provider_dry_run_does_not_install_cowsay(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = UvProvider(
                install_root=Path(temp_dir) / "venv",
                postinstall_scripts=True,
                min_release_age=3,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="cowsay")
            # dry_run must not have actually installed anything into the venv.
            assert not (Path(temp_dir) / "venv" / "venv" / "bin" / "cowsay").exists()

    def test_provider_action_args_override_provider_defaults(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = UvProvider(
                install_root=Path(temp_dir) / "venv",
                dry_run=True,
                postinstall_scripts=False,
                min_release_age=36500,
            )

            installed = provider.install(
                "cowsay",
                dry_run=False,
                postinstall_scripts=True,
                min_release_age=3,
            )
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_global_tool_mode_installs_into_uv_tool_bin_dir(self, test_machine):
        """With no ``uv_venv``, UvProvider falls back to ``uv tool install``."""
        with tempfile.TemporaryDirectory() as temp_dir:
            tool_dir = Path(temp_dir) / "tools"
            tool_bin_dir = Path(temp_dir) / "bin"
            old_tool_dir = os.environ.get("UV_TOOL_DIR")
            try:
                os.environ["UV_TOOL_DIR"] = str(tool_dir)
                provider = UvProvider(
                    install_root=None,
                    bin_dir=tool_bin_dir,
                    postinstall_scripts=True,
                    min_release_age=3,
                )

                installed = provider.install("cowsay")

                test_machine.assert_shallow_binary_loaded(
                    installed,
                    assert_version_command=False,
                )
                assert installed is not None
                assert installed.loaded_abspath is not None
                # Global mode lays shims in UV_TOOL_BIN_DIR.
                assert installed.loaded_abspath.parent == tool_bin_dir
                # And gives each tool its own venv under UV_TOOL_DIR.
                assert (tool_dir / "cowsay" / "pyvenv.cfg").exists()

                assert provider.uninstall("cowsay") is True
                assert provider.load("cowsay", quiet=True, no_cache=True) is None
            finally:
                if old_tool_dir is None:
                    os.environ.pop("UV_TOOL_DIR", None)
                else:
                    os.environ["UV_TOOL_DIR"] = old_tool_dir

    def test_global_tool_mode_can_load_and_uninstall_without_bin_shim(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            tool_dir = Path(temp_dir) / "tools"
            tool_bin_dir = Path(temp_dir) / "bin"
            old_tool_dir = os.environ.get("UV_TOOL_DIR")
            old_tool_bin_dir = os.environ.get("UV_TOOL_BIN_DIR")
            try:
                os.environ["UV_TOOL_DIR"] = str(tool_dir)
                os.environ["UV_TOOL_BIN_DIR"] = str(tool_bin_dir)
                provider = UvProvider(
                    install_root=None,
                    postinstall_scripts=True,
                    min_release_age=3,
                )

                installed = provider.install("cowsay")

                test_machine.assert_shallow_binary_loaded(
                    installed,
                    assert_version_command=False,
                )
                assert installed is not None
                shim_path = tool_bin_dir / "cowsay"
                assert shim_path.exists()
                shim_path.unlink()

                reloaded = provider.load("cowsay", quiet=True, no_cache=True)
                test_machine.assert_shallow_binary_loaded(
                    reloaded,
                    assert_version_command=False,
                )
                assert reloaded is not None
                assert reloaded.loaded_abspath == tool_dir / "cowsay" / "bin" / "cowsay"

                assert provider.uninstall("cowsay") is True
                assert provider.load("cowsay", quiet=True, no_cache=True) is None
            finally:
                if old_tool_dir is None:
                    os.environ.pop("UV_TOOL_DIR", None)
                else:
                    os.environ["UV_TOOL_DIR"] = old_tool_dir
                if old_tool_bin_dir is None:
                    os.environ.pop("UV_TOOL_BIN_DIR", None)
                else:
                    os.environ["UV_TOOL_BIN_DIR"] = old_tool_bin_dir

    def test_supports_methods_do_not_emit_unsupported_warnings(self, caplog):
        with tempfile.TemporaryDirectory() as tmpdir:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                provider = UvProvider(
                    install_root=Path(tmpdir) / "venv",
                    postinstall_scripts=False,
                    min_release_age=3,
                )
                installed = provider.install("cowsay")
                assert installed is not None
            assert "ignoring unsupported postinstall_scripts" not in caplog.text
            assert "ignoring unsupported min_release_age" not in caplog.text

    def test_binary_install_failure_propagates_as_BinaryInstallError(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            failing_binary = Binary(
                name="cowsay",
                binproviders=[
                    UvProvider(
                        install_root=Path(tmpdir) / "venv",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=36500,
            )
            with pytest.raises(BinaryInstallError):
                failing_binary.install()

    def test_search_finds_real_pypi_package_and_install_works(self, test_machine):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = UvProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=3,
            )
            results = provider.search("black")
            assert len(results) == 1
            match = results[0]
            assert match.name == "black"
            assert match.overrides == {"uv": {"install_args": ["black"]}}
            assert match.loaded_abspath is None
            assert match.loaded_version is None
            installed = match.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed.name == "black"

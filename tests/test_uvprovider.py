import logging
import os
import tempfile
from pathlib import Path

import pytest

from abxpkg import Binary, SemVer, UvProvider
from abxpkg.config import load_derived_cache
from abxpkg.exceptions import BinaryInstallError, BinProviderInstallError
from abxpkg.windows_compat import VENV_BIN_SUBDIR, VENV_PYTHON_BIN


class TestUvProvider:
    def test_installer_binary_is_cached_in_provider_local_derived_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            install_root = Path(tmpdir) / "uv-root"
            provider = UvProvider(
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=0,
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
                    min_release_age=0,
                )
                cached_installer = reloaded_provider.INSTALLER_BINARY()
            finally:
                os.environ["PATH"] = old_path

            assert cached_installer.loaded_abspath == installer.loaded_abspath
            assert cached_installer.loaded_version == installer.loaded_version

    def test_version_falls_back_to_uv_metadata_when_console_script_rejects_flags(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = UvProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
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
                    str(
                        provider.install_root
                        / "venv"
                        / VENV_BIN_SUBDIR
                        / VENV_PYTHON_BIN
                    ),
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
            # On Windows the console-script shim is ``cowsay.exe`` while
            # POSIX writes bare ``cowsay`` — compare via ``.stem`` so both
            # layouts pass.
            assert installed.loaded_abspath.parent == (
                venv_path / "venv" / VENV_BIN_SUBDIR
            )
            assert installed.loaded_abspath.stem == "cowsay"

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
            assert provider.bin_dir == install_root / "venv" / VENV_BIN_SUBDIR
            assert installed.loaded_abspath.parent == provider.bin_dir
            # Real on-disk side effects: ``uv venv`` created a real venv.
            assert (install_root / "venv" / "pyvenv.cfg").exists()
            assert (install_root / "venv" / VENV_BIN_SUBDIR / VENV_PYTHON_BIN).exists()
            # And the cowsay CLI got wired up inside the venv. On Windows
            # it's ``cowsay.exe``; use ``shutil.which``-style PATHEXT
            # resolution via ``bin_abspath``.
            from abxpkg.base_types import bin_abspath as _ba

            assert (
                _ba(
                    "cowsay",
                    PATH=str(install_root / "venv" / VENV_BIN_SUBDIR),
                )
                is not None
            )

    def test_explicit_venv_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = UvProvider(
                install_root=temp_dir_path / "ambient-venv",
                postinstall_scripts=True,
                min_release_age=0,
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
                min_release_age=0,
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
            assert provider.bin_dir == install_root / "venv" / VENV_BIN_SUBDIR
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
                min_release_age=0,
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
                min_release_age=0,
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
                min_release_age=0,
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
                min_release_age=0,
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
                min_release_age=0,
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

            direct_override = strict_provider.install("cowsay", min_release_age=0)
            test_machine.assert_shallow_binary_loaded(
                direct_override,
                assert_version_command=False,
            )
            assert strict_provider.uninstall("cowsay", min_release_age=0)

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
                min_release_age=0,
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
                min_release_age=0,
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
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
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
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=0,
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
                    str(
                        provider.install_root
                        / "venv"
                        / VENV_BIN_SUBDIR
                        / VENV_PYTHON_BIN
                    ),
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
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
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
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="cowsay")
            # dry_run must not have actually installed anything into the venv.
            # ``dry_run`` must not have installed anything; check the venv
            # scripts dir is clean regardless of Windows ``.exe`` suffix.
            from abxpkg.base_types import bin_abspath as _ba

            assert (
                _ba(
                    "cowsay",
                    PATH=str(Path(temp_dir) / "venv" / "venv" / VENV_BIN_SUBDIR),
                )
                is None
            )

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
                min_release_age=0,
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
                    min_release_age=0,
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
                    min_release_age=0,
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
                    min_release_age=0,
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

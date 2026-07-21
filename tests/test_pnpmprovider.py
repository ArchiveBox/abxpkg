import logging
import multiprocessing
import os
import subprocess
import tempfile
import traceback
from pathlib import Path

import pytest

from abxpkg import Binary, PnpmProvider, SemVer
from abxpkg.base_types import bin_abspath
from abxpkg.exceptions import BinaryInstallError, BinProviderInstallError


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
    except BaseException:
        results.put((False, "", traceback.format_exc()))


class TestPnpmProvider:
    def test_concurrent_managed_roots_share_one_safe_pnpm_bootstrap(
        self,
        tmp_path,
        test_machine,
    ):
        npm_binary = Path(test_machine.require_tool("npm")).resolve()
        node_binary = Path(test_machine.require_tool("node")).resolve()
        lib_dir = tmp_path / "lib"
        host_bin = tmp_path / "host-bin"
        host_bin.mkdir()
        (host_bin / "npm").symlink_to(npm_binary)
        (host_bin / "node").symlink_to(node_binary)

        process_count = 20
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(process_count)
        results = context.Queue()
        processes = [
            context.Process(
                target=_concurrent_pnpm_bootstrap_worker,
                args=(
                    str(lib_dir),
                    str(host_bin),
                    worker_index,
                    barrier,
                    results,
                ),
            )
            for worker_index in range(process_count)
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(180)

        outcomes = [results.get(timeout=5) for _ in processes]
        assert all(process.exitcode == 0 for process in processes)
        assert all(success for success, _path, _error in outcomes), outcomes
        expected_installer = (
            lib_dir / "npm" / "packages" / "pnpm" / "node_modules" / ".bin" / "pnpm"
        )
        assert expected_installer.is_file()
        assert {Path(path).resolve() for _success, path, _error in outcomes} == {
            expected_installer.resolve(),
        }
        npm_mutation_logs = list(
            (lib_dir / "worker-caches").glob("*/npm/_logs/*-debug-0.log"),
        )
        assert len(npm_mutation_logs) == 1, npm_mutation_logs

    @pytest.mark.parametrize(
        ("node_version", "expected_package"),
        [
            (SemVer(24, 0, 0), "pnpm"),
            (SemVer(22, 13, 0), "pnpm"),
            (SemVer(22, 12, 0), "pnpm@10"),
            (SemVer(18, 12, 0), "pnpm@10"),
            (SemVer(18, 11, 0), "pnpm@8"),
            (SemVer(16, 14, 0), "pnpm@8"),
            (SemVer(14, 6, 0), "pnpm@7"),
            (SemVer(12, 17, 0), "pnpm@6"),
            (SemVer(10, 16, 0), "pnpm@5"),
            (SemVer(10, 13, 0), "pnpm@4"),
            (SemVer(8, 15, 0), "pnpm@3"),
        ],
    )
    def test_self_bootstrap_selects_pnpm_compatible_with_node(
        self,
        node_version,
        expected_package,
    ):
        assert PnpmProvider._pnpm_package_for_node(node_version) == expected_package

    def test_store_dir_reuses_existing_install_root_store(self, tmp_path):
        install_root = tmp_path / "pnpm"
        expected_store = tmp_path / "existing-store"
        provider = PnpmProvider(
            install_root=install_root,
            postinstall_scripts=True,
            min_release_age=3,
        )

        assert provider.install_root is not None
        modules_dir = provider.install_root / "node_modules"
        modules_dir.mkdir(parents=True)
        (modules_dir / ".modules.yaml").write_text(
            f"storeDir: {expected_store}\n",
            encoding="utf-8",
        )

        assert provider._store_dir() == expected_store

    def test_refresh_bin_link_preserves_pnpm_shim_basedir_behavior(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            package_bin_dir = temp_path / "package" / "node_modules" / ".bin"
            exposed_bin_dir = temp_path / "bin"
            target = package_bin_dir / "demo"
            package_bin_dir.mkdir(parents=True)
            target.write_text('#!/bin/sh\ncat "$(dirname "$0")/payload.txt"\n')
            target.chmod(0o755)
            (package_bin_dir / "payload.txt").write_text("ok")

            provider = PnpmProvider(
                install_root=temp_path / "pnpm",
                bin_dir=exposed_bin_dir,
                postinstall_scripts=True,
                min_release_age=3,
            )

            exposed = provider._refresh_bin_link("demo", target)
            result = subprocess.run(
                [str(exposed)],
                check=True,
                capture_output=True,
                text=True,
            )
            assert result.stdout == "ok"

    def test_self_bootstrap_installs_pnpm_when_host_pnpm_is_not_on_path(
        self,
        test_machine,
    ):
        npm_binary = test_machine.require_tool("npm")
        node_binary = test_machine.require_tool("node")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "pnpm-root"
            node_bin_dir = Path(node_binary).resolve().parent
            constrained_path = os.pathsep.join([str(node_bin_dir), "/usr/bin", "/bin"])
            old_path = os.environ.get("PATH", "")
            old_npm_binary = os.environ.get("NPM_BINARY")
            os.environ["PATH"] = constrained_path
            os.environ["NPM_BINARY"] = npm_binary
            try:
                assert bin_abspath("pnpm", PATH=os.environ["PATH"]) is None
                provider = PnpmProvider(
                    install_root=install_root,
                    postinstall_scripts=True,
                    min_release_age=3,
                )

                installer = provider.INSTALLER_BINARY(no_cache=True)
                installed = provider.install("zx")
            finally:
                os.environ["PATH"] = old_path
                if old_npm_binary is None:
                    os.environ.pop("NPM_BINARY", None)
                else:
                    os.environ["NPM_BINARY"] = old_npm_binary

            assert installer.loaded_abspath is not None
            assert installer.loaded_abspath.is_relative_to(
                install_root / "npm",
            )
            installer_version = installer.exec(cmd=("--version",), quiet=True)
            assert installer_version.returncode == 0
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert (
                installed.loaded_abspath
                == install_root / "node_modules" / ".bin" / "zx"
            )

    def test_self_bootstrap_projects_host_dependencies_into_managed_env_bin(
        self,
        test_machine,
    ):
        npm_binary = Path(test_machine.require_tool("npm")).absolute()
        node_binary = Path(test_machine.require_tool("node")).absolute()

        with tempfile.TemporaryDirectory() as temp_dir:
            lib_dir = Path(temp_dir) / "lib"
            host_bin = Path(temp_dir) / "host-bin"
            host_bin.mkdir()
            (host_bin / "npm").symlink_to(npm_binary)
            (host_bin / "node").symlink_to(node_binary)
            old_path = os.environ.get("PATH", "")
            old_npm_binary = os.environ.get("NPM_BINARY")
            os.environ["PATH"] = str(host_bin)
            os.environ["NPM_BINARY"] = str(host_bin / "npm")
            provider = PnpmProvider(
                install_root=lib_dir / "pnpm",
                postinstall_scripts=True,
                min_release_age=0,
            )

            try:
                installer = provider.INSTALLER_BINARY(no_cache=True)
            finally:
                os.environ["PATH"] = old_path
                if old_npm_binary is None:
                    os.environ.pop("NPM_BINARY", None)
                else:
                    os.environ["NPM_BINARY"] = old_npm_binary

            assert installer.exec(cmd=("--version",), quiet=True).returncode == 0
            assert (lib_dir / "env" / "bin" / "npm").is_symlink()
            assert (lib_dir / "env" / "bin" / "node").is_symlink()
            assert (lib_dir / "env" / "bin" / "npm").resolve() == npm_binary.resolve()
            assert (lib_dir / "env" / "bin" / "node").resolve() == node_binary.resolve()
            assert not (lib_dir / "bin").exists()

    def test_host_pnpm_and_node_are_projected_before_execution(
        self,
        test_machine,
    ):
        test_machine.require_tool("node")

        with tempfile.TemporaryDirectory() as temp_dir:
            lib_dir = Path(temp_dir) / "lib"
            provider = PnpmProvider(
                install_root=lib_dir / "pnpm",
                postinstall_scripts=True,
                min_release_age=0,
            )
            pnpm_binary = provider.get_abspath("pnpm", quiet=True, no_cache=True)
            assert pnpm_binary is not None
            old_pnpm_binary = os.environ.get("PNPM_BINARY")
            os.environ["PNPM_BINARY"] = str(pnpm_binary)

            try:
                installer = provider.INSTALLER_BINARY(no_cache=True)
                assert installer.loaded_abspath is not None
                version = provider.exec(
                    bin_name=installer.loaded_abspath,
                    cmd=("--version",),
                    quiet=True,
                )
            finally:
                if old_pnpm_binary is None:
                    os.environ.pop("PNPM_BINARY", None)
                else:
                    os.environ["PNPM_BINARY"] = old_pnpm_binary

            projected_pnpm = lib_dir / "env" / "bin" / "pnpm"
            projected_node = lib_dir / "env" / "bin" / "node"
            assert installer.loaded_abspath == projected_pnpm
            assert projected_pnpm.is_symlink()
            assert projected_node.is_symlink()
            assert projected_pnpm.resolve() == Path(pnpm_binary).resolve()
            assert version.returncode == 0, version.stderr
            assert not (lib_dir / "bin").exists()

    def test_self_bootstrap_uses_host_npm_when_top_level_provider_excludes_env(
        self,
        test_machine,
    ):
        npm_binary = test_machine.require_tool("npm")
        test_machine.require_tool("node")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "pnpm-root"
            old_path = os.environ.get("PATH", "")
            old_binproviders = os.environ.get("ABXPKG_BINPROVIDERS")
            old_npm_binary = os.environ.get("NPM_BINARY")
            os.environ["PATH"] = "/usr/bin:/bin"
            os.environ["ABXPKG_BINPROVIDERS"] = "playwright"
            os.environ["NPM_BINARY"] = npm_binary
            try:
                provider = PnpmProvider(
                    install_root=install_root,
                    postinstall_scripts=True,
                    min_release_age=3,
                )

                installer = provider.INSTALLER_BINARY(no_cache=True)
                installed = provider.install("zx")
            finally:
                os.environ["PATH"] = old_path
                if old_binproviders is None:
                    os.environ.pop("ABXPKG_BINPROVIDERS", None)
                else:
                    os.environ["ABXPKG_BINPROVIDERS"] = old_binproviders
                if old_npm_binary is None:
                    os.environ.pop("NPM_BINARY", None)
                else:
                    os.environ["NPM_BINARY"] = old_npm_binary

            assert installer.loaded_abspath is not None
            assert installer.loaded_abspath.is_relative_to(install_root / "npm")
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert (
                installed.loaded_abspath
                == install_root / "node_modules" / ".bin" / "zx"
            )

    def test_install_args_win_for_ignore_scripts_and_min_release_age(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pnpm_prefix = Path(temp_dir) / "pnpm"
            provider = PnpmProvider(
                install_root=pnpm_prefix,
                postinstall_scripts=True,
                min_release_age=36500,
            ).get_provider_with_overrides(
                overrides={
                    "gifsicle": {
                        "install_args": [
                            "gifsicle",
                            "--ignore-scripts",
                            "--config.minimumReleaseAge=0",
                        ],
                    },
                },
            )

            installed = provider.install("gifsicle")

            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.exists()
            # The wrapper exists but the postinstall download was skipped via
            # explicit --ignore-scripts, so the vendored binary is missing.
            proc = installed.exec(cmd=("--version",), quiet=True)
            assert proc.returncode != 0
            # The provider's strict 100-year min_release_age was overridden
            # by the explicit --config.minimumReleaseAge=0 in install_args,
            # so the resolver was able to pick a real version.
            assert (pnpm_prefix / "node_modules" / "gifsicle" / "package.json").exists()
            # And the lockfile / package.json side effects must exist.
            assert (pnpm_prefix / "package.json").exists()
            assert (pnpm_prefix / "pnpm-lock.yaml").exists()

    def test_install_root_alias_installs_into_the_requested_prefix(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "pnpm-root"
            provider = PnpmProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            installed = provider.install("zx")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            bin_dir = provider.bin_dir
            assert bin_dir is not None
            assert provider.install_root == install_root
            assert bin_dir == install_root / "node_modules" / ".bin"
            assert bin_dir.exists()
            assert installed.loaded_abspath == bin_dir / "zx"
            assert installed.loaded_abspath.parent == bin_dir
            # Real on-disk pnpm install side effects.
            assert (install_root / "node_modules" / "zx" / "package.json").exists()
            assert (install_root / "package.json").exists()
            assert (install_root / "pnpm-lock.yaml").exists()
            # The pnpm content-addressable store should also have been
            # populated under cache_dir.
            store_root = provider.cache_dir
            assert store_root.exists()

    def test_managed_package_root_keeps_store_and_installer_under_same_lib(
        self,
        tmp_path,
        test_machine,
    ):
        lib_dir = tmp_path / "abx-lib"
        install_root = lib_dir / "pnpm" / "packages" / "zx"
        provider = PnpmProvider(
            install_root=install_root,
            postinstall_scripts=True,
            min_release_age=3,
        )

        assert provider.cache_dir == lib_dir / "cache" / "pnpm"
        assert (
            provider._installer_provider_root() == lib_dir / "npm" / "packages" / "pnpm"
        )

        installed = provider.install("zx")

        test_machine.assert_shallow_binary_loaded(installed)
        assert installed is not None
        assert installed.loaded_abspath is not None
        assert installed.loaded_abspath == install_root / "node_modules" / ".bin" / "zx"
        assert (install_root / "node_modules" / "zx" / "package.json").exists()
        modules_yaml = install_root / "node_modules" / ".modules.yaml"
        assert modules_yaml.exists()
        assert str(provider.cache_dir) in modules_yaml.read_text()
        proc = installed.exec(cmd=("--version",), quiet=True)
        assert proc.returncode == 0, proc.stderr

    def test_explicit_prefix_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = PnpmProvider(
                install_root=temp_dir_path / "ambient-pnpm",
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={"zx": {"install_args": ["zx@7.2.3"]}},
            )
            ambient_installed = ambient_provider.install(
                "zx",
                min_version=SemVer("1.0.0"),
            )
            assert ambient_installed is not None
            assert ambient_installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath.parent == ambient_provider.bin_dir

            install_root = temp_dir_path / "pnpm-root"
            provider = PnpmProvider(
                PATH=str(ambient_provider.bin_dir),
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=3,
            )

            installed = provider.install("zx", min_version=SemVer("8.8.0"))

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            bin_dir = provider.bin_dir
            assert bin_dir is not None
            assert provider.install_root == install_root
            assert bin_dir == install_root / "node_modules" / ".bin"
            assert bin_dir.exists()
            assert installed.loaded_abspath == bin_dir / "zx"
            assert installed.loaded_abspath.parent == bin_dir
            # The two installs must have produced two different on-disk binaries.
            assert installed.loaded_abspath != ambient_installed.loaded_abspath
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_setup_falls_back_to_temp_store_when_cache_dir_is_not_a_directory(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            provider = PnpmProvider(
                install_root=tmp_path / "pnpm",
                postinstall_scripts=True,
                min_release_age=3,
            )

            installed = provider.install("zx")
            assert provider.cache_dir.is_dir()
            test_machine.assert_shallow_binary_loaded(installed)
            assert (tmp_path / "pnpm" / "node_modules" / "zx" / "package.json").exists()

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PnpmProvider(
                install_root=Path(temp_dir) / "pnpm",
                postinstall_scripts=True,
                min_release_age=3,
            )
            installed, _ = test_machine.exercise_provider_lifecycle(
                provider,
                bin_name="zx",
            )
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.is_relative_to(provider.install_root)

    def test_scoped_package_resolves_all_pnpm_exposed_clis(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PnpmProvider(
                install_root=Path(temp_dir) / "pnpm",
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={"lit": {"install_args": ["@llamaindex/liteparse"]}},
            )

            installed = provider.install("lit")

            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_version is not None
            assert provider.bin_dir is not None
            assert provider.install_root is not None
            assert (
                installed.loaded_abspath.resolve()
                == (provider.bin_dir / "lit").resolve()
            )
            assert (provider.bin_dir / "lit").is_file()
            assert (provider.bin_dir / "liteparse").is_file()
            package_json = (
                provider.install_root
                / "node_modules"
                / "@llamaindex"
                / "liteparse"
                / "package.json"
            )
            assert package_json.exists()
            import json as _json

            assert (
                str(installed.loaded_version)
                == _json.loads(package_json.read_text())["version"]
            )
            exposed_bins = provider._available_cli_paths()
            assert set(exposed_bins) >= {"lit", "liteparse"}
            assert exposed_bins["lit"].resolve() == (provider.bin_dir / "lit").resolve()
            assert (
                exposed_bins["liteparse"].resolve()
                == (provider.bin_dir / "liteparse").resolve()
            )

            lit_proc = installed.exec(cmd=("--version",), quiet=True)
            assert lit_proc.returncode == 0, lit_proc.stderr
            assert "2." in (lit_proc.stdout + lit_proc.stderr)

            liteparse = provider.load("liteparse")
            test_machine.assert_shallow_binary_loaded(
                liteparse,
                assert_version_command=False,
            )
            assert liteparse is not None
            assert liteparse.loaded_abspath is not None
            assert (
                liteparse.loaded_abspath.resolve()
                == (provider.bin_dir / "liteparse").resolve()
            )
            liteparse_proc = liteparse.exec(cmd=("--version",), quiet=True)
            assert liteparse_proc.returncode == 0, liteparse_proc.stderr
            assert "2." in (liteparse_proc.stdout + liteparse_proc.stderr)

    def test_global_mode_resolves_pnpm_global_bin_dir_and_exposed_clis(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            pnpm_home = (Path(temp_dir) / "pnpm-home").resolve()
            previous_home = os.environ.get("PNPM_HOME")
            previous_path = os.environ.get("PATH", "")
            os.environ["PNPM_HOME"] = str(pnpm_home)
            os.environ["PATH"] = os.pathsep.join([str(pnpm_home), previous_path])
            try:
                provider = PnpmProvider(
                    install_root=None,
                    postinstall_scripts=True,
                    min_release_age=3,
                ).get_provider_with_overrides(
                    overrides={"lit": {"install_args": ["@llamaindex/liteparse"]}},
                )

                installed = provider.install("lit", no_cache=True)

                test_machine.assert_shallow_binary_loaded(
                    installed,
                    assert_version_command=False,
                )
                assert installed is not None
                provided_bin_dir = provider._provided_bin_dir(no_cache=True)
                assert provided_bin_dir is not None
                assert provided_bin_dir.resolve() == pnpm_home.resolve()
                exposed_bins = provider._available_cli_paths(no_cache=True)
                assert set(exposed_bins) >= {"lit", "liteparse"}
                assert exposed_bins["lit"].resolve().is_relative_to(pnpm_home)
                assert exposed_bins["liteparse"].resolve().is_relative_to(pnpm_home)

                liteparse = provider.load("liteparse", no_cache=True)
                test_machine.assert_shallow_binary_loaded(
                    liteparse,
                    assert_version_command=False,
                )
                assert liteparse is not None
                assert liteparse.loaded_abspath is not None
                assert liteparse.loaded_abspath.resolve().is_relative_to(pnpm_home)
                assert provider.uninstall("lit", no_cache=True) is True
            finally:
                if previous_home is None:
                    os.environ.pop("PNPM_HOME", None)
                else:
                    os.environ["PNPM_HOME"] = previous_home
                os.environ["PATH"] = previous_path

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            pnpm_prefix = Path(tmpdir) / "pnpm"
            old_provider = PnpmProvider(
                install_root=pnpm_prefix,
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={"zx": {"install_args": ["zx@7.2.3"]}},
            )
            old_installed = old_provider.install("zx", min_version=SemVer("1.0.0"))
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("7.2.3")

            upgraded = PnpmProvider(
                install_root=pnpm_prefix,
                postinstall_scripts=True,
                min_release_age=3,
            ).install("zx", min_version=SemVer("8.8.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("8.8.0"),
            )
            assert upgraded is not None
            assert upgraded.loaded_abspath is not None
            assert upgraded.loaded_version is not None
            assert old_installed.loaded_version is not None
            # The new install replaced the old one in the same prefix.
            assert upgraded.loaded_abspath == old_installed.loaded_abspath
            assert upgraded.loaded_version > old_installed.loaded_version
            installed_pkg = pnpm_prefix / "node_modules" / "zx" / "package.json"
            assert installed_pkg.exists()
            import json as _json

            assert _json.loads(installed_pkg.read_text())["version"] == str(
                upgraded.loaded_version,
            )

            # update() with an unreachable min_version must surface a real error.
            with pytest.raises(Exception):
                PnpmProvider(
                    install_root=pnpm_prefix,
                    postinstall_scripts=True,
                    min_release_age=3,
                ).update("zx", min_version=SemVer("999.0.0"))

    def test_provider_defaults_and_binary_overrides_enforce_min_release_age(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = PnpmProvider(
                install_root=Path(tmpdir) / "strict-pnpm",
                postinstall_scripts=True,
                min_release_age=36500,
            )
            assert strict_provider.supports_min_release_age("install") is True

            with pytest.raises(BinProviderInstallError):
                strict_provider.install("zx")
            test_machine.assert_provider_missing(strict_provider, "zx")

            direct_override = strict_provider.install("zx", min_release_age=3)
            test_machine.assert_shallow_binary_loaded(direct_override)
            assert strict_provider.uninstall("zx", min_release_age=3)

            binary = Binary(
                name="zx",
                binproviders=[
                    PnpmProvider(
                        install_root=Path(tmpdir) / "binary-pnpm",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=3,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

    def test_provider_defaults_and_binary_overrides_enforce_postinstall_scripts(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = PnpmProvider(
                install_root=Path(tmpdir) / "strict-pnpm",
                postinstall_scripts=False,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={"optipng": {"install_args": ["optipng-bin"]}},
            )
            assert strict_provider.supports_postinstall_disable("install") is True

            strict_installed = strict_provider.install("optipng")
            assert strict_installed is not None
            assert strict_installed.loaded_abspath is not None
            assert strict_installed.loaded_abspath.exists()
            strict_proc = strict_installed.exec(cmd=("--version",), quiet=True)
            assert strict_proc.returncode != 0, (
                f"strict optipng install with postinstall_scripts=False should "
                f"have left the binary broken (no vendor download), but exec "
                f"returned {strict_proc.returncode}"
            )

            # Use a fresh prefix to verify postinstall_scripts=True actually
            # runs the postinstall hook end-to-end. (Reinstalling into the
            # same prefix would hit pnpm's content-addressable store, which
            # caches the package without the vendor binaries from the
            # previous --ignore-scripts run.)
            override_provider = PnpmProvider(
                install_root=Path(tmpdir) / "override-pnpm",
                postinstall_scripts=False,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={"optipng": {"install_args": ["optipng-bin"]}},
            )
            direct_override = override_provider.install(
                "optipng",
                postinstall_scripts=True,
            )
            assert direct_override is not None
            assert direct_override.loaded_abspath is not None
            override_proc = direct_override.exec(cmd=("--version",), quiet=True)
            assert override_proc.returncode == 0, (
                f"postinstall_scripts=True override should produce a working "
                f"binary, but exec returned {override_proc.returncode}: "
                f"stdout={override_proc.stdout!r} stderr={override_proc.stderr!r}"
            )
            assert override_provider.uninstall("optipng", postinstall_scripts=True)

            binary = Binary(
                name="optipng",
                binproviders=[
                    PnpmProvider(
                        install_root=Path(tmpdir) / "binary-pnpm",
                        postinstall_scripts=False,
                        min_release_age=3,
                    ).get_provider_with_overrides(
                        overrides={"optipng": {"install_args": ["optipng-bin"]}},
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=3,
            )
            installed = binary.install()
            assert installed is not None
            assert installed.loaded_abspath is not None
            installed_proc = installed.exec(cmd=("--version",), quiet=True)
            assert installed_proc.returncode == 0

            failing_binary = Binary(
                name="optipng",
                binproviders=[
                    PnpmProvider(
                        install_root=Path(tmpdir) / "failing-pnpm",
                        postinstall_scripts=False,
                        min_release_age=3,
                    ).get_provider_with_overrides(
                        overrides={"optipng": {"install_args": ["optipng-bin"]}},
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=3,
            )
            failing_installed = failing_binary.install()
            assert failing_installed is not None
            failing_proc = failing_installed.exec(cmd=("--version",), quiet=True)
            assert failing_proc.returncode != 0

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="zx",
                binproviders=[
                    PnpmProvider(
                        install_root=Path(temp_dir) / "pnpm",
                        postinstall_scripts=True,
                        min_release_age=3,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=3,
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_zx(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PnpmProvider(
                install_root=Path(temp_dir) / "pnpm",
                postinstall_scripts=True,
                min_release_age=3,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="zx")
            # dry_run must not have actually installed anything.
            modules_dir = Path(temp_dir) / "pnpm" / "node_modules"
            if modules_dir.exists():
                assert not (modules_dir / "zx").exists()

    def test_provider_action_args_override_provider_defaults(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PnpmProvider(
                install_root=Path(temp_dir) / "pnpm",
                dry_run=True,
                postinstall_scripts=False,
                min_release_age=36500,
            )

            installed = provider.install(
                "zx",
                dry_run=False,
                postinstall_scripts=True,
                min_release_age=3,
            )
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_global_install_uses_pnpm_home(self, test_machine):
        # Hermetic global install: point PNPM_HOME at a temp dir so we can
        # verify the global install side effects without polluting $HOME.
        with tempfile.TemporaryDirectory() as temp_dir:
            # ``.resolve()`` so macOS's /var/folders tempdirs (which resolve
            # through /private) compare equal to the paths pnpm produces.
            pnpm_home = (Path(temp_dir) / "pnpm-home").resolve()
            previous = os.environ.get("PNPM_HOME")
            os.environ["PNPM_HOME"] = str(pnpm_home)
            try:
                provider = PnpmProvider(
                    install_root=None,  # global mode
                    postinstall_scripts=True,
                    min_release_age=3,
                )
                installed = provider.install("zx", no_cache=True)
                test_machine.assert_shallow_binary_loaded(installed)
                assert installed is not None
                assert installed.loaded_abspath is not None
                # The shim must end up under PNPM_HOME, not the user's $HOME.
                assert installed.loaded_abspath.resolve().is_relative_to(pnpm_home)
                # Real on-disk side effect: pnpm's global package manifest exists.
                assert (pnpm_home / "global").exists()
                assert provider.uninstall("zx", no_cache=True) is True
                assert provider.load("zx", quiet=True, no_cache=True) is None
            finally:
                if previous is None:
                    os.environ.pop("PNPM_HOME", None)
                else:
                    os.environ["PNPM_HOME"] = previous

    def test_no_cache_install_does_not_create_managed_store(self, test_machine):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            lib_dir = Path(temp_dir) / "lib"
            install_root = lib_dir / "pnpm" / "packages" / "zx"
            previous_lib_dir = os.environ.get("ABXPKG_LIB_DIR")
            previous_xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
            os.environ["ABXPKG_LIB_DIR"] = str(lib_dir)
            os.environ["XDG_CACHE_HOME"] = str(lib_dir / "cache")
            try:
                provider = PnpmProvider(
                    install_root=install_root,
                    postinstall_scripts=True,
                    min_release_age=3,
                )
                installed = provider.install("zx", no_cache=True)
            finally:
                if previous_lib_dir is None:
                    os.environ.pop("ABXPKG_LIB_DIR", None)
                else:
                    os.environ["ABXPKG_LIB_DIR"] = previous_lib_dir
                if previous_xdg_cache_home is None:
                    os.environ.pop("XDG_CACHE_HOME", None)
                else:
                    os.environ["XDG_CACHE_HOME"] = previous_xdg_cache_home

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.resolve().is_relative_to(
                install_root.resolve(),
            )
            assert not (lib_dir / "cache" / "pnpm").exists()

    def test_min_release_age_pins_to_older_version_when_strict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = PnpmProvider(
                install_root=Path(tmpdir) / "pnpm",
                postinstall_scripts=True,
                min_release_age=365,
            )
            assert strict_provider.supports_min_release_age("install") is True
            installed = strict_provider.install("zx")
            assert installed is not None
            assert installed.loaded_version is not None
            ceiling = SemVer.parse("8.8.0")
            assert ceiling is not None
            # zx 8.8.x was published too recently to clear a 365-day gate.
            assert installed.loaded_version < ceiling

    def test_supports_methods_do_not_emit_unsupported_warnings(self, caplog):
        # Sanity check: when the provider IS supported on this host (which
        # it always is for pnpm 10+), no "ignoring unsupported" warnings
        # should be emitted at install/update/uninstall time.
        with tempfile.TemporaryDirectory() as tmpdir:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                provider = PnpmProvider(
                    install_root=Path(tmpdir) / "pnpm",
                    postinstall_scripts=False,
                    min_release_age=3,
                )
                installed = provider.install("zx")
                assert installed is not None
            assert "ignoring unsupported postinstall_scripts" not in caplog.text
            assert "ignoring unsupported min_release_age" not in caplog.text

    def test_binary_install_failure_propagates_as_BinaryInstallError(self):
        # Strict 100-year release age + no override forces a real install
        # failure, which the Binary layer must surface as BinaryInstallError.
        with tempfile.TemporaryDirectory() as tmpdir:
            failing_binary = Binary(
                name="zx",
                binproviders=[
                    PnpmProvider(
                        install_root=Path(tmpdir) / "pnpm",
                        postinstall_scripts=True,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=36500,
            )
            failing_provider = failing_binary.binproviders[0]
            assert isinstance(failing_provider, PnpmProvider)
            assert failing_provider.supports_min_release_age("install") is True
            with pytest.raises(BinaryInstallError):
                failing_binary.install()

    def test_search_finds_real_npm_package_and_install_works(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PnpmProvider(
                install_root=Path(temp_dir) / "pnpm",
                postinstall_scripts=True,
                min_release_age=3,
            )
            results = provider.search("zx")
            assert results, "pnpm search zx should return registry matches"
            names = [r.name for r in results]
            assert "zx" in names
            match = next(r for r in results if r.name == "zx")
            assert match.overrides == {"pnpm": {"install_args": ["zx"]}}
            assert match.loaded_abspath is None
            assert match.loaded_version is None
            installed = match.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed.name == "zx"

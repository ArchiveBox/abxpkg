import os
import subprocess
import tempfile
from pathlib import Path
import sys

import pytest

from abxpkg import (
    BinName,
    BinProvider,
    BinProviderName,
    BunProvider,
    DenoProvider,
    EnvProvider,
    NpmProvider,
    PipProvider,
    PnpmProvider,
    SemVer,
    UvProvider,
    YarnProvider,
)


class TestBinProvider:
    def test_default_env_path_keeps_ambient_and_standard_package_dirs(
        self,
        tmp_path,
    ):
        ambient_bin = tmp_path / "ambient-bin"
        ambient_bin.mkdir()
        env = {
            **os.environ,
            "PATH": str(ambient_bin),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        }

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from abxpkg.binprovider import DEFAULT_ENV_PATH\n"
                    "from abxpkg.binprovider_ansible import AnsibleProvider\n"
                    "from abxpkg.binprovider_pyinfra import PyinfraProvider\n"
                    "paths = DEFAULT_ENV_PATH.split(':')\n"
                    f"assert {str(ambient_bin)!r} in paths\n"
                    "assert str(AnsibleProvider().PATH) == DEFAULT_ENV_PATH\n"
                    "assert str(PyinfraProvider().PATH) == DEFAULT_ENV_PATH\n"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 0, proc.stderr

    @pytest.mark.parametrize(
        ("provider_cls", "installer_bin"),
        (
            (PipProvider, "pip"),
            (NpmProvider, "npm"),
            (PnpmProvider, "pnpm"),
            (UvProvider, "uv"),
            (YarnProvider, "yarn"),
        ),
    )
    def test_provider_init_is_lazy_until_setup(
        self,
        test_machine,
        provider_cls,
        installer_bin,
    ):
        test_machine.require_tool(installer_bin)

        with tempfile.TemporaryDirectory() as tmpdir:
            provider = provider_cls(
                install_root=Path(tmpdir) / "install-root",
                euid=None,
                postinstall_scripts=True,
                min_release_age=0,
            )

            assert provider.euid is None
            assert not (Path(tmpdir) / "install-root").exists()

            provider.setup(no_cache=True)

        assert provider.euid is not None

    @pytest.mark.parametrize(
        ("provider_cls", "installer_bin"),
        (
            (PipProvider, "pip"),
            (NpmProvider, "npm"),
            (PnpmProvider, "pnpm"),
            (UvProvider, "uv"),
            (YarnProvider, "yarn"),
        ),
    )
    def test_installer_binary_abspath_resolves_without_recursing(
        self,
        test_machine,
        provider_cls,
        installer_bin,
    ):
        test_machine.require_tool(installer_bin)
        provider = provider_cls(postinstall_scripts=True, min_release_age=0)

        abspath = provider.get_abspath(installer_bin, quiet=True, no_cache=True)
        installer = provider.INSTALLER_BINARY(no_cache=True)

        assert abspath is not None
        assert installer.loaded_abspath is not None
        assert installer.name == installer_bin
        assert installer.loaded_version is not None

    def test_installer_binary_auto_installs_missing_dependency_into_configured_lib(
        self,
        monkeypatch,
    ):
        class BlackInstallerProvider(BinProvider):
            name: BinProviderName = "black_bootstrap"
            INSTALLER_BIN: BinName = "black"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            monkeypatch.setenv("ABXPKG_BINPROVIDERS", "pip")
            monkeypatch.setenv("ABXPKG_LIB_DIR", str(tmpdir_path / "abxlib"))

            installer = BlackInstallerProvider(
                postinstall_scripts=True,
                min_release_age=0,
            ).INSTALLER_BINARY(no_cache=True)

            assert installer.loaded_binprovider is not None
            assert installer.loaded_binprovider.name == "pip"
            assert installer.loaded_abspath is not None
            assert installer.loaded_abspath.resolve().is_relative_to(
                (tmpdir_path / "abxlib" / "pip" / "venv" / "bin").resolve(),
            )
            assert installer.loaded_version is not None

    def test_base_public_getters_resolve_real_host_python(self, test_machine):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=0)

        assert provider.get_install_args("python") == ("python",)
        assert provider.get_packages("python") == ("python",)
        loaded_python = provider.load("python")
        assert loaded_python is not None
        assert provider.get_abspath("python") == loaded_python.loaded_abspath
        assert provider.get_version("python") == SemVer.parse(
            "{}.{}.{}".format(*sys.version_info[:3]),
        )
        assert provider.get_sha256("python") == loaded_python.loaded_sha256

        loaded_or_installed = provider.install(
            "python",
            min_version=SemVer("3.0.0"),
        )
        test_machine.assert_shallow_binary_loaded(loaded_or_installed)

    def test_provider_ENV_includes_runtime_and_installer_context(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            pip_provider = PipProvider(
                install_root=tmpdir_path / "pip",
                postinstall_scripts=True,
                min_release_age=0,
            )
            uv_provider = UvProvider(
                install_root=tmpdir_path / "uv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            monkeypatch.setenv("UV_TOOL_DIR", str(tmpdir_path / "uv-tools"))
            uv_tool_provider = UvProvider(
                install_root=None,
                bin_dir=tmpdir_path / "uv-bin",
                postinstall_scripts=True,
                min_release_age=0,
            )
            pnpm_provider = PnpmProvider(
                install_root=tmpdir_path / "pnpm",
                postinstall_scripts=True,
                min_release_age=0,
            )
            yarn_provider = YarnProvider(
                install_root=tmpdir_path / "yarn",
                postinstall_scripts=True,
                min_release_age=0,
            )
            bun_provider = BunProvider(
                install_root=tmpdir_path / "bun",
                postinstall_scripts=True,
                min_release_age=0,
            )
            deno_provider = DenoProvider(
                install_root=tmpdir_path / "deno",
                postinstall_scripts=True,
                min_release_age=0,
            )

            assert {"VIRTUAL_ENV"} <= set(pip_provider.ENV)
            assert {"VIRTUAL_ENV", "UV_CACHE_DIR"} <= set(uv_provider.ENV)
            assert {"UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR"} <= set(
                uv_tool_provider.ENV,
            )
            assert {"PNPM_HOME", "NODE_MODULES_DIR", "NODE_PATH"} <= set(
                pnpm_provider.ENV,
            )
            assert {
                "YARN_GLOBAL_FOLDER",
                "YARN_CACHE_FOLDER",
                "NODE_MODULES_DIR",
            } <= set(yarn_provider.ENV)
            assert {"BUN_INSTALL", "NODE_MODULES_DIR", "NODE_PATH"} <= set(
                bun_provider.ENV,
            )
            assert {"DENO_INSTALL_ROOT", "DENO_DIR", "DENO_TLS_CA_STORE"} <= set(
                deno_provider.ENV,
            )

    def test_build_exec_env_merges_materialized_runtime_module_paths(
        self,
        tmp_path,
    ):
        base_node_modules = tmp_path / "base" / "node_modules"
        base_site_packages = tmp_path / "base" / "site-packages"
        first_site_packages = (
            tmp_path
            / "pip"
            / "first"
            / "venv"
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        second_site_packages = (
            tmp_path
            / "pip"
            / "second"
            / "venv"
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        first_site_packages.mkdir(parents=True)
        second_site_packages.mkdir(parents=True)

        first_pnpm = PnpmProvider(
            install_root=tmp_path / "pnpm" / "packages" / "singlefile",
            postinstall_scripts=True,
            min_release_age=0,
        )
        second_pnpm = PnpmProvider(
            install_root=tmp_path / "pnpm" / "packages" / "chrome",
            postinstall_scripts=True,
            min_release_age=0,
        )
        first_pip = PipProvider(
            install_root=tmp_path / "pip" / "first",
            postinstall_scripts=True,
            min_release_age=0,
        )
        second_pip = PipProvider(
            install_root=tmp_path / "pip" / "second",
            postinstall_scripts=True,
            min_release_age=0,
        )

        first_event_env = BinProvider.build_exec_env(
            providers=[first_pnpm, first_pip],
            base_env={},
        )
        second_event_env = BinProvider.build_exec_env(
            providers=[second_pnpm, second_pip],
            base_env={},
        )
        env = BinProvider.build_exec_env(
            base_env={
                "PATH": os.environ["PATH"],
                "NODE_PATH": str(base_node_modules),
                "PYTHONPATH": str(base_site_packages),
            },
            extra_env=first_event_env,
        )
        env = BinProvider.build_exec_env(base_env=env, extra_env=second_event_env)

        assert first_pnpm.install_root is not None
        assert second_pnpm.install_root is not None
        assert env["NODE_PATH"].split(":") == [
            str(base_node_modules),
            str(first_pnpm.install_root / "node_modules"),
            str(second_pnpm.install_root / "node_modules"),
        ]
        assert env["PYTHONPATH"].split(":") == [
            str(base_site_packages),
            str(first_site_packages),
            str(second_site_packages),
        ]

    def test_build_exec_env_keeps_provider_path_before_ambient_path(
        self,
        tmp_path,
    ):
        provider_bin = tmp_path / "provider" / "bin"
        ambient_bin = tmp_path / "ambient" / "bin"
        extra_prepend_bin = tmp_path / "extra-prepend" / "bin"
        extra_append_bin = tmp_path / "extra-append" / "bin"
        for path in (provider_bin, ambient_bin, extra_prepend_bin, extra_append_bin):
            path.mkdir(parents=True)

        provider = EnvProvider(PATH=str(provider_bin), install_root=None)
        prepend_env = BinProvider.build_exec_env(
            providers=[provider],
            base_env={"PATH": str(ambient_bin)},
            extra_env={"PATH": f"{extra_prepend_bin}{os.pathsep}"},
        )
        append_env = BinProvider.build_exec_env(
            providers=[provider],
            base_env={"PATH": str(ambient_bin)},
            extra_env={"PATH": f"{os.pathsep}{extra_append_bin}"},
        )

        assert prepend_env["PATH"].split(os.pathsep) == [
            str(provider_bin),
            str(extra_prepend_bin),
            str(ambient_bin),
        ]
        assert append_env["PATH"].split(os.pathsep) == [
            str(provider_bin),
            str(ambient_bin),
            str(extra_append_bin),
        ]

    def test_get_provider_with_overrides_changes_real_install_behavior(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_provider = PipProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            overridden = base_provider.get_provider_with_overrides(
                overrides={"black": {"install_args": ["black==23.1.0"]}},
            )

            assert base_provider.get_install_args("black") == ("black",)
            assert overridden.get_install_args("black") == ("black==23.1.0",)

            installed = overridden.install("black")
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_version == SemVer("23.1.0")

    def test_exec_uses_provider_PATH_for_nested_subprocesses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PipProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = provider.install("black")
            assert installed is not None

            proc = provider.exec(
                sys.executable,
                cmd=[
                    "-c",
                    "import subprocess, sys; proc = subprocess.run(['black', '--version'], capture_output=True, text=True); sys.stdout.write((proc.stdout or proc.stderr).strip()); sys.exit(proc.returncode)",
                ],
                quiet=True,
            )

            assert proc.returncode == 0, proc.stderr or proc.stdout
            assert installed.loaded_version is not None
            assert str(installed.loaded_version) in proc.stdout

    def test_shallow_binary_exec_uses_loaded_provider_runtime_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PipProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = provider.install("black")
            assert installed is not None

            proc = installed.exec(
                sys.executable,
                cmd=[
                    "-c",
                    "import subprocess, sys; proc = subprocess.run(['black', '--version'], capture_output=True, text=True); sys.stdout.write((proc.stdout or proc.stderr).strip()); sys.exit(proc.returncode)",
                ],
                quiet=True,
            )

            assert proc.returncode == 0, proc.stderr or proc.stdout
            assert installed.loaded_version is not None
            assert str(installed.loaded_version) in proc.stdout

    def test_exec_prefers_provider_PATH_over_explicit_env_PATH(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            ambient_provider = PipProvider(
                install_root=tmpdir_path / "ambient-venv",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"black": {"install_args": ["black==23.1.0"]}},
            )
            ambient_installed = ambient_provider.install(
                "black",
                min_version=SemVer("1.0.0"),
            )
            assert ambient_installed is not None

            provider = PipProvider(
                install_root=tmpdir_path / "provider-venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = provider.install("black", min_version=SemVer("24.0.0"))
            assert installed is not None
            proc = provider.exec(
                sys.executable,
                cmd=[
                    "-c",
                    "import subprocess, sys; proc = subprocess.run(['black', '--version'], capture_output=True, text=True); sys.stdout.write((proc.stdout or proc.stderr).strip()); sys.exit(proc.returncode)",
                ],
                env={"PATH": str(ambient_provider.bin_dir)},
                quiet=True,
            )

            assert proc.returncode == 0, proc.stderr or proc.stdout
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version is not None
            assert str(installed.loaded_version) in proc.stdout
            assert str(ambient_installed.loaded_version) not in proc.stdout

    def test_exec_timeout_is_enforced_for_real_commands(self):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=0)

        with pytest.raises(subprocess.TimeoutExpired):
            provider.exec(
                sys.executable,
                cmd=["-c", "import time; time.sleep(5)"],
                timeout=2,
                quiet=True,
            )

    def test_docs_url_returns_provider_specific_link_for_unloaded_binary(self):
        from abxpkg import Binary, ShallowBinary

        assert (
            PipProvider().get_docs_url("pip_search")
            == "https://pypi.org/project/pip_search"
        )
        assert UvProvider().get_docs_url("black") == "https://pypi.org/project/black"
        assert (
            NpmProvider().get_docs_url("@puppeteer/browsers")
            == "https://www.npmjs.com/package/@puppeteer/browsers"
        )
        assert EnvProvider().get_docs_url("python") is None

        # Binary that's not yet loaded falls back across binproviders.
        binary = Binary(name="pip_search", binproviders=[EnvProvider(), PipProvider()])
        assert binary.docs_url() == "https://pypi.org/project/pip_search"

        # Loaded ShallowBinary uses the provider it was loaded from.
        loaded = ShallowBinary.model_validate(
            {
                "name": "pip_search",
                "binprovider": PipProvider(),
                "version": SemVer.parse("3.2.1"),
                "abspath": Path(sys.executable),
            },
        )
        assert loaded.docs_url() == "https://pypi.org/project/pip_search"

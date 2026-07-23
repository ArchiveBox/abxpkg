import os
import tempfile
from pathlib import Path
import logging
from typing import cast

import pytest

from abxpkg import Binary, GoGetProvider, SemVer


class TestGoGetProvider:
    def test_installer_binary_uses_go_version_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lib_dir = (Path(temp_dir) / "lib").resolve()
            previous_lib_dir = os.environ.get("ABXPKG_LIB_DIR")
            os.environ["ABXPKG_LIB_DIR"] = str(lib_dir)
            try:
                bootstrap_provider = GoGetProvider(
                    postinstall_scripts=True,
                    min_release_age=3,
                )
                bootstrapped = bootstrap_provider.INSTALLER_BINARY(no_cache=True)
                assert bootstrapped.loaded_abspath is not None

                # Re-resolve after bootstrap so an installer supplied by Apt or
                # Homebrew is discovered as a host binary and projected through
                # the stable managed env path before GoGet executes it.
                provider = GoGetProvider(postinstall_scripts=True, min_release_age=3)
                installer = provider.INSTALLER_BINARY(no_cache=True)
            finally:
                if previous_lib_dir is None:
                    os.environ.pop("ABXPKG_LIB_DIR", None)
                else:
                    os.environ["ABXPKG_LIB_DIR"] = previous_lib_dir

            assert installer.loaded_abspath == lib_dir / "env" / "bin" / "go"
            assert installer.loaded_abspath.is_symlink()
            assert installer.loaded_version is not None
            assert installer.loaded_version >= SemVer("1.0.0")

    def test_default_install_args_fail_closed_for_bare_binary_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GoGetProvider.model_validate(
                {
                    "install_root": Path(temp_dir) / "go-root",
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            with pytest.raises(ValueError):
                provider.get_install_args("shfmt", quiet=False)

    def test_module_path_name_installs_without_overrides(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            module_path = "mvdan.cc/sh/v3/cmd/shfmt"
            provider = GoGetProvider.model_validate(
                {
                    "install_root": Path(temp_dir) / "go-root",
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            )

            installed = provider.install(module_path)

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.name == "shfmt"
            assert provider.load(module_path, quiet=True, no_cache=True) is not None
            assert provider.uninstall(module_path)
            assert provider.load(module_path, quiet=True, no_cache=True) is None

    def test_install_root_and_bin_dir_aliases_install_into_the_requested_paths(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "go-root"
            bin_dir = Path(temp_dir) / "custom-bin"
            provider = GoGetProvider.model_validate(
                {
                    "install_root": install_root,
                    "bin_dir": bin_dir,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )

            installed = provider.install("shfmt")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_install_root_without_explicit_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = GoGetProvider.model_validate(
                {
                    "install_root": temp_dir_path / "ambient-go",
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@v3.7.0"],
                    },
                },
            )
            ambient_installed = ambient_provider.install(
                "shfmt",
                min_version=SemVer("1.0.0"),
            )
            assert ambient_installed is not None

            install_root = temp_dir_path / "go-root"
            provider = GoGetProvider.model_validate(
                {
                    "PATH": str(ambient_provider.bin_dir),
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )

            installed = provider.install("shfmt", min_version=SemVer("3.8.0"))

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_explicit_go_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = GoGetProvider(
                bin_dir=temp_dir_path / "ambient-go/bin",
                install_root=temp_dir_path / "ambient-go",
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@v3.7.0"],
                    },
                },
            )
            ambient_installed = ambient_provider.install(
                "shfmt",
                min_version=SemVer("1.0.0"),
            )
            assert ambient_installed is not None

            gobin = temp_dir_path / "go/bin"
            gopath = temp_dir_path / "go"
            provider = GoGetProvider(
                PATH=str(ambient_provider.bin_dir),
                bin_dir=gobin,
                install_root=gopath,
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )

            installed = provider.install("shfmt", min_version=SemVer("3.8.0"))

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == gopath
            assert provider.bin_dir == gobin
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert installed.loaded_version is not None
            assert ambient_installed.loaded_version is not None
            assert installed.loaded_version > ambient_installed.loaded_version

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GoGetProvider(
                bin_dir=Path(temp_dir) / "go/bin",
                install_root=Path(temp_dir) / "go",
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="shfmt")

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            gobin = Path(temp_dir) / "go/bin"
            gopath = Path(temp_dir) / "go"
            old_provider = GoGetProvider(
                bin_dir=gobin,
                install_root=gopath,
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@v3.7.0"],
                    },
                },
            )
            old_installed = old_provider.install("shfmt")
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("3.7.0")

            provider = GoGetProvider(
                bin_dir=gobin,
                install_root=gopath,
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )
            upgraded = provider.install("shfmt", min_version=SemVer("3.8.0"))
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("3.8.0"),
            )

            updated = provider.update("shfmt", min_version=SemVer("3.8.0"))
            test_machine.assert_shallow_binary_loaded(
                updated,
                expected_version=SemVer("3.8.0"),
            )

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        caplog,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = (
                    GoGetProvider(
                        bin_dir=Path(temp_dir) / "bad-go/bin",
                        install_root=Path(temp_dir) / "bad-go",
                        postinstall_scripts=False,
                        min_release_age=1,
                    )
                    .get_provider_with_overrides(
                        overrides={
                            "shfmt": {
                                "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                            },
                        },
                    )
                    .install("shfmt")
                )
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

            caplog.clear()
            binary = Binary(
                name="shfmt",
                binproviders=[
                    GoGetProvider(
                        bin_dir=Path(temp_dir) / "ok-go/bin",
                        install_root=Path(temp_dir) / "ok-go",
                        postinstall_scripts=False,
                        min_release_age=1,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=1,
                overrides={
                    "goget": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="shfmt",
                binproviders=[
                    GoGetProvider(
                        bin_dir=Path(temp_dir) / "go/bin",
                        install_root=Path(temp_dir) / "go",
                        postinstall_scripts=True,
                        min_release_age=3,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=3,
                overrides={
                    "goget": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_shfmt(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GoGetProvider(
                bin_dir=Path(temp_dir) / "go/bin",
                install_root=Path(temp_dir) / "go",
                postinstall_scripts=True,
                min_release_age=3,
            ).get_provider_with_overrides(
                overrides={
                    "shfmt": {
                        "install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"],
                    },
                },
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="shfmt")

    def test_search_finds_real_go_module_and_install_works(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GoGetProvider(
                install_root=Path(temp_dir) / "go",
                bin_dir=Path(temp_dir) / "go/bin",
                postinstall_scripts=True,
                min_release_age=3,
            )
            # ``shfmt`` is the canonical "go-installable CLI" example —
            # the parent module is the unit ``go list -m -versions``
            # accepts, and the actual installable command lives at
            # ``mvdan.cc/sh/v3/cmd/shfmt`` and produces a ``shfmt`` binary
            # that responds to ``--version``.
            module = "mvdan.cc/sh/v3"
            results = provider.search(module)
            assert len(results) == 1
            match = results[0]
            assert match.name == "v3"  # leaf of module path
            install_args = cast(
                list[str],
                match.overrides.get("goget", {}).get("install_args", []),
            )
            install_arg = install_args[0]
            assert install_arg.startswith(module + "@v")
            assert match.loaded_abspath is None
            assert match.loaded_version is None
            # Repoint install_args at the installable CLI inside the module
            # so .install() actually produces the ``shfmt`` binary.
            shfmt_binary = match.model_copy(
                update={
                    "name": "shfmt",
                    "overrides": {
                        "goget": {
                            "install_args": [
                                f"mvdan.cc/sh/v3/cmd/shfmt@{install_arg.split('@')[1]}",
                            ],
                        },
                    },
                },
            )
            installed = shfmt_binary.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed.name == "shfmt"

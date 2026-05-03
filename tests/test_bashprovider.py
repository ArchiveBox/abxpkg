import tempfile
from pathlib import Path

from abxpkg import BashProvider, Binary


BASH_ZX_INSTALL = (
    'npm install --quiet --prefix "$INSTALL_ROOT/npm" zx '
    '&& ln -sf "$INSTALL_ROOT/npm/node_modules/.bin/zx" "$BIN_DIR/bash-zx"'
)


class TestBashProvider:
    def test_install_root_alias_without_explicit_bin_dir_uses_root_bin(
        self,
        test_machine,
    ):
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "bash-root"
            provider = BashProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={"bash-zx": {"install": BASH_ZX_INSTALL}},
            )

            installed = provider.install("bash-zx")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath is not None

    def test_install_root_and_bin_dir_aliases_install_into_the_requested_paths(
        self,
        test_machine,
    ):
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "bash-root"
            bin_dir = Path(temp_dir) / "bash-bin"
            provider = BashProvider.model_validate(
                {
                    "install_root": install_root,
                    "bin_dir": bin_dir,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={"bash-zx": {"install": BASH_ZX_INSTALL}},
            )

            installed = provider.install("bash-zx")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath is not None

    def test_explicit_bash_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = BashProvider(
                install_root=temp_dir_path / "ambient-root",
                bin_dir=temp_dir_path / "ambient-root/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"bash-zx": {"install": BASH_ZX_INSTALL}},
            )
            ambient_installed = ambient_provider.install("bash-zx")
            assert ambient_installed is not None

            provider = BashProvider(
                PATH=str(ambient_provider.bin_dir),
                install_root=temp_dir_path / "bash-root",
                bin_dir=temp_dir_path / "bash-bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"bash-zx": {"install": BASH_ZX_INSTALL}},
            )

            installed = provider.install("bash-zx")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert provider.bin_dir == temp_dir_path / "bash-bin"
            assert installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath is not None

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = BashProvider(
                install_root=Path(temp_dir) / "bash-root",
                bin_dir=Path(temp_dir) / "bash-root/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"bash-zx": {"install": BASH_ZX_INSTALL}},
            )

            test_machine.exercise_provider_lifecycle(provider, bin_name="bash-zx")

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="bash-zx",
                binproviders=[
                    BashProvider(
                        install_root=Path(temp_dir) / "bash-root",
                        bin_dir=Path(temp_dir) / "bash-root/bin",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                overrides={"bash": {"install": BASH_ZX_INSTALL}},
                postinstall_scripts=True,
                min_release_age=0,
            )

            test_machine.exercise_binary_lifecycle(binary)

    def test_search_returns_empty_for_bash_provider(self):
        # BashProvider has no package index — packages are installed via
        # arbitrary user-provided shell scripts — so search returns [].
        assert BashProvider().search("zx") == []
        assert BashProvider().search("nonexistent-binary-xyz") == []

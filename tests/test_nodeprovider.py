import pwd
import tempfile
from pathlib import Path

from abxpkg import Binary, NodeProvider, SemVer
import pytest


class TestNodeProvider:
    def test_root_managed_install_uses_sudo_invoking_uid(self, tmp_path):
        invoking_uid = next(
            entry.pw_uid for entry in pwd.getpwall() if entry.pw_uid > 0
        )
        provider = NodeProvider(install_root=tmp_path / "node")

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

    def test_official_distribution_provides_node_and_npm_without_root(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "node"
            provider = NodeProvider(install_root=install_root)

            installed = Binary(name="npm", binproviders=[provider]).install(
                no_cache=True,
            )

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath == install_root.resolve() / "bin" / "npm"
            assert (install_root / "bin" / "node").is_file()
            assert (install_root / "bin" / "npm").exists()
            assert (
                provider.exec("node", cmd=("--version",), quiet=True).stdout.strip()
                == "v22.23.1"
            )
            assert provider.exec("npm", cmd=("--version",), quiet=True).returncode == 0

    def test_node_version_floor_is_enforced_before_download(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "node"
            provider = NodeProvider(install_root=install_root)

            with pytest.raises(ValueError):
                provider.install(
                    "node",
                    min_version=SemVer("23.0.0"),
                    no_cache=True,
                )

            assert not install_root.exists()

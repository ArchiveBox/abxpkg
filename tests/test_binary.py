import tempfile
from pathlib import Path

import pytest

from abxpkg import (
    Binary,
    BinProvider,
    EnvProvider,
    NpmProvider,
    PipProvider,
    SemVer,
    UvProvider,
)
from abxpkg.exceptions import (
    BinaryLoadError,
    BinaryInstallError,
    BinaryUninstallError,
    BinaryUpdateError,
)
from abxpkg.windows_compat import VENV_BIN_SUBDIR


class TestBinary:
    def test_short_aliases_match_loaded_field_names(self):
        binary = Binary(
            name="python",
            binproviders=[
                EnvProvider(postinstall_scripts=True, min_release_age=0),
            ],
        ).load(no_cache=True)

        assert binary.binproviders
        assert binary.binprovider == binary.loaded_binprovider
        assert binary.abspath == binary.loaded_abspath
        assert binary.abspaths == binary.loaded_abspaths
        assert binary.version == binary.loaded_version
        assert binary.sha256 == binary.loaded_sha256
        assert binary.mtime == binary.loaded_mtime
        assert binary.euid == binary.loaded_euid

    def test_get_binprovider_applies_overrides_and_provider_filtering(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Point EnvProvider at an empty PATH and install_root so the test
            # stays hermetic even when ``black`` is already installed
            # elsewhere on the host or cached from a prior test run.
            empty_bin_dir = Path(tmpdir) / "empty_bin"
            empty_bin_dir.mkdir()
            env_provider = EnvProvider(
                PATH=str(empty_bin_dir),
                install_root=Path(tmpdir) / "empty_env",
            )
            pip_provider = PipProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            binary = Binary(
                name="black",
                binproviders=[env_provider, pip_provider],
                overrides={"pip": {"install_args": ["black"]}},
                postinstall_scripts=True,
                min_release_age=0,
            )

            overridden_provider = binary.get_binprovider("pip")
            assert overridden_provider.get_install_args("black") == ("black",)
            with pytest.raises(KeyError):
                binary.get_binprovider("brew")

            installed = binary.install()
            assert installed.loaded_binprovider is not None
            assert installed.loaded_binprovider.name == "pip"

            with pytest.raises(BinaryLoadError):
                test_machine.unloaded_binary(binary).load(
                    binproviders=["env"],
                    no_cache=True,
                )
            loaded = test_machine.unloaded_binary(binary).load(
                binproviders=["pip"],
                no_cache=True,
            )
            test_machine.assert_shallow_binary_loaded(loaded)

    def test_get_binprovider_applies_provider_field_patches_from_binary_overrides(
        self,
    ):
        binary = Binary(
            name="forum-dl",
            binproviders=[
                PipProvider(
                    postinstall_scripts=False,
                    min_release_age=7,
                ),
            ],
            overrides={
                "pip": {
                    "install_args": ["forum-dl", "cchardet==2.2.0a2"],
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            },
        )

        provider = binary.get_binprovider("pip")

        assert provider.postinstall_scripts is True
        assert provider.min_release_age == 0
        assert provider.get_install_args("forum-dl") == (
            "forum-dl",
            "cchardet==2.2.0a2",
        )

    def test_min_version_rejection_paths_raise_public_errors(self):
        binary = Binary(
            name="python",
            binproviders=[
                EnvProvider(postinstall_scripts=True, min_release_age=0),
            ],
            min_version=SemVer("999.0.0"),
            postinstall_scripts=True,
            min_release_age=0,
        )

        with pytest.raises(BinaryLoadError):
            binary.load()
        with pytest.raises(BinaryInstallError):
            binary.install()
        with pytest.raises(BinaryUpdateError):
            binary.update()
        with pytest.raises(BinaryUninstallError):
            binary.uninstall()

    def test_install_and_update_upgrade_real_installed_version(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_path = Path(tmpdir) / "venv"
            old_binary = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        install_root=venv_path,
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                overrides={"pip": {"install_args": ["black==23.1.0"]}},
            )
            old_installed = old_binary.install()
            assert old_installed.loaded_version is not None
            required_version = SemVer.parse("24.0.0")
            assert required_version is not None
            assert tuple(old_installed.loaded_version) < tuple(required_version)

            upgraded = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        install_root=venv_path,
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                min_version=SemVer("24.0.0"),
            ).install()
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("24.0.0"),
            )

            updated = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        install_root=venv_path,
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                min_version=SemVer("24.0.0"),
            ).update()
            test_machine.assert_shallow_binary_loaded(
                updated,
                expected_version=SemVer("24.0.0"),
            )

            removed = updated.uninstall()
            assert removed.loaded_abspath is None
            assert removed.loaded_mtime is None
            assert removed.loaded_euid is None

    def test_empty_binprovider_filter_returns_binary_unchanged(self):
        binary = Binary(
            name="python",
            binproviders=[
                EnvProvider(postinstall_scripts=True, min_release_age=0),
            ],
            postinstall_scripts=True,
            min_release_age=0,
        )

        assert binary.install(binproviders=[]) == binary
        assert binary.load(binproviders=[]) == binary
        assert binary.update(binproviders=[]) == binary
        assert binary.uninstall(binproviders=[]) == binary

    def test_binary_params_override_provider_defaults_and_binary_overrides_win(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PipProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=False,
                min_release_age=36500,
            ).get_provider_with_overrides(
                overrides={"black": {"install_args": ["black"]}},
            )
            binary = Binary(
                name="black",
                binproviders=[provider],
                postinstall_scripts=True,
                min_release_age=0,
                overrides={"pip": {"install_args": ["black==23.1.0"]}},
            )

            resolved_provider = binary.get_binprovider("pip")
            assert resolved_provider.get_install_args("black") == ("black==23.1.0",)

            installed = binary.install()
            assert installed.loaded_version == SemVer("23.1.0")

            upgraded = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        install_root=Path(tmpdir) / "venv",
                        postinstall_scripts=False,
                        min_release_age=36500,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                min_version=SemVer("24.0.0"),
            ).install()
            test_machine.assert_shallow_binary_loaded(
                upgraded,
                expected_version=SemVer("24.0.0"),
            )

    def test_binary_install_works_with_provider_install_root_alias(self, test_machine):
        with tempfile.TemporaryDirectory() as tmpdir:
            install_root = Path(tmpdir) / "pip-root"
            providers: list[BinProvider] = [
                PipProvider.model_validate(
                    {
                        "install_root": install_root,
                        "postinstall_scripts": True,
                        "min_release_age": 0,
                    },
                ),
            ]
            binary = Binary(
                name="black",
                binproviders=providers,
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = binary.install()

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed.loaded_abspath is not None
            provider = binary.get_binprovider("pip")
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "venv" / VENV_BIN_SUBDIR
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_binary_dry_run_passes_through_to_provider_without_installing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PipProvider(
                install_root=Path(tmpdir) / "venv",
                postinstall_scripts=True,
                min_release_age=0,
            )
            binary = Binary(
                name="black",
                binproviders=[provider],
                postinstall_scripts=True,
                min_release_age=0,
            )

            installed = binary.install(dry_run=True)
            assert installed.loaded_version == SemVer("999.999.999")
            assert provider.load("black", quiet=True, no_cache=True) is None

            ensured = binary.install(dry_run=True)
            assert ensured.loaded_version == SemVer("999.999.999")
            assert provider.load("black", quiet=True, no_cache=True) is None

            updated = binary.update(dry_run=True)
            assert updated.loaded_version == SemVer("999.999.999")
            assert provider.load("black", quiet=True, no_cache=True) is None

            with pytest.raises(BinaryUninstallError):
                binary.uninstall(dry_run=True)
            assert provider.load("black", quiet=True, no_cache=True) is None

    def test_binary_dry_run_install_does_not_update_stale_existing_binary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_path = Path(tmpdir) / "venv"
            old_binary = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        install_root=venv_path,
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                overrides={"pip": {"install_args": ["black==23.1.0"]}},
            )
            old_installed = old_binary.install()
            assert old_installed.loaded_version == SemVer("23.1.0")

            binary = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        install_root=venv_path,
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                min_version=SemVer("24.0.0"),
            )
            dry_installed = binary.install(dry_run=True)
            assert dry_installed.loaded_version == SemVer("999.999.999")

            loaded_after_dry_run = binary.get_binprovider("pip").load(
                "black",
                quiet=True,
                no_cache=True,
            )
            assert loaded_after_dry_run is not None
            assert loaded_after_dry_run.loaded_version == SemVer("23.1.0")

    def test_binary_install_no_cache_bypasses_already_loaded_short_circuit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            binary = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        install_root=Path(tmpdir) / "venv",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = binary.install()
            assert installed.is_valid

            forced = installed.install(dry_run=True, no_cache=True)
            assert forced.loaded_version == SemVer("999.999.999")

    def test_binary_uninstall_prioritizes_provider_with_cached_install_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            npm_provider = NpmProvider(
                install_root=tmp_path / "npm",
                postinstall_scripts=True,
                min_release_age=0,
            )
            installed = npm_provider.install("zx")

            assert installed is not None
            assert npm_provider.load("zx", no_cache=True) is not None

            binary = Binary(
                name="zx",
                binproviders=[
                    UvProvider(
                        install_root=tmp_path / "uv",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                    npm_provider,
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )

            removed = binary.uninstall()

            assert removed.loaded_abspath is None
            assert npm_provider.load("zx", no_cache=True) is None

    def test_binary_action_args_override_binary_and_provider_defaults(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PipProvider(
                install_root=Path(tmpdir) / "venv",
                dry_run=True,
                postinstall_scripts=False,
                min_release_age=36500,
            )
            binary = Binary(
                name="black",
                binproviders=[provider],
                postinstall_scripts=False,
                min_release_age=36500,
            )

            installed = binary.install(
                dry_run=False,
                postinstall_scripts=True,
                min_release_age=0,
            )

            test_machine.assert_shallow_binary_loaded(installed)

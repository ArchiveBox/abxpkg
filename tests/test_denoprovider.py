import logging
import tempfile
from pathlib import Path
from typing import cast

import pytest

from abxpkg import Binary, DenoProvider
from abxpkg.binprovider import BinProvider
from abxpkg.exceptions import BinaryInstallError, BinProviderInstallError


class TestDenoProvider:
    def test_install_root_alias_installs_into_the_requested_prefix(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "deno-root"
            provider = DenoProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": False,
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
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            # Real on-disk side effect: deno's shim landed in <root>/bin/cowsay
            # and the global module cache is populated under cache_dir.
            assert (install_root / "bin" / "cowsay").exists()
            assert provider.cache_dir.exists()

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DenoProvider(
                install_root=Path(temp_dir) / "deno",
                postinstall_scripts=False,
                min_release_age=0,
            )
            installed, _ = test_machine.exercise_provider_lifecycle(
                provider,
                bin_name="cowsay",
                assert_version_command=False,
            )
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.is_relative_to(provider.install_root)

    def test_provider_defaults_and_binary_overrides_enforce_min_release_age(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = DenoProvider(
                install_root=Path(tmpdir) / "strict-deno",
                postinstall_scripts=False,
                min_release_age=36500,
            )
            # CI matrix installs Deno 2.5+ which supports
            # --minimum-dependency-age.
            assert strict_provider.supports_min_release_age("install") is True

            with pytest.raises(BinProviderInstallError):
                strict_provider.install("cowsay")
            test_machine.assert_provider_missing(strict_provider, "cowsay")

            direct_override = strict_provider.install(
                "cowsay",
                min_release_age=0,
            )
            test_machine.assert_shallow_binary_loaded(
                direct_override,
                assert_version_command=False,
            )
            assert (
                strict_provider.install_root is not None
                and (strict_provider.install_root / "bin" / "cowsay").exists()
            )
            assert strict_provider.uninstall("cowsay", min_release_age=0)

            binary = Binary(
                name="cowsay",
                binproviders=cast(
                    list[BinProvider],
                    [
                        DenoProvider(
                            install_root=Path(tmpdir) / "binary-deno",
                            postinstall_scripts=False,
                            min_release_age=36500,
                        ),
                    ],
                ),
                postinstall_scripts=False,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )

    def test_min_release_age_extreme_value_blocks_install(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            strict_provider = DenoProvider(
                install_root=Path(tmpdir) / "deno",
                postinstall_scripts=False,
                min_release_age=36500,  # 100 years
            )
            assert strict_provider.supports_min_release_age("install") is True
            with pytest.raises(BinProviderInstallError):
                strict_provider.install("cowsay")
            assert strict_provider.install_root is not None
            assert not (strict_provider.install_root / "bin" / "cowsay").exists()

    def test_postinstall_scripts_default_off_does_not_block_simple_packages(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = DenoProvider(
                install_root=Path(tmpdir) / "deno",
                postinstall_scripts=False,
                min_release_age=0,
            )
            installed = provider.install("cowsay")
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.exists()

    def test_jsr_scheme_is_honored_for_jsr_packages(self, test_machine):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = DenoProvider(
                install_root=Path(tmpdir) / "deno",
                postinstall_scripts=False,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "fileserver": {
                        "install_args": ["jsr:@std/http/file-server"],
                    },
                },
            )

            installed = provider.install("fileserver")
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root is not None
            # ``deno install`` writes ``bin/fileserver`` on POSIX and
            # ``bin/fileserver.CMD`` (+ optional ``.PS1`` wrapper) on
            # Windows; compare parent + stem so both layouts pass.
            assert installed.loaded_abspath.parent == provider.install_root / "bin"
            assert installed.loaded_abspath.stem == "fileserver"

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="cowsay",
                binproviders=cast(
                    list[BinProvider],
                    [
                        DenoProvider(
                            install_root=Path(temp_dir) / "deno",
                            postinstall_scripts=False,
                            min_release_age=0,
                        ),
                    ],
                ),
                postinstall_scripts=False,
                min_release_age=0,
            )
            test_machine.exercise_binary_lifecycle(
                binary,
                assert_version_command=False,
            )

    def test_provider_dry_run_does_not_install_cowsay(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DenoProvider(
                install_root=Path(temp_dir) / "deno",
                postinstall_scripts=False,
                min_release_age=0,
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="cowsay")
            shim = Path(temp_dir) / "deno" / "bin" / "cowsay"
            assert not shim.exists()

    def test_supports_methods_do_not_emit_unsupported_warnings(self, caplog):
        with tempfile.TemporaryDirectory() as tmpdir:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                provider = DenoProvider(
                    install_root=Path(tmpdir) / "deno",
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
                binproviders=cast(
                    list[BinProvider],
                    [
                        DenoProvider(
                            install_root=Path(tmpdir) / "deno",
                            postinstall_scripts=False,
                            min_release_age=36500,
                        ),
                    ],
                ),
                postinstall_scripts=False,
                min_release_age=36500,
            )
            with pytest.raises(BinaryInstallError):
                failing_binary.install()

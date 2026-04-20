import logging
import os
import tempfile
from pathlib import Path

import pytest

from abxpkg import Binary, PnpmProvider, SemVer
from abxpkg.exceptions import BinaryInstallError, BinProviderInstallError


class TestPnpmProvider:
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
            # POSIX shells propagate the failing vendor binary's exit code;
            # Windows ``.cmd`` wrappers return 0 but emit the ``is not
            # recognized`` error to stderr — accept either as proof the
            # postinstall was skipped.
            proc = installed.exec(cmd=("--version",), quiet=True)
            assert proc.returncode != 0 or "not recognized" in (proc.stderr or "")
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
            assert installed.loaded_abspath.parent == bin_dir
            # POSIX writes ``bin_dir/zx`` while Windows writes the
            # ``bin_dir/zx.CMD`` launcher — compare ``.stem`` so both
            # layouts pass.
            assert installed.loaded_abspath.stem == "zx"
            # Real on-disk pnpm install side effects.
            assert (install_root / "node_modules" / "zx" / "package.json").exists()
            assert (install_root / "package.json").exists()
            assert (install_root / "pnpm-lock.yaml").exists()
            # The pnpm content-addressable store should also have been
            # populated under cache_dir.
            store_root = provider.cache_dir
            assert store_root.exists()

    def test_explicit_prefix_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = PnpmProvider(
                install_root=temp_dir_path / "ambient-pnpm",
                postinstall_scripts=True,
                min_release_age=0,
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
                min_release_age=0,
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
            assert installed.loaded_abspath.parent == bin_dir
            # POSIX writes ``bin_dir/zx`` while Windows writes the
            # ``bin_dir/zx.CMD`` launcher — compare ``.stem`` so both
            # layouts pass.
            assert installed.loaded_abspath.stem == "zx"
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
                min_release_age=0,
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
                min_release_age=0,
            )
            installed, _ = test_machine.exercise_provider_lifecycle(
                provider,
                bin_name="zx",
            )
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.is_relative_to(provider.install_root)

    def test_provider_direct_min_version_revalidates_old_install_and_upgrades(
        self,
        test_machine,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            pnpm_prefix = Path(tmpdir) / "pnpm"
            old_provider = PnpmProvider(
                install_root=pnpm_prefix,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={"zx": {"install_args": ["zx@7.2.3"]}},
            )
            old_installed = old_provider.install("zx", min_version=SemVer("1.0.0"))
            assert old_installed is not None
            assert old_installed.loaded_version == SemVer("7.2.3")

            upgraded = PnpmProvider(
                install_root=pnpm_prefix,
                postinstall_scripts=True,
                min_release_age=0,
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
                    min_release_age=0,
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

            direct_override = strict_provider.install("zx", min_release_age=0)
            test_machine.assert_shallow_binary_loaded(direct_override)
            assert strict_provider.uninstall("zx", min_release_age=0)

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
                min_release_age=0,
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
                min_release_age=0,
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
                min_release_age=0,
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
                        min_release_age=0,
                    ).get_provider_with_overrides(
                        overrides={"optipng": {"install_args": ["optipng-bin"]}},
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
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
                        min_release_age=0,
                    ).get_provider_with_overrides(
                        overrides={"optipng": {"install_args": ["optipng-bin"]}},
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=0,
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
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_zx(self, test_machine):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PnpmProvider(
                install_root=Path(temp_dir) / "pnpm",
                postinstall_scripts=True,
                min_release_age=0,
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
                min_release_age=0,
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
                    min_release_age=0,
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
                    min_release_age=0,
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

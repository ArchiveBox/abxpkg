import logging
import subprocess
import tempfile

from pathlib import Path

import pytest

from abxpkg import Binary, BrewProvider, SemVer
from abxpkg.exceptions import BinaryInstallError


def _pick_formula_for_live_cycle() -> str:
    probe = BrewProvider(postinstall_scripts=True, min_release_age=3)
    assert probe.is_valid
    brew_bin = probe.INSTALLER_BINARY().loaded_abspath
    candidates = ("hello", "jq", "watch", "fzy")
    for formula in candidates:
        proc = subprocess.run(
            [str(brew_bin), "list", "--formula", formula],
            capture_output=True,
            text=True,
        )
        if (
            proc.returncode != 0
            and probe.get_abspath(formula, quiet=True, no_cache=True) is None
        ):
            return formula
    for formula in candidates:
        try:
            probe.uninstall(formula, quiet=True, no_cache=True)
        except Exception:
            continue
        proc = subprocess.run(
            [str(brew_bin), "list", "--formula", formula],
            capture_output=True,
            text=True,
        )
        if (
            proc.returncode != 0
            and probe.get_abspath(formula, quiet=True, no_cache=True) is None
        ):
            return formula
    raise AssertionError(
        "Unable to find a brew formula candidate that can be installed on the test machine",
    )


class TestBrewProvider:
    def test_install_root_alias_symlinks_formula_into_requested_bin_dir(
        self,
        test_machine,
    ):
        test_machine.require_tool("brew")
        formula = _pick_formula_for_live_cycle()

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "brew-root"
            provider = BrewProvider(
                install_root=install_root,
                postinstall_scripts=True,
                min_release_age=3,
            )

            installed = provider.install(formula, no_cache=True)
            assert installed is not None
            bin_dir = provider.bin_dir
            assert bin_dir is not None

            test_machine.assert_shallow_binary_loaded(installed)
            assert provider.install_root == install_root
            assert bin_dir == install_root / "bin"
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath == bin_dir / formula
            assert installed.loaded_abspath.is_symlink()
            assert installed.loaded_abspath.resolve() != installed.loaded_abspath

            provider.uninstall(formula, quiet=True, no_cache=True)
            assert not installed.loaded_abspath.exists()

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("brew")
        formula = _pick_formula_for_live_cycle()
        provider = BrewProvider(postinstall_scripts=True, min_release_age=3)

        installed, _ = test_machine.exercise_provider_lifecycle(
            provider,
            bin_name=formula,
        )
        assert provider.install_root is not None
        assert provider.bin_dir == provider.install_root / "bin"
        assert installed.loaded_abspath is not None
        assert installed.loaded_abspath.parent == provider.bin_dir

    def test_unsupported_min_release_age_warns_and_continues(
        self,
        test_machine,
        caplog,
    ):
        test_machine.require_tool("brew")
        formula = _pick_formula_for_live_cycle()

        provider_for_cleanup = BrewProvider(
            postinstall_scripts=False,
            min_release_age=3,
        )
        try:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = BrewProvider().install(
                    formula,
                    min_release_age=1,
                )
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text

            caplog.clear()
            binary = Binary(
                name=formula,
                binproviders=[BrewProvider()],
                postinstall_scripts=False,
                min_release_age=1,
            )
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = binary.install(no_cache=True)
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
        finally:
            provider_for_cleanup.uninstall(formula, quiet=True, no_cache=True)

    def test_postinstall_disable_is_live_and_min_version_is_enforced(
        self,
        test_machine,
    ):
        test_machine.require_tool("brew")
        formula = _pick_formula_for_live_cycle()
        provider = BrewProvider(postinstall_scripts=True, min_release_age=3)

        installed = provider.install(
            formula,
            postinstall_scripts=False,
            min_release_age=3,
            no_cache=True,
        )
        test_machine.assert_shallow_binary_loaded(installed)

        updated = provider.update(
            formula,
            postinstall_scripts=False,
            min_release_age=3,
            no_cache=True,
        )
        test_machine.assert_shallow_binary_loaded(updated)

        with pytest.raises(ValueError):
            provider.update(
                formula,
                postinstall_scripts=True,
                min_release_age=3,
                min_version=SemVer("999.0.0"),
                no_cache=True,
            )

        too_new = Binary(
            name=formula,
            binproviders=[
                BrewProvider(postinstall_scripts=True, min_release_age=3),
            ],
            postinstall_scripts=True,
            min_release_age=3,
            min_version=SemVer("999.0.0"),
        )
        with pytest.raises(BinaryInstallError):
            too_new.install(no_cache=True)

    def test_helper_install_args_used_by_native_brew_backend(self, test_machine):
        test_machine.require_tool("brew")
        # Keep this multi-package contract independent of the lifecycle
        # candidate picker. ``watch`` pulls a comparatively large dependency
        # closure on fresh macOS runners, while these two leaf formulae test
        # the same native multi-argument install path.
        primary = "hello"
        extra = "fzy"

        provider = BrewProvider(
            postinstall_scripts=True,
            min_release_age=3,
        ).get_provider_with_overrides(
            overrides={primary: {"install_args": [primary, extra]}},
        )

        for pkg in (primary, extra):
            try:
                provider.uninstall(pkg, quiet=True, no_cache=True)
            except Exception:
                pass

        installed = provider.install(primary, no_cache=True)
        test_machine.assert_shallow_binary_loaded(installed)

        assert provider.load(extra, quiet=True, no_cache=True) is not None

        for pkg in (primary, extra):
            try:
                provider.uninstall(pkg, quiet=True, no_cache=True)
            except Exception:
                pass

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("brew")
        formula = _pick_formula_for_live_cycle()
        binary = Binary(
            name=formula,
            binproviders=[
                BrewProvider(postinstall_scripts=True, min_release_age=3),
            ],
            postinstall_scripts=True,
            min_release_age=3,
        )
        test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_formula(self, test_machine):
        test_machine.require_tool("brew")
        provider = BrewProvider(postinstall_scripts=False, min_release_age=3)
        test_machine.exercise_provider_dry_run(
            provider,
            bin_name=test_machine.pick_missing_brew_formula(),
        )

    def test_search_finds_real_brew_formula_and_install_works(self, test_machine):
        test_machine.require_tool("brew")
        provider = BrewProvider(postinstall_scripts=True, min_release_age=3)
        results = provider.search("jq")
        assert results, "brew search jq should return matches"
        names = [r.name for r in results]
        assert "jq" in names
        match = next(r for r in results if r.name == "jq")
        assert match.overrides == {"brew": {"install_args": ["jq"]}}
        assert match.loaded_abspath is None
        assert match.loaded_version is None
        provider.uninstall("jq", quiet=True, no_cache=True)
        installed = match.install()
        test_machine.assert_shallow_binary_loaded(installed)
        assert installed.name == "jq"

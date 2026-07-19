import logging

import pytest

from abxpkg import AptProvider, Binary


@pytest.mark.root_required
class TestAptProvider:
    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("apt-get")

        provider = AptProvider(postinstall_scripts=True, min_release_age=3)
        test_machine.exercise_provider_lifecycle(
            provider,
            bin_name=test_machine.pick_missing_apt_package(),
        )

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        caplog,
    ):
        test_machine.require_tool("apt-get")
        package = test_machine.pick_missing_apt_package()

        with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
            installed = AptProvider().install(
                package,
                postinstall_scripts=False,
                min_release_age=1,
            )
        test_machine.assert_shallow_binary_loaded(installed)
        assert "ignoring unsupported min_release_age=1" in caplog.text
        assert "ignoring unsupported postinstall_scripts=False" in caplog.text

        caplog.clear()
        binary = Binary(
            name=test_machine.pick_missing_apt_package(),
            binproviders=[AptProvider()],
            postinstall_scripts=False,
            min_release_age=1,
        )
        with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
            installed = binary.install()
        test_machine.assert_shallow_binary_loaded(installed)
        assert "ignoring unsupported min_release_age=1" in caplog.text
        assert "ignoring unsupported postinstall_scripts=False" in caplog.text

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("apt-get")

        binary = Binary(
            name=test_machine.pick_missing_apt_package(),
            binproviders=[
                AptProvider(postinstall_scripts=True, min_release_age=3),
            ],
            postinstall_scripts=True,
            min_release_age=3,
        )
        test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_package(self, test_machine):
        test_machine.require_tool("apt-get")
        provider = AptProvider(postinstall_scripts=True, min_release_age=3)
        test_machine.exercise_provider_dry_run(
            provider,
            bin_name=test_machine.pick_missing_apt_package(),
        )

    def test_search_finds_real_apt_package_and_install_works(self, test_machine):
        test_machine.require_tool("apt-get")
        provider = AptProvider(postinstall_scripts=True, min_release_age=3)
        results = provider.search("wget")
        assert results, "apt-cache search wget should return matches"
        names = [r.name for r in results]
        assert "wget" in names
        wget_match = next(r for r in results if r.name == "wget")
        assert wget_match.overrides == {"apt": {"install_args": ["wget"]}}
        # The returned Binary is non-loaded — it has no abspath/version yet.
        assert wget_match.loaded_abspath is None
        assert wget_match.loaded_version is None
        # ...but installing it must produce a real, valid binary on disk.
        provider.uninstall("wget", quiet=True, no_cache=True)
        installed = wget_match.install()
        test_machine.assert_shallow_binary_loaded(installed)
        assert installed.name == "wget"

    def test_helper_install_args_used_by_native_apt_backend(self, test_machine):
        test_machine.require_tool("apt-get")

        primary = test_machine.pick_missing_apt_package()
        extra = "jq" if primary != "jq" else "tree"

        provider = AptProvider(
            postinstall_scripts=True,
            min_release_age=3,
        ).get_provider_with_overrides(
            overrides={primary: {"install_args": [primary, extra]}},
        )

        for pkg in (primary, extra):
            provider.uninstall(pkg, quiet=True, no_cache=True)

        provider.install(primary, no_cache=True)
        assert provider.load(extra, quiet=True, no_cache=True) is not None

        provider.uninstall(primary, no_cache=True)
        provider.uninstall(extra, quiet=True, no_cache=True)

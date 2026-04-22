import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from abxpkg import Binary, PlaywrightProvider


def _resolve_shim_target(shim: Path) -> Path:
    """Resolve a managed bin_dir shim to its real browser target.

    On Linux the shim is a symlink, so ``.resolve()`` naturally follows
    it. On macOS the shim is a shell script that ``exec``s the binary
    inside a ``.app`` bundle (a direct symlink breaks dyld's
    ``@executable_path``-relative Framework loading), so ``.resolve()``
    just returns the script path itself. Parse the ``exec <path>`` line
    to recover the target in that case. We key off ``is_symlink()``
    rather than comparing ``shim == shim.resolve()`` because macOS
    ``$TMPDIR`` lives under ``/var/folders/...`` → ``/private/var/...``,
    so ``resolve()`` always differs from the input even for plain files.
    """
    if shim.is_symlink():
        return shim.resolve()
    try:
        script = shim.read_text(encoding="utf-8")
    except OSError:
        return shim.resolve()
    match = re.search(r"exec '([^']+)'", script)
    if not match:
        return shim.resolve()
    return Path(match.group(1)).resolve()


@pytest.fixture(scope="module")
def seeded_playwright_root():
    with tempfile.TemporaryDirectory() as temp_dir:
        install_root = Path(temp_dir) / "seeded-playwright-root"
        provider = PlaywrightProvider(install_root=install_root)
        installed = provider.install("chromium", no_cache=True)
        assert installed is not None
        assert installed.loaded_abspath is not None
        assert installed.loaded_abspath.exists()
        yield install_root


class TestPlaywrightProvider:
    @staticmethod
    def copy_seeded_playwright_root(
        seeded_playwright_root: Path,
        install_root: Path,
    ) -> None:
        shutil.copytree(
            seeded_playwright_root,
            install_root,
            symlinks=True,
            copy_function=os.link,
        )
        copied_bin_dir = install_root / "bin"
        if not copied_bin_dir.is_dir():
            return
        seeded_resolved = seeded_playwright_root.resolve()
        for link_path in copied_bin_dir.iterdir():
            if link_path.is_symlink():
                link_target = link_path.resolve(strict=False)
                if seeded_resolved not in link_target.parents:
                    continue
                relative_target = link_target.relative_to(seeded_resolved)
                link_path.unlink()
                link_path.symlink_to(install_root / relative_target)
                continue
            # macOS chrome/chromium shims are shell scripts that hardcode
            # the seeded install_root path; rewrite them so they exec the
            # copy under this test's install_root instead.
            if not link_path.is_file():
                continue
            try:
                script = link_path.read_text(encoding="utf-8")
            except OSError:
                continue
            match = re.search(r"exec '([^']+)'", script)
            if not match:
                continue
            target_path = Path(match.group(1))
            if seeded_resolved not in target_path.resolve().parents:
                continue
            relative_target = target_path.resolve().relative_to(seeded_resolved)
            new_target = install_root / relative_target
            link_path.write_text(
                script.replace(str(target_path), str(new_target)),
                encoding="utf-8",
            )

    def test_chromium_install_puts_real_browser_into_managed_bin_dir(
        self,
        test_machine,
        seeded_playwright_root,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            playwright_root = Path(temp_dir) / "playwright-root"
            self.copy_seeded_playwright_root(seeded_playwright_root, playwright_root)
            provider = PlaywrightProvider(install_root=playwright_root)

            installed = provider.load("chromium", no_cache=True)
            assert installed is not None
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed.name == "chromium"
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.exists()
            assert provider.bin_dir is not None
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert installed.loaded_abspath == provider.bin_dir / "chromium"
            # The shim resolves into ``playwright_root/cache`` (the
            # managed ``PLAYWRIGHT_BROWSERS_PATH`` for this provider).
            real_target = _resolve_shim_target(installed.loaded_abspath)
            assert (playwright_root / "cache").resolve() in real_target.parents
            # Playwright lays out chromium builds as chromium-<build>/.
            assert any(
                child.name.startswith("chromium-")
                for child in (playwright_root / "cache").iterdir()
                if child.is_dir()
            )

            loaded = provider.load("chromium", no_cache=True)
            test_machine.assert_shallow_binary_loaded(
                loaded,
                assert_version_command=False,
            )
            assert loaded is not None
            assert loaded.loaded_abspath is not None
            assert loaded.loaded_abspath.resolve() == installed.loaded_abspath.resolve()

            loaded_or_installed = provider.install("chromium")
            test_machine.assert_shallow_binary_loaded(
                loaded_or_installed,
                assert_version_command=False,
            )
            assert loaded_or_installed is not None
            assert loaded_or_installed.loaded_abspath is not None
            assert (
                loaded_or_installed.loaded_abspath.resolve()
                == installed.loaded_abspath.resolve()
            )

    def test_install_root_alias_without_explicit_bin_dir_uses_root_bin(
        self,
        test_machine,
        seeded_playwright_root,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "pw-root"
            self.copy_seeded_playwright_root(seeded_playwright_root, install_root)
            provider = PlaywrightProvider.model_validate(
                {
                    "install_root": install_root,
                },
            )

            installed = provider.install("chromium")

            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == provider.bin_dir
            # Chromium build landed directly under install_root (which is
            # the effective PLAYWRIGHT_BROWSERS_PATH for this provider).
            assert any(
                child.name.startswith("chromium-")
                for child in (install_root / "cache").iterdir()
                if child.is_dir()
            )

    def test_install_root_and_bin_dir_aliases_install_into_the_requested_paths(
        self,
        test_machine,
        seeded_playwright_root,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "pw-root"
            bin_dir = Path(temp_dir) / "custom-bin"
            self.copy_seeded_playwright_root(seeded_playwright_root, install_root)
            provider = PlaywrightProvider.model_validate(
                {
                    "install_root": install_root,
                    "bin_dir": bin_dir,
                },
            )

            installed = provider.install("chromium")

            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == bin_dir
            # Browser tree still landed in install_root, not bin_dir.
            assert any(
                child.name.startswith("chromium-")
                for child in (install_root / "cache").iterdir()
                if child.is_dir()
            )

    def test_explicit_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
        seeded_playwright_root,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_root = temp_dir_path / "ambient-root"
            self.copy_seeded_playwright_root(seeded_playwright_root, ambient_root)
            ambient_provider = PlaywrightProvider(
                install_root=ambient_root,
                bin_dir=ambient_root / "bin",
            )
            ambient_installed = ambient_provider.install("chromium")
            assert ambient_installed is not None
            assert ambient_installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath.parent == ambient_provider.bin_dir

            install_root = temp_dir_path / "playwright-root"
            self.copy_seeded_playwright_root(seeded_playwright_root, install_root)
            provider = PlaywrightProvider(
                PATH=str(ambient_provider.bin_dir),
                install_root=install_root,
                bin_dir=temp_dir_path / "custom-bin",
            )

            installed = provider.install("chromium")

            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert provider.bin_dir == temp_dir_path / "custom-bin"
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == provider.bin_dir
            # The two providers resolve to different on-disk symlinks.
            assert installed.loaded_abspath != ambient_installed.loaded_abspath

    def test_provider_install_args_are_passed_through_to_playwright_install(
        self,
        test_machine,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            playwright_root = Path(temp_dir) / "playwright-root"
            # Use ``--no-shell`` to skip the headless shell download, which
            # is a real playwright install flag we can verify by checking
            # that ``chromium_headless_shell-*`` does NOT end up on disk.
            provider = PlaywrightProvider(
                install_root=playwright_root,
            ).get_provider_with_overrides(
                overrides={"chromium": {"install_args": ["chromium", "--no-shell"]}},
            )

            installed = provider.install("chromium")

            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.exists()
            cache_dir = playwright_root / "cache"
            chromium_dirs = [
                child
                for child in cache_dir.iterdir()
                if child.is_dir()
                and child.name.startswith("chromium-")
                and not child.name.startswith("chromium_headless_shell")
            ]
            assert chromium_dirs, "chromium-<build> dir should exist on disk"
            # ``--no-shell`` should have skipped the headless shell download.
            headless_shell_dirs = [
                child
                for child in cache_dir.iterdir()
                if child.is_dir() and child.name.startswith("chromium_headless_shell")
            ]
            assert not headless_shell_dirs, (
                f"--no-shell should have skipped chromium_headless_shell, "
                f"but found: {[p.name for p in headless_shell_dirs]}"
            )

    def test_provider_direct_methods_exercise_real_lifecycle(
        self,
        test_machine,
        seeded_playwright_root,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "playwright-root"
            self.copy_seeded_playwright_root(seeded_playwright_root, install_root)
            provider = PlaywrightProvider(install_root=install_root)

            loaded = provider.load("chromium", no_cache=True)
            test_machine.assert_shallow_binary_loaded(
                loaded,
                assert_version_command=False,
            )

            loaded_or_installed = provider.install("chromium")
            test_machine.assert_shallow_binary_loaded(
                loaded_or_installed,
                assert_version_command=False,
            )

            updated = provider.update("chromium", no_cache=True)
            test_machine.assert_shallow_binary_loaded(
                updated,
                assert_version_command=False,
            )

            assert provider.uninstall("chromium", no_cache=True) is True
            test_machine.assert_provider_missing(provider, "chromium")

    def test_binary_direct_methods_exercise_real_lifecycle(
        self,
        test_machine,
        seeded_playwright_root,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "playwright-root"
            self.copy_seeded_playwright_root(seeded_playwright_root, install_root)
            binary = Binary(
                name="chromium",
                binproviders=[
                    PlaywrightProvider(
                        install_root=install_root,
                    ),
                ],
            )

            loaded = binary.load(no_cache=True)
            test_machine.assert_shallow_binary_loaded(
                loaded,
                assert_version_command=False,
            )

            loaded_or_installed = test_machine.unloaded_binary(binary).install()
            test_machine.assert_shallow_binary_loaded(
                loaded_or_installed,
                assert_version_command=False,
            )

            updated = loaded.update()
            test_machine.assert_shallow_binary_loaded(
                updated,
                assert_version_command=False,
            )

            removed = updated.uninstall()
            assert not removed.is_valid
            assert removed.loaded_binprovider is None
            assert removed.loaded_abspath is None
            assert removed.loaded_version is None
            assert removed.loaded_sha256 is None
            test_machine.assert_binary_missing(binary)

    def test_update_refreshes_chromium_in_place(
        self,
        test_machine,
        seeded_playwright_root,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            playwright_root = Path(temp_dir) / "playwright-root"
            self.copy_seeded_playwright_root(seeded_playwright_root, playwright_root)
            provider = PlaywrightProvider(install_root=playwright_root)

            installed = provider.load("chromium", no_cache=True)
            test_machine.assert_shallow_binary_loaded(
                installed,
                assert_version_command=False,
            )
            assert installed is not None
            assert installed.loaded_abspath is not None
            original_target = installed.loaded_abspath.resolve()
            assert original_target.exists()

            updated = provider.update("chromium", no_cache=True)
            test_machine.assert_shallow_binary_loaded(
                updated,
                assert_version_command=False,
            )
            assert updated is not None
            assert updated.loaded_abspath is not None
            # The shim resolves to a chromium build that actually
            # exists on disk after update (whether the build-id moved
            # depends on the current playwright release, but the
            # resolved target must always exist and still live inside
            # ``playwright_root``).
            updated_target = _resolve_shim_target(updated.loaded_abspath)
            assert updated_target.exists()
            assert (playwright_root / "cache").resolve() in updated_target.parents
            assert any(
                child.name.startswith("chromium-")
                for child in (playwright_root / "cache").iterdir()
                if child.is_dir()
            )

    def test_provider_dry_run_does_not_install_chromium(self, test_machine):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            playwright_root = Path(temp_dir) / "playwright-root"
            provider = PlaywrightProvider(install_root=playwright_root)

            test_machine.exercise_provider_dry_run(provider, bin_name="chromium")
            # dry_run must not have actually downloaded any browsers.
            cache_dir = playwright_root / "cache"
            browser_dirs = (
                [
                    p
                    for p in cache_dir.iterdir()
                    if p.is_dir()
                    and p.name.startswith(("chromium-", "firefox-", "webkit-"))
                ]
                if cache_dir.is_dir()
                else []
            )
            assert not browser_dirs, (
                f"dry_run should not have created any browser dirs, got: "
                f"{[p.name for p in browser_dirs]}"
            )

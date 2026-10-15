# ci-runner: hosted
"""Exercise the real privileged browser installer and its managed Node runtime."""

from abxpkg import PuppeteerProvider


def test_reloaded_puppeteer_installer_preserves_runtime_under_sudo(tmp_path):
    root = tmp_path / "puppeteer"
    provider = PuppeteerProvider(
        install_root=root,
        postinstall_scripts=True,
        min_release_age=0,
    )
    provider.setup(postinstall_scripts=True, min_release_age=0)

    # Reload the provider as a subsequent CLI invocation does. The installed
    # browsers script must retain the runtime that pnpm provisioned for it.
    reloaded = PuppeteerProvider(
        install_root=root,
        postinstall_scripts=True,
        min_release_age=0,
    )
    reloaded.setup(postinstall_scripts=True, min_release_age=0)
    installer = reloaded.INSTALLER_BINARY()
    assert installer.loaded_abspath is not None
    normal = reloaded.exec(installer.loaded_abspath, ["install", "--help"])
    assert normal.returncode == 0, normal.stderr
    assert "--install-deps" in normal.stdout

    privileged = reloaded._run_install_with_sudo(["--help"])
    assert privileged is not None
    assert privileged.returncode == 0, privileged.stderr
    assert privileged.stdout == normal.stdout

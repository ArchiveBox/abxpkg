import tempfile
from pathlib import Path

from abxpkg import Binary, EnvProvider, NpmProvider, PipProvider


class TestInstall:
    def test_env_provider_install_surface_uses_real_python(self, test_machine):
        provider = EnvProvider(postinstall_scripts=True, min_release_age=3)
        loaded = provider.load("python")
        installed = provider.install("python")
        loaded_or_installed = provider.install("python")
        updated = provider.update("python")
        uninstalled = provider.uninstall("python")

        test_machine.assert_shallow_binary_loaded(loaded)
        test_machine.assert_shallow_binary_loaded(installed)
        test_machine.assert_shallow_binary_loaded(loaded_or_installed)
        assert updated is None
        assert uninstalled is False

    def test_pip_binary_install_surface(self, test_machine):
        with tempfile.TemporaryDirectory() as tmpdir:
            binary = Binary(
                name="black",
                binproviders=[
                    PipProvider(
                        install_root=Path(tmpdir) / "venv",
                        postinstall_scripts=True,
                        min_release_age=3,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=3,
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_npm_binary_install_surface(self, test_machine):
        with tempfile.TemporaryDirectory() as tmpdir:
            binary = Binary(
                name="zx",
                binproviders=[
                    NpmProvider(
                        install_root=Path(tmpdir) / "npm",
                        postinstall_scripts=True,
                        min_release_age=3,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=3,
            )
            test_machine.exercise_binary_lifecycle(binary)

import tempfile
from pathlib import Path
import logging
import subprocess

import pytest

from abxpkg import Binary, DockerProvider, SemVer


@pytest.mark.docker_required
class TestDockerProvider:
    def test_bin_dir_alias_without_explicit_install_root_keeps_default_root(
        self,
        test_machine,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = Path(temp_dir) / "custom-bin"
            provider = DockerProvider.model_validate(
                {
                    "bin_dir": bin_dir,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )

            installed = provider.install("shellcheck")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root is not None
            assert provider.install_root != bin_dir.parent
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath.parent == provider.bin_dir
            metadata_dir = provider.install_root / "metadata"
            assert metadata_dir == provider.install_root / "metadata"
            assert (metadata_dir / "shellcheck.json").is_file()

    def test_install_root_alias_without_explicit_bin_dir_uses_root_bin(
        self,
        test_machine,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "docker-root"
            provider = DockerProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )

            installed = provider.install("shellcheck")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            metadata_dir = install_root / "metadata"
            assert metadata_dir.is_dir()
            assert (metadata_dir / "shellcheck.json").is_file()

    def test_install_root_and_bin_dir_aliases_install_the_shim_in_the_requested_location(
        self,
        test_machine,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "docker-root"
            bin_dir = Path(temp_dir) / "custom-bin"
            provider = DockerProvider.model_validate(
                {
                    "install_root": install_root,
                    "bin_dir": bin_dir,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )

            installed = provider.install("shellcheck")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath.parent == provider.bin_dir
            metadata_dir = install_root / "metadata"
            assert metadata_dir.is_dir()
            assert (metadata_dir / "shellcheck.json").is_file()

    def test_explicit_docker_shim_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = DockerProvider(
                bin_dir=temp_dir_path / "ambient-docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            ambient_installed = ambient_provider.install("shellcheck")
            assert ambient_installed is not None

            docker_shim_dir = temp_dir_path / "docker/bin"
            provider = DockerProvider(
                PATH=str(ambient_provider.bin_dir),
                bin_dir=docker_shim_dir,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )

            installed = provider.install("shellcheck")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root is not None
            assert provider.install_root != docker_shim_dir.parent
            assert provider.bin_dir == docker_shim_dir
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert ambient_installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath.parent == ambient_provider.bin_dir
            assert installed.loaded_version == SemVer("0.10.0")

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DockerProvider(
                bin_dir=Path(temp_dir) / "docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="shellcheck")

    def test_provider_direct_min_version_revalidates_final_installed_image(
        self,
        test_machine,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DockerProvider(
                bin_dir=Path(temp_dir) / "docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            with pytest.raises(ValueError):
                provider.install("shellcheck", min_version=SemVer("999.0.0"))

            loaded = provider.load("shellcheck", quiet=True, no_cache=True)
            test_machine.assert_shallow_binary_loaded(loaded)
            assert loaded is not None
            assert loaded.loaded_version == SemVer("0.10.0")

    def test_latest_tag_falls_back_to_runtime_version_probe(self, test_machine):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DockerProvider(
                bin_dir=Path(temp_dir) / "docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:latest"]},
                },
            )

            installed = provider.install("shellcheck")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_version is not None

    def test_install_repairs_snapshot_collision_by_repulling_image(
        self,
        monkeypatch,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DockerProvider(
                bin_dir=Path(temp_dir) / "docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            )
            image_ref = "koalaman/shellcheck:latest"
            calls: list[list[str]] = []

            def fake_exec(self, bin_name, cmd=(), quiet=False, **kwargs):
                cmd_list = [str(part) for part in cmd]
                calls.append(cmd_list)
                if cmd_list == ["pull", image_ref]:
                    if calls.count(cmd_list) == 1:
                        return subprocess.CompletedProcess(
                            [str(bin_name), *cmd_list],
                            1,
                            "latest: Pulling from koalaman/shellcheck\n",
                            "unable to prepare extraction snapshot: "
                            'AlreadyExists: target snapshot "sha256:test": '
                            "already exists\n",
                        )
                    return subprocess.CompletedProcess(
                        [str(bin_name), *cmd_list],
                        0,
                        "Status: Downloaded newer image for koalaman/shellcheck:latest\n",
                        "",
                    )
                if cmd_list == ["image", "rm", "--force", image_ref]:
                    return subprocess.CompletedProcess(
                        [str(bin_name), *cmd_list],
                        0,
                        "Deleted: sha256:test\n",
                        "",
                    )
                raise AssertionError(f"unexpected docker exec: {cmd_list}")

            monkeypatch.setattr(DockerProvider, "exec", fake_exec)

            output = provider.default_install_handler(
                "shellcheck",
                install_args=[image_ref],
            )

            assert ["pull", image_ref] in calls
            assert calls.count(["pull", image_ref]) == 2
            assert ["image", "rm", "--force", image_ref] in calls
            assert "already exists" in output
            assert provider.install_root is not None
            assert provider.bin_dir is not None
            assert (provider.install_root / "metadata" / "shellcheck.json").is_file()
            assert (provider.bin_dir / "shellcheck").is_file()

    def test_unsupported_security_controls_warn_and_continue(
        self,
        test_machine,
        caplog,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = (
                    DockerProvider(
                        bin_dir=Path(temp_dir) / "bad/bin",
                        postinstall_scripts=False,
                        min_release_age=1,
                    )
                    .get_provider_with_overrides(
                        overrides={
                            "shellcheck": {
                                "install_args": ["koalaman/shellcheck:v0.10.0"],
                            },
                        },
                    )
                    .install("shellcheck")
                )
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

            caplog.clear()
            binary = Binary(
                name="shellcheck",
                binproviders=[
                    DockerProvider(
                        bin_dir=Path(temp_dir) / "ok/bin",
                        postinstall_scripts=False,
                        min_release_age=1,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=1,
                overrides={
                    "docker": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            with caplog.at_level(logging.WARNING, logger="abxpkg.binprovider"):
                installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert "ignoring unsupported min_release_age=1" in caplog.text
            assert "ignoring unsupported postinstall_scripts=False" in caplog.text

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="shellcheck",
                binproviders=[
                    DockerProvider(
                        bin_dir=Path(temp_dir) / "docker/bin",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                overrides={
                    "docker": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_shellcheck(self, test_machine):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DockerProvider(
                bin_dir=Path(temp_dir) / "docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="shellcheck")

    def test_search_finds_real_docker_image_and_install_works(self, test_machine):
        test_machine.require_docker_daemon()
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DockerProvider(
                bin_dir=Path(temp_dir) / "docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            )
            results = provider.search("alpine")
            assert results, "docker search alpine should return Docker Hub matches"
            names = [r.name for r in results]
            assert "alpine" in names
            match = next(r for r in results if r.name == "alpine")
            assert match.overrides == {"docker": {"install_args": ["alpine:latest"]}}
            assert match.loaded_abspath is None
            assert match.loaded_version is None
            installed = match.install()
            test_machine.assert_shallow_binary_loaded(installed)
            assert installed.name == "alpine"

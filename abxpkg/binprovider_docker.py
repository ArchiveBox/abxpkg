#!/usr/bin/env python3
__package__ = "abxpkg"

import os
import json

from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field, model_validator, TypeAdapter
from typing import Self

from .base_types import (
    DEFAULT_ABXPKG_LIB_DIR,
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    HostBinPath,
    abxpkg_install_root_default,
)
from .semver import SemVer
from .binprovider import BinProvider, log_method_call, remap_kwargs
from .logging import format_subprocess_output


# Ultimate fallback when neither the constructor arg nor
# ``ABXPKG_DOCKER_ROOT`` nor ``ABXPKG_LIB_DIR`` is set.
DEFAULT_DOCKER_ROOT = DEFAULT_ABXPKG_LIB_DIR / "docker"


class DockerProvider(BinProvider):
    name: BinProviderName = "docker"
    _log_emoji = "🐳"
    INSTALLER_BIN: BinName = "docker"
    INSTALLER_BINPROVIDERS: ClassVar[tuple[BinProviderName, ...] | None] = ("env",)

    PATH: PATHStr = (
        ""  # Starts empty; setup_PATH() replaces it with the shim bin_dir only.
    )

    # Default: ABXPKG_DOCKER_ROOT > ABXPKG_LIB_DIR/docker > None.
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("docker"),
        validation_alias="docker_root",
    )
    # detect_euid_to_use() resolves this to the managed shim dir, setup() creates it, and
    # _write_shim()/default_abspath_handler() read it to surface docker-backed wrappers.
    bin_dir: Path | None = Field(default=None, validation_alias="docker_shim_dir")

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Fill in the managed docker root and shim bin_dir defaults after validation."""
        if self.install_root is None:
            self.install_root = DEFAULT_DOCKER_ROOT
        if self.bin_dir is None:
            self.bin_dir = self.install_root / "bin"

        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use with the docker shim bin_dir only."""
        bin_dir = self.bin_dir
        assert bin_dir is not None
        self.PATH = self._merge_PATH(
            bin_dir,
            PATH=self.PATH,
            prepend=True,
        )
        super().setup_PATH(no_cache=no_cache)

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        bin_dir = self.bin_dir
        install_root = self.install_root
        assert bin_dir is not None
        assert install_root is not None
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(bin_dir,),
                preserve_root=True,
            )
        bin_dir.mkdir(parents=True, exist_ok=True)
        (install_root / "metadata").mkdir(parents=True, exist_ok=True)

    def default_install_args_handler(self, bin_name: BinName, **context) -> InstallArgs:
        return [f"{bin_name}:latest"]

    def default_docs_url_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> str | None:
        try:
            install_args = self.get_install_args(str(bin_name), quiet=True)
        except Exception:
            install_args = [f"{bin_name}:latest"]
        ref = next(
            (str(arg) for arg in install_args if arg and not arg.startswith("-")),
            f"{bin_name}:latest",
        )
        # strip digest and tag
        image = ref.split("@", 1)[0]
        if ":" in image.rsplit("/", 1)[-1]:
            image = image.rsplit(":", 1)[0]
        # Docker Hub conventions:
        #   "redis"            -> Official image at /_/redis
        #   "user/repo"        -> Community image at /r/user/repo
        #   "ghcr.io/user/x"   -> Not Docker Hub; link to the registry's web UI
        if "/" not in image:
            return f"https://hub.docker.com/_/{image}"
        first, _, rest = image.partition("/")
        if "." in first or ":" in first:  # custom registry
            return f"https://{first}/{rest}"
        return f"https://hub.docker.com/r/{image}"

    @remap_kwargs({"packages": "install_args"})
    def _main_image_ref(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
    ) -> str:
        """Return the primary image ref that owns the shim/version metadata for bin_name."""
        package_list = list(install_args or self.get_install_args(bin_name))
        assert package_list, (
            f"{self.__class__.__name__} requires at least one docker image ref for {bin_name}"
        )
        return str(package_list[0])

    def _image_tag(self, image_ref: str) -> str:
        """Extract the explicit docker tag from an image ref, defaulting to ``latest``."""
        image_without_digest = image_ref.split("@", 1)[0]
        last_component = image_without_digest.rsplit("/", 1)[-1]
        if ":" in last_component:
            return image_without_digest.rsplit(":", 1)[-1]
        return "latest"

    def _write_metadata(self, bin_name: str, image_ref: str) -> None:
        """Persist the resolved docker image ref/tag that currently backs a shim."""
        install_root = self.install_root
        assert install_root is not None
        metadata_dir = install_root / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        (metadata_dir / f"{bin_name}.json").write_text(
            json.dumps(
                {
                    "image": image_ref,
                    "tag": self._image_tag(image_ref),
                },
            ),
            encoding="utf-8",
        )

    def _read_metadata(self, bin_name: str) -> dict[str, Any] | None:
        """Read the cached shim metadata for a docker-backed binary, if present."""
        install_root = self.install_root
        assert install_root is not None
        metadata_path = install_root / "metadata" / f"{bin_name}.json"
        if not metadata_path.is_file():
            return None
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    def _write_shim(
        self,
        bin_name: str,
        image_ref: str,
        no_cache: bool = False,
    ) -> Path:
        """Write the executable wrapper that runs the selected docker image as a CLI."""
        bin_dir = self.bin_dir
        assert bin_dir is not None
        wrapper_path = bin_dir / bin_name
        wrapper_path.parent.mkdir(parents=True, exist_ok=True)
        docker_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert docker_bin

        wrapper_path.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env sh",
                    "set -eu",
                    'workdir="${PWD:-$(pwd)}"',
                    # Keep docker shims stateless: always run as an interactive,
                    # auto-cleaned one-shot container with the caller's uid/gid.
                    f'exec "{docker_bin}" run --rm -i --user "$(id -u):$(id -g)" -v "$workdir:$workdir" -w "$workdir" "{image_ref}" "$@"',
                    "",
                ],
            ),
            encoding="utf-8",
        )
        wrapper_path.chmod(0o755)
        return wrapper_path

    @staticmethod
    def _should_repair_failed_pull(output: str) -> bool:
        """Detect known broken-layer states that should trigger a forced image cleanup."""
        return any(
            marker in output
            for marker in (
                "unable to prepare extraction snapshot",
                "failed to prepare extraction snapshot",
                "target snapshot",
                "missing parent",
            )
        )

    def default_search_handler(
        self,
        bin_name: BinName,
        min_version: SemVer | None = None,
        min_release_age: float | None = None,
        timeout: int | None = None,
        **context,
    ) -> list:
        """Search Docker Hub for images whose name matches bin_name (substring)."""
        from .binary import Binary

        # Use ``self.INSTALLER_BINARY`` so docker's auto-install logic
        # kicks in if env's docker is missing/broken.
        installer = self.INSTALLER_BINARY(no_cache=bool(context.get("no_cache", False)))
        assert installer and installer.loaded_abspath
        # ``docker search --format '{{json .}}' <q>`` emits one JSON object per match.
        proc = self.exec(
            bin_name=installer.loaded_abspath,
            cmd=["search", "--limit", "25", "--format", "{{json .}}", str(bin_name)],
            quiet=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return []
        results: list = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            image_name = entry.get("Name", "")
            description = entry.get("Description", "") or image_name
            if not image_name or str(bin_name).lower() not in image_name.lower():
                continue
            # ``Name`` from docker search is the full ``namespace/image``
            # ref; the leaf is what we use as ``Binary.name`` so shim and
            # metadata file writes (which use the bin_name as filename)
            # don't fail on slashes. Full ref is preserved in install_args.
            leaf_name = image_name.rsplit("/", 1)[-1]
            results.append(
                Binary(
                    name=leaf_name,
                    description=f"{image_name} - {description}".strip(" -"),
                    binproviders=[self],
                    overrides={self.name: {"install_args": [f"{image_name}:latest"]}},
                ),
            )
        return results

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        logs: list[str] = []
        for image_ref in install_args:
            proc = self.exec(
                bin_name=installer_bin,
                cmd=["pull", image_ref],
                timeout=timeout,
            )
            if proc.returncode != 0:
                pull_output = format_subprocess_output(proc.stdout, proc.stderr)
                if self._should_repair_failed_pull(pull_output):
                    repair_proc = self.exec(
                        bin_name=installer_bin,
                        cmd=["image", "rm", "--force", image_ref],
                        quiet=True,
                        timeout=timeout,
                    )
                    logs.extend(
                        output
                        for output in (
                            pull_output,
                            format_subprocess_output(
                                repair_proc.stdout,
                                repair_proc.stderr,
                            ),
                        )
                        if output
                    )
                    proc = self.exec(
                        bin_name=installer_bin,
                        cmd=["pull", image_ref],
                        timeout=timeout,
                    )
                if proc.returncode != 0:
                    self._raise_proc_error("install", image_ref, proc)
            logs.append(format_subprocess_output(proc.stdout, proc.stderr))

        main_image = self._main_image_ref(bin_name, install_args)
        self._write_metadata(bin_name, main_image)
        self._write_shim(bin_name, main_image, no_cache=no_cache)

        return "\n".join(logs).strip()

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        return self.default_install_handler(
            bin_name=bin_name,
            install_args=install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
            no_cache=no_cache,
            timeout=timeout,
        )

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> bool:
        install_args = install_args or self.get_install_args(bin_name)
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        bin_dir = self.bin_dir
        install_root = self.install_root
        assert bin_dir is not None
        assert install_root is not None
        wrapper_path = bin_dir / bin_name
        wrapper_path.unlink(missing_ok=True)
        (install_root / "metadata" / f"{bin_name}.json").unlink(missing_ok=True)

        main_image = self._main_image_ref(bin_name, install_args)
        for image_ref in install_args:
            proc = self.exec(
                bin_name=installer_bin,
                cmd=["image", "rm", "--force", image_ref],
                quiet=True,
                timeout=timeout,
            )
            if proc.returncode != 0 and image_ref == main_image:
                self._raise_proc_error("uninstall", image_ref, proc)

        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        bin_dir = self.bin_dir
        assert bin_dir is not None
        wrapper_path = bin_dir / str(bin_name)
        if wrapper_path.is_file() and os.access(wrapper_path, os.R_OK):
            return TypeAdapter(HostBinPath).validate_python(wrapper_path)
        abspath = super().default_abspath_handler(bin_name, **context)
        if abspath is None:
            return None
        return TypeAdapter(HostBinPath).validate_python(abspath)

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> SemVer | None:
        metadata = self._read_metadata(str(bin_name))
        if metadata:
            parsed_tag = SemVer.parse(str(metadata["tag"]))
            if parsed_tag:
                return parsed_tag

        abspath = abspath or self.get_abspath(bin_name, quiet=True)
        if not abspath:
            return None

        try:
            version = super().default_version_handler(
                bin_name,
                abspath=abspath,
                timeout=timeout,
                **context,
            )
            if isinstance(version, SemVer):
                return version
            if isinstance(version, (str, bytes, tuple)):
                return SemVer.parse(version)
            if isinstance(version, list):
                return SemVer.parse(tuple(version))
            return None
        except ValueError:
            return None

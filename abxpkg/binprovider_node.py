#!/usr/bin/env python3

__package__ = "abxpkg"

import hashlib
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request

from pathlib import Path
from typing import ClassVar, NamedTuple, Self

from pydantic import Field, computed_field, model_validator

from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    DEFAULT_ABXPKG_LIB_DIR,
    abxpkg_cache_dir_default,
    abxpkg_ephemeral_cache_dir_default,
    abxpkg_install_root_default,
    bin_abspath,
)
from .binprovider import BinProvider, log_method_call, remap_kwargs
from .semver import SemVer


NODE_VERSION = "22.23.1"
NODE_BINARIES = frozenset({"node", "npm", "npx", "corepack"})


class NodeArtifact(NamedTuple):
    archive_name: str
    url: str
    sha256: str


NODE_ARTIFACTS: dict[tuple[str, str], tuple[str, str]] = {
    ("linux", "x86_64"): (
        f"node-v{NODE_VERSION}-linux-x64.tar.xz",
        "9749e988f437343b7fa832c69ded82a312e41a03116d766797ac14f6f9eee578",
    ),
    ("linux", "arm64"): (
        f"node-v{NODE_VERSION}-linux-arm64.tar.xz",
        "0294e8b915ab75f92c7513d2fcb830ae06e10684e6c603e99a87dbf8835389c1",
    ),
    ("darwin", "x86_64"): (
        f"node-v{NODE_VERSION}-darwin-x64.tar.gz",
        "b8da981b8a0b1241b70249204916da76c63573ddf5814dbd2d1e41069105cb81",
    ),
    ("darwin", "arm64"): (
        f"node-v{NODE_VERSION}-darwin-arm64.tar.gz",
        "ef28d8fab2c0e4314522d4bb1b7173270aa3937e93b92cb7de79c112ac1fa953",
    ),
}


def node_artifact(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> NodeArtifact:
    normalized_machine = (machine or platform.machine()).lower()
    if normalized_machine in {"amd64", "x64"}:
        normalized_machine = "x86_64"
    elif normalized_machine == "aarch64":
        normalized_machine = "arm64"
    target = ((system or platform.system()).lower(), normalized_machine)
    if target[0] == "linux" and platform.libc_ver()[0].lower() == "musl":
        raise RuntimeError(
            "NodeProvider official Linux binaries require glibc; musl is unsupported",
        )
    try:
        archive_name, sha256 = NODE_ARTIFACTS[target]
    except KeyError as err:
        supported = ", ".join(f"{os_name}/{arch}" for os_name, arch in NODE_ARTIFACTS)
        raise RuntimeError(
            f"NodeProvider does not support {target[0]}/{target[1]}; "
            f"supported targets are {supported}",
        ) from err
    return NodeArtifact(
        archive_name=archive_name,
        url=f"https://nodejs.org/dist/v{NODE_VERSION}/{archive_name}",
        sha256=sha256,
    )


class NodeProvider(BinProvider):
    """Install the official prebuilt Node.js runtime without root privileges."""

    name: BinProviderName = "node"
    _log_emoji = "🟢"
    INSTALLER_BIN: BinName = "node"
    INSTALLER_BINPROVIDERS: ClassVar[tuple[BinProviderName, ...] | None] = ("env",)
    DEFAULT_SUPPORTED_PLATFORMS: ClassVar[tuple[str, ...] | None] = (
        "darwin",
        "linux",
    )

    PATH: PATHStr = ""
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("node"),
        validation_alias="node_root",
    )
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> dict[str, str]:
        if self.install_root is None:
            return {}
        return {"NODE_PATH": str(self.install_root / "lib" / "node_modules")}

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.install_root is None:
            self.install_root = DEFAULT_ABXPKG_LIB_DIR / "node"
        if self.bin_dir is None:
            self.bin_dir = self.install_root / "bin"
        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        assert self.bin_dir is not None
        self.PATH = self._merge_PATH(self.bin_dir, PATH=self.PATH, prepend=True)
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
        assert self.install_root is not None
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.install_root.parent,),
                preserve_root=True,
            )
        self.install_root.parent.mkdir(parents=True, exist_ok=True)

    def default_install_args_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> InstallArgs:
        return (f"node@{NODE_VERSION}",)

    def default_docs_url_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> str:
        return f"https://nodejs.org/download/release/v{NODE_VERSION}/"

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        """Resolve Node's own executables directly from its managed archive."""
        assert self.bin_dir is not None
        return bin_abspath(bin_name, PATH=str(self.bin_dir))

    def _download_archive(
        self,
        *,
        no_cache: bool,
    ) -> tuple[NodeArtifact, Path]:
        artifact = node_artifact()
        assert self.install_root is not None
        cache_root = (
            abxpkg_ephemeral_cache_dir_default("node")
            if no_cache
            else abxpkg_cache_dir_default("node")
            or self.install_root.parent / "cache" / "node"
        )
        cache_dir = cache_root / f"v{NODE_VERSION}"
        cache_dir.mkdir(parents=True, exist_ok=True)
        archive_path = cache_dir / artifact.archive_name

        if archive_path.is_file():
            cached_sha256 = self.get_sha256(
                "node",
                abspath=archive_path,
                no_cache=True,
            )
            if cached_sha256 != artifact.sha256:
                archive_path.unlink()
            else:
                return artifact, archive_path

        temp_fd, temp_name = tempfile.mkstemp(
            dir=cache_dir,
            prefix=f".{artifact.archive_name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        digest = hashlib.sha256()
        try:
            request = urllib.request.Request(
                artifact.url,
                headers={"User-Agent": f"abxpkg node bootstrap/{NODE_VERSION}"},
            )
            with (
                os.fdopen(temp_fd, "wb") as archive_file,
                urllib.request.urlopen(
                    request,
                    timeout=self.install_timeout,
                ) as response,
            ):
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(chunk)
                    archive_file.write(chunk)

            downloaded_sha256 = digest.hexdigest()
            if downloaded_sha256 != artifact.sha256:
                raise RuntimeError(
                    f"Refusing downloaded {artifact.url}: expected SHA256 "
                    f"{artifact.sha256}, got {downloaded_sha256}",
                )
            os.replace(temp_path, archive_path)
        finally:
            temp_path.unlink(missing_ok=True)

        return artifact, archive_path

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        if str(bin_name) not in NODE_BINARIES:
            raise ValueError(
                f"NodeProvider can install {sorted(NODE_BINARIES)}, not {bin_name!r}",
            )
        requested_args = install_args or self.get_install_args(bin_name)
        if tuple(map(str, requested_args)) != (f"node@{NODE_VERSION}",):
            raise ValueError(
                f"NodeProvider only supports node@{NODE_VERSION}, not "
                f"{', '.join(map(str, requested_args))}",
            )
        installed_version = SemVer.parse(NODE_VERSION)
        if (
            str(bin_name) == "node"
            and min_version
            and installed_version
            and installed_version < min_version
        ):
            raise ValueError(
                f"NodeProvider ships Node {NODE_VERSION}, below required {min_version}",
            )

        artifact, archive_path = self._download_archive(no_cache=no_cache)
        assert self.install_root is not None
        archive_root = artifact.archive_name.removesuffix(".tar.xz").removesuffix(
            ".tar.gz",
        )
        with tempfile.TemporaryDirectory(
            dir=self.install_root.parent,
            prefix=".node-install-",
        ) as temp_dir:
            staging_dir = Path(temp_dir)
            with tarfile.open(archive_path, mode="r:*") as archive:
                archive.extractall(staging_dir, filter="data")
            extracted_root = staging_dir / archive_root
            if not (extracted_root / "bin" / "node").is_file():
                raise RuntimeError(
                    f"Node archive {artifact.archive_name} did not contain bin/node",
                )
            if self.install_root.exists():
                shutil.rmtree(self.install_root)
            os.replace(extracted_root, self.install_root)

        if os.geteuid() == 0 and self.EUID != 0:
            target_gid = self.get_pw_record(self.EUID).pw_gid
            for root in (archive_path.parent, self.install_root):
                for current_root, dir_names, file_names in os.walk(root):
                    current_path = Path(current_root)
                    for path in (
                        current_path,
                        *[current_path / name for name in dir_names],
                        *[current_path / name for name in file_names],
                    ):
                        if path.is_symlink():
                            os.lchown(path, self.EUID, target_gid)
                        else:
                            os.chown(path, self.EUID, target_gid)

        return f"Installed Node.js {NODE_VERSION} from {artifact.url}"

    default_update_handler = default_install_handler

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> bool:
        assert self.install_root is not None
        if self.install_root.exists():
            shutil.rmtree(self.install_root)
        return True

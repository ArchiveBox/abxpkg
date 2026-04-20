#!/usr/bin/env python3

__package__ = "abxpkg"

import json
import os
import sys
from pathlib import Path
from typing import Self

from platformdirs import user_cache_path
from pydantic import Field, TypeAdapter, computed_field, model_validator

from .binary import Binary
from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abxpkg_install_root_default,
)
from .binprovider import (
    BinProvider,
    EnvProvider,
    env_flag_is_true,
    log_method_call,
    remap_kwargs,
)
from .logging import format_subprocess_output
from .semver import SemVer


USER_CACHE_PATH = user_cache_path("yarn", "abxpkg")


# No forced fallback — when no explicit workspace root is set, this
# provider stays unconfigured instead of inventing one implicitly.


class YarnProvider(BinProvider):
    """Yarn package manager provider (Yarn 4 / Berry recommended).

    Yarn 4 runs inside a project dir containing a ``package.json`` and
    ``.yarnrc.yml``. This provider auto-initializes that project dir under
    ``install_root`` on first use, configures ``nodeLinker: node-modules`` so
    binaries land in ``<install_root>/node_modules/.bin``, and writes the
    ``npmMinimalAgeGate`` security setting from ``min_release_age``.

    Yarn classic (1.x) does not support ``npmMinimalAgeGate`` /
    ``--mode skip-build``; on those hosts ``supports_min_release_age`` /
    ``supports_postinstall_disable`` return ``False`` and the runtime falls
    back to a plain install while logging a warning.
    """

    name: BinProviderName = "yarn"
    _log_emoji = "🧶"
    INSTALLER_BIN: BinName = "yarn"

    PATH: PATHStr = ""  # Starts empty; setup_PATH() lazily uses install_root/node_modules/.bin only.
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABXPKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABXPKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    # Workspace dir. Default: ABXPKG_YARN_ROOT > ABXPKG_LIB_DIR/yarn > None.
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("yarn"),
        validation_alias="yarn_prefix",
    )
    # detect_euid_to_use() fills this with ``<install_root>/node_modules/.bin`` and setup()
    # creates it as part of the managed Yarn workspace bootstrap flow.
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        env: dict[str, str] = {
            "YARN_ENABLE_TELEMETRY": "0",
            "YARN_ENABLE_GLOBAL_CACHE": "1",
            "YARN_GLOBAL_FOLDER": str(self.cache_dir),
            "YARN_CACHE_FOLDER": str(self.cache_dir / "v6"),
        }
        if self.install_root:
            node_modules_dir = str(self.install_root / "node_modules")
            env["NODE_MODULES_DIR"] = node_modules_dir
            env["NODE_MODULE_DIR"] = node_modules_dir
            env["NODE_PATH"] = os.pathsep + node_modules_dir
        return env

    @property
    def cache_dir(self) -> Path:
        """Return Yarn's shared global cache directory."""
        # Yarn's global cache roots are always derived from the standard
        # platform cache dir; there is no separate provider field to override.
        return Path(USER_CACHE_PATH)

    def get_cache_info(
        self,
        bin_name: BinName,
        abspath: HostBinPath,
    ) -> dict[str, list[Path]] | None:
        cache_info = super().get_cache_info(bin_name, abspath)
        if cache_info is None or self.install_root is None:
            return cache_info

        install_args = self.get_install_args(str(bin_name), quiet=True) or [
            str(bin_name),
        ]
        main_package = install_args[0]
        package = (
            "@" + main_package[1:].split("@", 1)[0]
            if main_package.startswith("@")
            else main_package.split("@", 1)[0]
        )
        package_json = self.install_root / "node_modules" / package / "package.json"
        if package_json.exists():
            cache_info["fingerprint_paths"].append(package_json)
        return cache_info

    def supports_min_release_age(self, action, no_cache: bool = False) -> bool:
        if action not in ("install", "update"):
            return False
        # npmMinimalAgeGate landed in Yarn 4.10
        threshold = SemVer.parse("4.10.0")
        try:
            installer = self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return False
        version = installer.loaded_version if installer else None
        return bool(version and threshold and version >= threshold)

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        if action not in ("install", "update"):
            return False
        # Yarn 2+ supports the enableScripts setting and --mode skip-build
        threshold = SemVer.parse("2.0.0")
        try:
            installer = self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return False
        version = installer.loaded_version if installer else None
        return bool(version and threshold and version >= threshold)

    def default_install_args_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> InstallArgs:
        if str(bin_name) == "puppeteer":
            return ("puppeteer", "@puppeteer/browsers")
        if str(bin_name) == "puppeteer-browsers":
            return ("@puppeteer/browsers",)
        return TypeAdapter(InstallArgs).validate_python(
            super().default_install_args_handler(bin_name, **context)
            or [str(bin_name)],
        )

    @computed_field
    @property
    def is_valid(self) -> bool:
        return super().is_valid

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Derive Yarn's managed node_modules/.bin dir from install_root."""
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "node_modules" / ".bin"
        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from install_root/node_modules/.bin only."""
        if self.bin_dir:
            self.PATH = self._merge_PATH(
                self.bin_dir,
                PATH=self.PATH,
                prepend=True,
            )
        super().setup_PATH(no_cache=no_cache)

    def INSTALLER_BINARY(self, no_cache: bool = False):
        from . import DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

        if not no_cache and self._INSTALLER_BINARY and self._INSTALLER_BINARY.is_valid:
            loaded = self._INSTALLER_BINARY
        else:
            env_provider = EnvProvider(install_root=None, bin_dir=None)
            env_provider.PATH = env_provider._merge_PATH(
                self.PATH,
                PATH=env_provider.PATH,
                prepend=True,
            )
            raw_provider_names = os.environ.get("ABXPKG_BINPROVIDERS")
            selected_provider_names = (
                [
                    provider_name.strip()
                    for provider_name in raw_provider_names.split(",")
                ]
                if raw_provider_names
                else list(DEFAULT_PROVIDER_NAMES)
            )
            installer_providers = [
                env_provider
                if provider_name == "env"
                else PROVIDER_CLASS_BY_NAME[provider_name]()
                for provider_name in selected_provider_names
                if provider_name
                and provider_name in PROVIDER_CLASS_BY_NAME
                and provider_name != self.name
            ]
            loaded = Binary(
                name=self.INSTALLER_BIN,
                binproviders=installer_providers,
            ).load(no_cache=no_cache)
            if loaded and loaded.loaded_abspath:
                if loaded.loaded_version and loaded.loaded_sha256:
                    self.write_cached_binary(
                        self.INSTALLER_BIN,
                        loaded.loaded_abspath,
                        loaded.loaded_version,
                        loaded.loaded_sha256,
                        resolved_provider_name=(
                            loaded.loaded_binprovider.name
                            if loaded.loaded_binprovider is not None
                            else self.name
                        ),
                        cache_kind="dependency",
                    )
                self._INSTALLER_BINARY = loaded

        raw_provider_names = os.environ.get("ABXPKG_BINPROVIDERS")
        selected_provider_names = (
            [provider_name.strip() for provider_name in raw_provider_names.split(",")]
            if raw_provider_names
            else list(DEFAULT_PROVIDER_NAMES)
        )
        dependency_providers = [
            EnvProvider(install_root=None, bin_dir=None)
            if provider_name == "env"
            else PROVIDER_CLASS_BY_NAME[provider_name]()
            for provider_name in selected_provider_names
            if provider_name
            and provider_name in PROVIDER_CLASS_BY_NAME
            and provider_name != self.name
        ]
        node_loaded = (
            Binary(
                name="node",
                binproviders=dependency_providers,
            ).load(no_cache=no_cache)
            if dependency_providers
            else None
        )
        if (
            node_loaded
            and node_loaded.loaded_abspath
            and node_loaded.loaded_version
            and node_loaded.loaded_sha256
        ):
            self.write_cached_binary(
                "node",
                node_loaded.loaded_abspath,
                node_loaded.loaded_version,
                node_loaded.loaded_sha256,
                resolved_provider_name=(
                    node_loaded.loaded_binprovider.name
                    if node_loaded.loaded_binprovider is not None
                    else self.name
                ),
                cache_kind="dependency",
            )
        return loaded

    @log_method_call(include_result=True)
    def exec(
        self,
        bin_name,
        cmd=(),
        cwd: Path | str = ".",
        quiet=False,
        should_log_command: bool = True,
        **kwargs,
    ):
        # Yarn 4 expects to be invoked from inside its project dir, so default
        # cwd to <yarn_prefix>.
        if cwd == "." and self.install_root:
            self.install_root.mkdir(parents=True, exist_ok=True)
            cwd = self.install_root
        return super().exec(
            bin_name=bin_name,
            cmd=cmd,
            cwd=cwd,
            quiet=quiet,
            should_log_command=should_log_command,
            **kwargs,
        )

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.install_root,),
                preserve_root=True,
            )
        self._ensure_writable_cache_dir(self.cache_dir)
        prefix = self.install_root
        if not prefix:
            raise TypeError(
                "YarnProvider.setup requires yarn_prefix to be set "
                "(pass install_root= or set ABXPKG_YARN_ROOT / ABXPKG_LIB_DIR)",
            )
        prefix.mkdir(parents=True, exist_ok=True)
        package_json = prefix / "package.json"
        if not package_json.exists():
            # Note: do NOT write a ``packageManager`` field — Yarn 1.22 reads
            # it as an opt-in to corepack and refuses to install if the
            # running yarn version doesn't match.
            package_json.write_text(
                json.dumps(
                    {
                        "name": "abxpkg-yarn-project",
                        "version": "0.0.0",
                        "private": True,
                    },
                    indent=2,
                )
                + "\n",
            )
        # Yarn 2+ uses .yarnrc.yml; pin nodeLinker so binaries end up in
        # node_modules/.bin instead of the PnP store.
        installer = self.INSTALLER_BINARY(no_cache=no_cache)
        version = installer.loaded_version if installer else None
        berry_threshold = SemVer.parse("2.0.0")
        if version and berry_threshold and version >= berry_threshold:
            yarnrc = prefix / ".yarnrc.yml"
            existing = yarnrc.read_text() if yarnrc.exists() else ""
            if "nodeLinker:" not in existing:
                yarnrc.write_text(
                    (existing.rstrip("\n") + "\n" if existing else "")
                    + "nodeLinker: node-modules\n",
                )

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
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
        min_release_age = 7.0 if min_release_age is None else min_release_age
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}@>={min_version}"
                if arg
                and not arg.startswith(("-", ".", "/"))
                and ":" not in arg.split("/")[0]
                and "@" not in arg.split("/")[-1]
                else arg
                for arg in install_args
            ]

        installer = self.INSTALLER_BINARY(no_cache=no_cache)
        version = installer.loaded_version if installer else None
        berry_threshold = SemVer.parse("2.0.0")
        is_berry = (
            version is not None
            and berry_threshold is not None
            and version >= berry_threshold
        )

        # Rewrite ``.yarnrc.yml`` (Yarn 2+ only) so npmMinimalAgeGate /
        # enableScripts always reflect the latest provider/binary defaults.
        if is_berry and version is not None and self.install_root:
            prefix = self.install_root
            yarnrc = prefix / ".yarnrc.yml"
            existing = yarnrc.read_text() if yarnrc.exists() else ""
            kept = [
                line
                for line in existing.splitlines()
                if not line.strip().startswith(("npmMinimalAgeGate:", "enableScripts:"))
            ]
            age_threshold = SemVer.parse("4.10.0")
            if (
                min_release_age is not None
                and min_release_age > 0
                and age_threshold is not None
                and version >= age_threshold
            ):
                duration = (
                    f"{int(min_release_age)}d"
                    if min_release_age >= 1 and float(min_release_age).is_integer()
                    else f"{max(int(min_release_age * 24 * 60), 1)}m"
                )
                kept.append(f"npmMinimalAgeGate: {duration}")
            if not postinstall_scripts:
                kept.append("enableScripts: false")
            content = "\n".join(kept)
            yarnrc.write_text(content + "\n" if content else "")

        if is_berry:
            if no_cache:
                cache_proc = self.exec(
                    bin_name=installer_bin,
                    cmd=["cache", "clean", "--all"],
                    timeout=timeout,
                )
                if cache_proc.returncode != 0:
                    self._raise_proc_error("install", install_args, cache_proc)
            cmd = ["add", *install_args]
            if not postinstall_scripts:
                cmd = [
                    "add",
                    "--mode",
                    "skip-build",
                    *install_args,
                ]
        else:
            cmd = ["add", *install_args]
            if no_cache and "--force" not in cmd:
                cmd.insert(1, "--force")

        proc = self.exec(
            bin_name=installer_bin,
            cmd=cmd,
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", install_args, proc)
        return format_subprocess_output(proc.stdout, proc.stderr)

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
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
        min_release_age = 7.0 if min_release_age is None else min_release_age
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}@>={min_version}"
                if arg
                and not arg.startswith(("-", ".", "/"))
                and ":" not in arg.split("/")[0]
                and "@" not in arg.split("/")[-1]
                else arg
                for arg in install_args
            ]

        installer = self.INSTALLER_BINARY(no_cache=no_cache)
        version = installer.loaded_version if installer else None
        berry_threshold = SemVer.parse("2.0.0")
        is_berry = (
            version is not None
            and berry_threshold is not None
            and version >= berry_threshold
        )

        if is_berry and version is not None and self.install_root:
            prefix = self.install_root
            yarnrc = prefix / ".yarnrc.yml"
            existing = yarnrc.read_text() if yarnrc.exists() else ""
            kept = [
                line
                for line in existing.splitlines()
                if not line.strip().startswith(("npmMinimalAgeGate:", "enableScripts:"))
            ]
            age_threshold = SemVer.parse("4.10.0")
            if (
                min_release_age is not None
                and min_release_age > 0
                and age_threshold is not None
                and version >= age_threshold
            ):
                duration = (
                    f"{int(min_release_age)}d"
                    if min_release_age >= 1 and float(min_release_age).is_integer()
                    else f"{max(int(min_release_age * 24 * 60), 1)}m"
                )
                kept.append(f"npmMinimalAgeGate: {duration}")
            if not postinstall_scripts:
                kept.append("enableScripts: false")
            content = "\n".join(kept)
            yarnrc.write_text(content + "\n" if content else "")

        if is_berry:
            if no_cache:
                cache_proc = self.exec(
                    bin_name=installer_bin,
                    cmd=["cache", "clean", "--all"],
                    timeout=timeout,
                )
                if cache_proc.returncode != 0:
                    self._raise_proc_error("update", install_args, cache_proc)
            cmd = ["up", *install_args]
            if not postinstall_scripts:
                cmd = [
                    "up",
                    "--mode",
                    "skip-build",
                    *install_args,
                ]
        else:
            cmd = ["upgrade", *install_args]
            if no_cache and "--force" not in cmd:
                cmd.insert(1, "--force")

        proc = self.exec(
            bin_name=installer_bin,
            cmd=cmd,
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("update", install_args, proc)
        return format_subprocess_output(proc.stdout, proc.stderr)

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
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        install_args = install_args or self.get_install_args(bin_name)
        if str(bin_name) == "puppeteer" and tuple(install_args) == (
            "puppeteer",
            "@puppeteer/browsers",
        ):
            install_args = ["puppeteer"]

        proc = self.exec(
            bin_name=installer_bin,
            cmd=["remove", *install_args],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", install_args, proc)
        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except Exception:
            pass

        try:
            self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return None

        if self.install_root:
            candidate = self.install_root / "node_modules" / ".bin" / str(bin_name)
            if candidate.exists():
                return TypeAdapter(HostBinPath).validate_python(candidate)
        return None

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> SemVer | None:
        try:
            version = self._version_from_exec(
                bin_name,
                abspath=abspath,
                timeout=timeout,
            )
            if version:
                return version
        except ValueError:
            pass

        try:
            self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return None

        if not self.install_root:
            return None
        install_args = self.get_install_args(str(bin_name), **context) or [
            str(bin_name),
        ]
        main_package = install_args[0]
        package = (
            "@" + main_package[1:].split("@", 1)[0]
            if main_package.startswith("@")
            else main_package.split("@", 1)[0]
        )
        assert self.install_root is not None  # guarded by early return above
        package_json = self.install_root / "node_modules" / package / "package.json"
        if package_json.exists():
            try:
                return json.loads(package_json.read_text())["version"]
            except Exception:
                return None
        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_yarn.py load zx
    # ./binprovider_yarn.py install zx
    result = yarn = YarnProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(yarn, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)

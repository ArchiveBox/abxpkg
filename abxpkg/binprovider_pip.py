#!/usr/bin/env python

__package__ = "abxpkg"

import os
import sys
import site
import re
import sysconfig
from platformdirs import user_cache_path

from pathlib import Path
from typing import Self
from pydantic import Field, model_validator, TypeAdapter, computed_field

from .binary import Binary
from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    HostBinPath,
    abxpkg_install_root_default,
    bin_abspath,
    bin_abspaths,
)
from .semver import SemVer
from .binprovider import (
    BinProvider,
    DEFAULT_ENV_PATH,
    EnvProvider,
    env_flag_is_true,
    log_method_call,
    remap_kwargs,
)
from .logging import format_subprocess_output
from .windows_compat import IS_WINDOWS


USER_CACHE_PATH = user_cache_path(
    appname="pip",
    appauthor="abxpkg",
)

# ``venv`` creates ``Scripts/`` on Windows and ``bin/`` everywhere else —
# the directory where python.exe / pip.exe / installed console scripts
# live. All ``install_root/venv/<this>`` lookups must agree on this name.
VENV_BIN_SUBDIR = "Scripts" if IS_WINDOWS else "bin"


# pip >= 26.0 is required for ``--uploaded-prior-to`` (see pypa/pip#13625).
_PIP_MIN_RELEASE_AGE_VERSION = SemVer((26, 0, 0))


class PipProvider(BinProvider):
    name: BinProviderName = "pip"
    _log_emoji = "🐍"
    INSTALLER_BIN: BinName = "pip"

    PATH: PATHStr = ""  # Starts empty; setup_PATH() lazily uses install_root/venv/bin in venv mode, or discovers ambient Python script dirs in global mode.
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABXPKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABXPKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    # None = system site-packages (user or global), otherwise a provider root.
    # In install_root mode the actual virtualenv lives at install_root/venv
    # so provider metadata like derived.env can stay next to it.
    # Default: ABXPKG_PIP_ROOT > ABXPKG_LIB_DIR/pip > None.
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("pip"),
        validation_alias="pip_venv",
    )
    # detect_euid_to_use() fills this from ``<install_root>/venv/bin`` when using a managed
    # virtualenv; global mode leaves it unset so setup_PATH() discovers ambient script dirs.
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        venv_root = self.install_root / "venv"
        env: dict[str, str] = {"VIRTUAL_ENV": str(venv_root)}
        # Add site-packages to PYTHONPATH so scripts can import installed pkgs
        for sp in sorted(
            (venv_root / "lib").glob("python*/site-packages"),
        ):
            env["PYTHONPATH"] = ":" + str(sp)
            break
        return env

    def supports_min_release_age(self, action, no_cache: bool = False) -> bool:
        if action not in ("install", "update"):
            return False
        if self.install_root:
            return True
        try:
            installer = self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return False
        version = installer.loaded_version
        if version is None:
            return False
        return version >= _PIP_MIN_RELEASE_AGE_VERSION  # pyright: ignore[reportOperatorIssue]

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        return action in ("install", "update")

    @staticmethod
    def _install_args_have_option(args: InstallArgs, *options: str) -> bool:
        """Return True when install_args already contains any requested pip option."""
        return any(
            arg == option or arg.startswith(f"{option}=")
            for arg in args
            for option in options
        )

    @computed_field
    @property
    def is_valid(self) -> bool:
        """False if install_root is not created yet or if pip binary is not found in PATH"""
        if self.install_root:
            venv_pip_path = self.install_root / "venv" / VENV_BIN_SUBDIR / "python"
            if venv_pip_path.exists() and not (
                os.path.isfile(venv_pip_path) and os.access(venv_pip_path, os.X_OK)
            ):
                return False
        return super().is_valid

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Derive the managed virtualenv bin_dir from install_root when one is pinned."""
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "venv" / VENV_BIN_SUBDIR
        return self

    @property
    def cache_dir(self) -> Path:
        """Return pip's shared download/build cache dir."""
        return Path(USER_CACHE_PATH)

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from install_root/venv/bin in venv mode, or from discovered ambient Python script dirs in global mode."""
        PATH = self.PATH

        if self.bin_dir:
            self.PATH = self._merge_PATH(self.bin_dir)
        else:
            pip_bin_dirs = {
                *(
                    str(Path(sitepackage_dir).parent.parent.parent / VENV_BIN_SUBDIR)
                    for sitepackage_dir in site.getsitepackages()
                ),
                str(
                    Path(site.getusersitepackages()).parent.parent.parent
                    / VENV_BIN_SUBDIR,
                ),
                sysconfig.get_path("scripts"),
                str(Path(sys.executable).resolve().parent),
            }

            for abspath in bin_abspaths("python", PATH=DEFAULT_ENV_PATH):
                pip_bin_dirs.add(str(abspath.parent))
            for abspath in bin_abspaths("python3", PATH=DEFAULT_ENV_PATH):
                pip_bin_dirs.add(str(abspath.parent))

            # remove any active venv from PATH because we're trying to only get the global system python paths
            active_venv = os.environ.get("VIRTUAL_ENV")
            if active_venv:
                pip_bin_dirs.discard(f"{active_venv}/{VENV_BIN_SUBDIR}")

            self.PATH = self._merge_PATH(*sorted(pip_bin_dirs), PATH=PATH)
        super().setup_PATH(no_cache=no_cache)

    def INSTALLER_BINARY(self, no_cache: bool = False):
        if self.install_root:
            venv_pip = self.install_root / "venv" / VENV_BIN_SUBDIR / "pip"
            if venv_pip.is_file() and os.access(venv_pip, os.X_OK):
                if not no_cache:
                    loaded = self.load_cached_binary(self.INSTALLER_BIN, venv_pip)
                    if loaded and loaded.loaded_abspath:
                        self._INSTALLER_BINARY = loaded
                        return loaded
                if (
                    not no_cache
                    and self._INSTALLER_BINARY
                    and self._INSTALLER_BINARY.loaded_abspath == venv_pip
                    and self._INSTALLER_BINARY.is_valid
                ):
                    return self._INSTALLER_BINARY
                env_provider = EnvProvider(
                    PATH=str(venv_pip.parent),
                    install_root=None,
                    bin_dir=None,
                )
                loaded = env_provider.load(
                    bin_name=self.INSTALLER_BIN,
                    no_cache=no_cache,
                )
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
                    python_loaded = EnvProvider(
                        PATH=str(venv_pip.parent),
                        install_root=None,
                        bin_dir=None,
                    ).load(
                        bin_name="python",
                        no_cache=no_cache,
                    )
                    if (
                        python_loaded
                        and python_loaded.loaded_abspath
                        and python_loaded.loaded_version
                        and python_loaded.loaded_sha256
                    ):
                        self.write_cached_binary(
                            "python",
                            python_loaded.loaded_abspath,
                            python_loaded.loaded_version,
                            python_loaded.loaded_sha256,
                            resolved_provider_name=(
                                python_loaded.loaded_binprovider.name
                                if python_loaded.loaded_binprovider is not None
                                else self.name
                            ),
                            cache_kind="dependency",
                        )
                    self._INSTALLER_BINARY = loaded
                    return self._INSTALLER_BINARY
        loaded = super().INSTALLER_BINARY(no_cache=no_cache)
        from . import DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

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
        python_loaded = (
            Binary(
                name="python",
                binproviders=dependency_providers,
            ).load(no_cache=no_cache)
            if dependency_providers
            else None
        )
        if (
            python_loaded
            and python_loaded.loaded_abspath
            and python_loaded.loaded_version
            and python_loaded.loaded_sha256
        ):
            self.write_cached_binary(
                "python",
                python_loaded.loaded_abspath,
                python_loaded.loaded_version,
                python_loaded.loaded_sha256,
                resolved_provider_name=(
                    python_loaded.loaded_binprovider.name
                    if python_loaded.loaded_binprovider is not None
                    else self.name
                ),
                cache_kind="dependency",
            )
        return loaded

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        """create pip venv dir if needed"""
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.install_root,),
                preserve_root=True,
            )
        self._ensure_writable_cache_dir(self.cache_dir)

        if self.install_root:
            self._setup_venv(self.install_root / "venv", no_cache=no_cache)

    def _setup_venv(self, pip_venv: Path, *, no_cache: bool = False) -> None:
        """Create the managed virtualenv and bootstrap pip/setuptools into it."""
        pip_venv.parent.mkdir(parents=True, exist_ok=True)

        # create new venv in pip_venv if it doesn't exist
        venv_pip_path = pip_venv / VENV_BIN_SUBDIR / "python"
        venv_pip_binary_exists = os.path.isfile(venv_pip_path) and os.access(
            venv_pip_path,
            os.X_OK,
        )
        if venv_pip_binary_exists:
            return

        import venv

        venv.create(
            str(pip_venv),
            system_site_packages=False,
            clear=True,
            symlinks=True,
            with_pip=True,
            upgrade_deps=True,
        )
        assert os.path.isfile(venv_pip_path) and os.access(
            venv_pip_path,
            os.X_OK,
        ), f"could not find pip inside venv after creating it: {pip_venv}"

        # Bootstrap pip + setuptools into the newly created venv. We skip
        # security flags here because the venv was just created by Python's
        # own ``venv`` module and we're upgrading its baseline tooling.
        pip_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert pip_abspath
        cache_arg = (
            "--no-cache-dir"
            if no_cache or not self._ensure_writable_cache_dir(self.cache_dir)
            else f"--cache-dir={self.cache_dir}"
        )
        proc = self.exec(
            bin_name=pip_abspath,
            cmd=[
                "install",
                cache_arg,
                "--no-input",
                "--disable-pip-version-check",
                "--quiet",
                "--upgrade",
                "pip",
                "setuptools",
            ],
            quiet=True,
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", ["pip", "setuptools"], proc)

    def _security_flags(
        self,
        install_args: InstallArgs,
        *,
        postinstall_scripts: bool,
        min_release_age: float,
        no_cache: bool = False,
    ) -> list[str]:
        """Build pip ``install`` security flags based on provider config.

        - ``--only-binary :all:`` when ``postinstall_scripts`` is disabled
          (wheels only, no sdist builds — pip's equivalent of ``--no-build``).
        - ``--uploaded-prior-to=<ISO8601>`` when ``min_release_age`` is set
          and pip is new enough to support the flag (pip >= 26.0, see
          pypa/pip#13625). Older pip versions silently skip the flag.
        """
        flags: list[str] = []

        has_only_binary_flag = self._install_args_have_option(
            install_args,
            "--only-binary",
        )
        if not postinstall_scripts and not has_only_binary_flag:
            flags.extend(["--only-binary", ":all:"])

        if min_release_age <= 0:
            return flags

        has_release_age_flag = self._install_args_have_option(
            install_args,
            "--uploaded-prior-to",
        )
        if has_release_age_flag:
            return flags

        installer = self.INSTALLER_BINARY(no_cache=no_cache)
        pip_ver = installer.loaded_version if installer else None
        if pip_ver is None or pip_ver == SemVer((999, 999, 999)):
            return flags
        if pip_ver < _PIP_MIN_RELEASE_AGE_VERSION:  # pyright: ignore[reportOperatorIssue]
            return flags

        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=min_release_age)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        flags.append(f"--uploaded-prior-to={cutoff}")
        return flags

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
        pip_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert pip_abspath
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}>={min_version}"
                if arg
                and not arg.startswith("-")
                and not any(c in arg for c in ">=<!=~")
                else arg
                for arg in install_args
            ]
        assert postinstall_scripts is not None
        assert min_release_age is not None

        security_flags = self._security_flags(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            no_cache=no_cache,
        )
        cache_arg = (
            "--no-cache-dir"
            if no_cache or not self._ensure_writable_cache_dir(self.cache_dir)
            else f"--cache-dir={self.cache_dir}"
        )

        proc = self.exec(
            bin_name=pip_abspath,
            cmd=[
                "install",
                cache_arg,
                "--no-input",
                "--disable-pip-version-check",
                "--quiet",
                *security_flags,
                *install_args,
            ],
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
        pip_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert pip_abspath
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}>={min_version}"
                if arg
                and not arg.startswith("-")
                and not any(c in arg for c in ">=<!=~")
                else arg
                for arg in install_args
            ]
        assert postinstall_scripts is not None
        assert min_release_age is not None

        security_flags = self._security_flags(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            no_cache=no_cache,
        )

        cache_arg = (
            "--no-cache-dir"
            if no_cache or not self._ensure_writable_cache_dir(self.cache_dir)
            else f"--cache-dir={self.cache_dir}"
        )
        proc = self.exec(
            bin_name=pip_abspath,
            cmd=[
                "install",
                cache_arg,
                "--no-input",
                "--disable-pip-version-check",
                "--quiet",
                "--upgrade",
                *security_flags,
                *install_args,
            ],
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
        pip_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert pip_abspath
        install_args = install_args or self.get_install_args(bin_name)

        proc = self.exec(
            bin_name=pip_abspath,
            cmd=[
                "uninstall",
                "--yes",
                *install_args,
            ],
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

        # try searching for the bin_name in BinProvider.PATH first (fastest)
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except ValueError:
            pass

        try:
            installer_binary = self.INSTALLER_BINARY(no_cache=no_cache)
            pip_abspath = installer_binary.loaded_abspath
            assert pip_abspath
        except Exception:
            return None

        if self.install_root:
            managed_pip = self.install_root / "venv" / VENV_BIN_SUBDIR / "pip"
            if pip_abspath != managed_pip:
                return None

        # fallback to using pip show to get the site-packages bin path
        output_lines = (
            self.exec(
                bin_name=pip_abspath,
                cmd=["show", "--no-input", str(bin_name)],
                quiet=False,
                timeout=self.version_timeout,
            )
            .stdout.strip()
            .split("\n")
        )
        # For more information, please refer to <http://unlicense.org/>
        # Location: /Volumes/NVME/Users/squash/Library/Python/3.11/lib/python/site-packages
        # Requires: brotli, certifi, mutagen, pycryptodomex, requests, urllib3, websockets
        # Required-by:
        try:
            location = [line for line in output_lines if line.startswith("Location: ")][
                0
            ].split("Location: ", 1)[-1]
        except IndexError:
            return None
        PATH = str(Path(location).parent.parent.parent / VENV_BIN_SUBDIR)
        abspath = bin_abspath(str(bin_name), PATH=PATH)
        if abspath:
            return TypeAdapter(HostBinPath).validate_python(abspath)
        else:
            return None

    @staticmethod
    def _package_name_from_install_arg(install_arg: str) -> str | None:
        """Extract a bare Python package name from a pip install arg when possible."""
        if not install_arg or install_arg.startswith("-"):
            return None
        if "://" in install_arg:
            return None
        if install_arg.startswith((".", "/", "~")):
            return None
        package_name = re.split(r"[<>=!~;]", install_arg, maxsplit=1)[0]
        package_name = package_name.split("[", 1)[0].strip()
        return package_name or None

    def _package_name_for_bin(self, bin_name: BinName) -> str | None:
        """Pick the owning Python package name used to resolve version/metadata lookups."""
        install_args = self.get_install_args(bin_name, quiet=True)
        for install_arg in install_args:
            package_name = self._package_name_from_install_arg(install_arg)
            if package_name:
                return package_name
        return None

    def get_cache_info(
        self,
        bin_name: BinName,
        abspath: HostBinPath,
    ) -> dict[str, list[Path]] | None:
        cache_info = super().get_cache_info(bin_name, abspath)
        if cache_info is None or self.install_root is None:
            return cache_info

        package_name = self._package_name_for_bin(bin_name)
        if not package_name:
            return cache_info

        normalized_name = package_name.lower().replace("-", "_")
        metadata_files = sorted(
            ((self.install_root / "venv") / "lib").glob(
                f"python*/site-packages/{normalized_name}*.dist-info/METADATA",
            ),
        ) or sorted(
            ((self.install_root / "venv") / "lib").glob(
                f"python*/site-packages/{normalized_name}*.dist-info/PKG-INFO",
            ),
        )
        if metadata_files:
            cache_info["fingerprint_paths"].append(metadata_files[0])
        return cache_info

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
            pip_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert pip_abspath
        except Exception:
            return None

        # fallback to using pip show to get the version (slower)
        package_name = self._package_name_for_bin(bin_name) or str(bin_name)
        output_lines = (
            self.exec(
                bin_name=pip_abspath,
                cmd=["show", "--no-input", package_name],
                quiet=False,
                timeout=timeout,
            )
            .stdout.strip()
            .split("\n")
        )
        try:
            version_str = [
                line for line in output_lines if line.startswith("Version: ")
            ][0].split("Version: ", 1)[-1]
            return SemVer.parse(version_str)
        except Exception:
            return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_pip.py load yt-dlp
    # ./binprovider_pip.py install pip
    # ./binprovider_pip.py get_version pip
    # ./binprovider_pip.py get_abspath pip
    result = pip = PipProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(pip, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)

#!/usr/bin/env python3

__package__ = "abxpkg"

import os
import sys
import json

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator, TypeAdapter, computed_field
from platformdirs import user_cache_path

from .binary import Binary
from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    HostBinPath,
    abxpkg_install_root_default,
    bin_abspath,
)
from .semver import SemVer
from .binprovider import (
    BinProvider,
    EnvProvider,
    env_flag_is_true,
    log_method_call,
    remap_kwargs,
)
from .logging import format_subprocess_output


USER_CACHE_PATH = user_cache_path(
    appname="npm",
    appauthor="abxpkg",
)


class NpmProvider(BinProvider):
    name: BinProviderName = "npm"
    _log_emoji = "📦"
    INSTALLER_BIN: BinName = "npm"

    PATH: PATHStr = ""  # Starts empty; setup_PATH() lazily discovers npm local/global bin dirs. When install_root is set this becomes bin_dir only, otherwise it comes from npm prefix state.
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABXPKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABXPKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    # None = -g global, otherwise it's a path.
    # Default: ABXPKG_NPM_ROOT > ABXPKG_LIB_DIR/npm > None.
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("npm"),
        validation_alias="npm_prefix",
    )
    # detect_euid_to_use() fills this with ``<install_root>/node_modules/.bin`` in managed
    # mode; ambient mode leaves it unset so _load_PATH() discovers npm's real global bins.
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        node_modules_dir = str(self.install_root / "node_modules")
        return {
            "NODE_MODULES_DIR": node_modules_dir,
            "NODE_MODULE_DIR": node_modules_dir,
            "NODE_PATH": ":" + node_modules_dir,
            "npm_config_prefix": str(self.install_root),
        }

    @property
    def cache_dir(self) -> Path:
        """Return npm's shared package cache dir used for install/update mutations."""
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
        main_package = next(
            (arg for arg in install_args if arg and not arg.startswith("-")),
            str(bin_name),
        )
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

        try:
            npm_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert npm_abspath
        except Exception:
            return False

        # npm 11+ supports ``--min-release-age``. Probe ``npm install --help``
        # rather than version-sniffing because the flag was backported to
        # several 10.x releases and the exact version varies by distro.
        proc = self.exec(
            bin_name=npm_abspath,
            cmd=["install", "--help"],
            quiet=True,
            timeout=self.version_timeout,
        )
        help_text = "\n".join(
            part.strip() for part in (proc.stdout, proc.stderr) if part.strip()
        )
        return proc.returncode == 0 and "--min-release-age" in help_text

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        return action in ("install", "update")

    @staticmethod
    def _install_args_have_option(args: InstallArgs, *options: str) -> bool:
        """Return True when install_args already contains any of the requested options."""
        return any(
            arg == option or arg.startswith(f"{option}=")
            for arg in args
            for option in options
        )

    @staticmethod
    def _install_arg_value(args: InstallArgs, *options: str) -> str | None:
        """Return the explicit value for the first matching CLI option in install_args."""
        for idx, arg in enumerate(args):
            for option in options:
                if arg == option and idx + 1 < len(args):
                    return args[idx + 1]
                if arg.startswith(f"{option}="):
                    return arg.split("=", 1)[1]
        return None

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
        """False if install_root is not created yet or if npm binary is not found in PATH"""
        return super().is_valid

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Detect the user (UID) to run as when executing npm."""
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "node_modules" / ".bin"

        return self

    def _load_PATH(self, no_cache: bool = False) -> str:
        """Resolve npm's effective bin PATH from the current local/global npm prefixes."""
        PATH = self.PATH

        if self.bin_dir:
            return self._merge_PATH(self.bin_dir)

        try:
            installer_binary = self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return PATH
        npm_abspath = installer_binary.loaded_abspath
        if not npm_abspath:
            return PATH
        env_provider = EnvProvider(install_root=None, bin_dir=None, euid=self.euid)

        npm_bin_dirs: set[Path] = set()

        # find all local and global npm PATHs
        npm_local_dir = Path(
            env_provider.exec(
                bin_name=npm_abspath,
                cmd=["prefix"],
                quiet=True,
            ).stdout.strip(),
        )

        # start at npm_local_dir and walk up to $HOME (or /), finding all npm bin dirs along the way
        search_dir = npm_local_dir
        stop_if_reached = [str(Path("/")), str(Path("~").expanduser().absolute())]
        num_hops, max_hops = 0, 6
        while num_hops < max_hops and str(search_dir) not in stop_if_reached:
            try:
                npm_bin_dirs.add(list(search_dir.glob("node_modules/.bin"))[0])
                break
            except (IndexError, OSError, Exception):
                # could happen because we dont have permission to access the parent dir, or it's been moved, or many other weird edge cases...
                pass
            search_dir = search_dir.parent
            num_hops += 1

        npm_global_dir = (
            Path(
                env_provider.exec(
                    bin_name=npm_abspath,
                    cmd=["prefix", "-g"],
                    quiet=True,
                ).stdout.strip(),
            )
            / "bin"
        )
        npm_bin_dirs.add(npm_global_dir)

        return self._merge_PATH(*sorted(npm_bin_dirs), PATH=PATH)

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from npm prefix state; install_root mode uses bin_dir only, ambient mode discovers local/global npm bin dirs."""
        self.PATH = self._load_PATH(no_cache=no_cache)
        super().setup_PATH(no_cache=no_cache)

    def INSTALLER_BINARY(self, no_cache: bool = False):
        from . import DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

        loaded = super().INSTALLER_BINARY(no_cache=no_cache)
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

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        """create npm install prefix and node_modules_dir if needed"""
        if not self.PATH:
            self.PATH = self._load_PATH(no_cache=no_cache)
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.install_root, self.bin_dir),
                preserve_root=True,
            )

        self._ensure_writable_cache_dir(self.cache_dir)

        if self.bin_dir:
            self.bin_dir.mkdir(parents=True, exist_ok=True)

    def _build_mutation_args(
        self,
        install_args: InstallArgs,
        *,
        cache_arg: str | None = None,
        postinstall_scripts: bool,
        min_release_age: float,
    ) -> list[str]:
        """Shared ``install``/``update`` CLI args (security flags + prefix)."""
        resolved_cache_arg = cache_arg or (
            "--no-cache"
            if not self._ensure_writable_cache_dir(self.cache_dir)
            else f"--cache={self.cache_dir}"
        )
        explicit_args = [
            "--force",
            "--no-audit",
            "--no-fund",
            "--loglevel=error",
            resolved_cache_arg,
            *install_args,
        ]
        min_release_age_days = f"{min_release_age:g}"
        extra: list[str] = []
        if not postinstall_scripts and not self._install_args_have_option(
            explicit_args,
            "--ignore-scripts",
        ):
            extra.append("--ignore-scripts")
        if min_release_age > 0 and not self._install_args_have_option(
            explicit_args,
            "--min-release-age",
        ):
            extra.append(f"--min-release-age={min_release_age_days}")

        mutation_args = [
            "--force",
            "--no-audit",
            "--no-fund",
            "--loglevel=error",
            resolved_cache_arg,
            *extra,
        ]
        if self.install_root:
            mutation_args.append(f"--prefix={self.install_root}")
        else:
            mutation_args.append("--global")
        return mutation_args

    def _linked_bin_path(self, bin_name: BinName | HostBinPath) -> Path | None:
        """Return the managed shim path for an npm-installed executable, if any."""
        if self.bin_dir is None:
            return None
        return self.bin_dir / str(bin_name)

    def _refresh_bin_link(
        self,
        bin_name: BinName | HostBinPath,
        target: HostBinPath,
    ) -> HostBinPath:
        """Recreate the managed shim symlink pointing at the resolved npm executable."""
        link_path = self._linked_bin_path(bin_name)
        assert link_path is not None, "_refresh_bin_link requires bin_dir to be set"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        # Idempotent refresh: skip when shim already points at target.
        # Rewriting on every load() bumps mtime and churns the inode,
        # which invalidates fingerprint caches unnecessarily.
        if link_path.is_symlink():
            try:
                if link_path.readlink() == Path(target):
                    return TypeAdapter(HostBinPath).validate_python(link_path)
            except OSError:
                pass
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink(missing_ok=True)
        link_path.symlink_to(target)
        return TypeAdapter(HostBinPath).validate_python(link_path)

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
        npm_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert npm_abspath
        assert postinstall_scripts is not None
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
        if self._install_args_have_option(install_args, "--ignore-scripts"):
            postinstall_scripts = False
        explicit_min_release_age = self._install_arg_value(
            install_args,
            "--min-release-age",
        )
        if explicit_min_release_age is not None:
            try:
                min_release_age = float(explicit_min_release_age)
            except ValueError as err:
                raise ValueError(
                    f"{self.__class__.__name__} got invalid --min-release-age value: {explicit_min_release_age!r}",
                ) from err
        else:
            min_release_age = 7.0 if min_release_age is None else min_release_age

        mutation_args = self._build_mutation_args(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        if no_cache and not self._install_args_have_option(
            [*mutation_args, *install_args],
            "--prefer-online",
            "--prefer-offline",
            "--offline",
        ):
            mutation_args.append("--prefer-online")
        proc = self.exec(
            bin_name=npm_abspath,
            cmd=["install", *mutation_args, *install_args],
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
        npm_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert npm_abspath
        assert postinstall_scripts is not None
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
        if self._install_args_have_option(install_args, "--ignore-scripts"):
            postinstall_scripts = False
        explicit_min_release_age = self._install_arg_value(
            install_args,
            "--min-release-age",
        )
        if explicit_min_release_age is not None:
            try:
                min_release_age = float(explicit_min_release_age)
            except ValueError as err:
                raise ValueError(
                    f"{self.__class__.__name__} got invalid --min-release-age value: {explicit_min_release_age!r}",
                ) from err
        else:
            min_release_age = 7.0 if min_release_age is None else min_release_age

        mutation_args = self._build_mutation_args(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        if no_cache and not self._install_args_have_option(
            [*mutation_args, *install_args],
            "--prefer-online",
            "--prefer-offline",
            "--offline",
        ):
            mutation_args.append("--prefer-online")
        mutation_verb = "install" if min_version is not None else "update"
        proc = self.exec(
            bin_name=npm_abspath,
            cmd=[mutation_verb, *mutation_args, *install_args],
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
        npm_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert npm_abspath
        assert postinstall_scripts is not None
        install_args = install_args or self.get_install_args(bin_name)
        if str(bin_name) == "puppeteer" and tuple(install_args) == (
            "puppeteer",
            "@puppeteer/browsers",
        ):
            install_args = ["puppeteer"]
        if self._install_args_have_option(install_args, "--ignore-scripts"):
            postinstall_scripts = False

        cache_arg = (
            "--no-cache"
            if not self._ensure_writable_cache_dir(self.cache_dir)
            else f"--cache={self.cache_dir}"
        )
        explicit_args = [
            "--force",
            "--no-audit",
            "--no-fund",
            "--loglevel=error",
            cache_arg,
            *install_args,
        ]
        uninstall_args = [
            "--force",
            "--no-audit",
            "--no-fund",
            "--loglevel=error",
            cache_arg,
            *(
                ["--ignore-scripts"]
                if (
                    not postinstall_scripts
                    and not self._install_args_have_option(
                        explicit_args,
                        "--ignore-scripts",
                    )
                )
                else []
            ),
        ]
        if self.install_root:
            uninstall_args.append(f"--prefix={self.install_root}")
        else:
            uninstall_args.append("--global")

        proc = self.exec(
            bin_name=npm_abspath,
            cmd=["uninstall", *uninstall_args, *install_args],
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
        # print(self.__class__.__name__, 'on_get_abspath', bin_name)

        # try searching for the bin_name in BinProvider.PATH first (fastest)
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except Exception:
            pass

        try:
            npm_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert npm_abspath
        except Exception:
            return None

        # fallback to using npm show to get alternate binary names based on the package, then try to find those in BinProvider.PATH
        try:
            install_args = self.get_install_args(str(bin_name)) or [str(bin_name)]
            main_package = install_args[
                0
            ]  # assume first package in list is the main one
            package_info = json.loads(
                self.exec(
                    bin_name=npm_abspath,
                    cmd=["show", "--json", main_package, "bin"],
                    timeout=self.version_timeout,
                    quiet=True,
                ).stdout.strip(),
            )
            # { ...
            #   "version": "2.2.3",
            #   "bin": {
            #     "mercury-parser": "cli.js",
            #     "postlight-parser": "cli.js"
            #   },
            #   ...
            # }
            alt_bin_names = (
                package_info.get("bin", package_info)
                if isinstance(package_info, dict)
                else {}
            ).keys()
            for alt_bin_name in alt_bin_names:
                abspath = bin_abspath(
                    alt_bin_name,
                    PATH=str(self.bin_dir) if self.bin_dir else self.PATH,
                )
                if abspath:
                    direct_abspath = TypeAdapter(HostBinPath).validate_python(abspath)
                    if str(alt_bin_name) == str(bin_name) or self.bin_dir is None:
                        return direct_abspath
                    return self._refresh_bin_link(bin_name, direct_abspath)
        except Exception:
            pass
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
            npm_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert npm_abspath
        except Exception:
            return None

        package = None

        # fallback to using npm list to get the installed package version
        try:
            install_args = self.get_install_args(str(bin_name), **context) or [
                str(bin_name),
            ]
            main_package = install_args[
                0
            ]  # assume first package in list is the main one

            # remove the package version if it exists "@postslight/parser@^1.2.3" -> "@postlight/parser"
            if main_package[0] == "@":
                package = "@" + main_package[1:].split("@", 1)[0]
            else:
                package = main_package.split("@", 1)[0]

            # npm list --depth=0 --json --prefix=<prefix> "@postlight/parser"
            # (dont use 'npm info @postlight/parser version', it shows *any* available version, not installed version)
            json_output = self.exec(
                bin_name=npm_abspath,
                cmd=[
                    "list",
                    f"--prefix={self.install_root}"
                    if self.install_root
                    else "--global",
                    "--depth=0",
                    "--json",
                    package,
                ],
                timeout=timeout,
                quiet=True,
            ).stdout.strip()
            # {
            #   "name": "lib",
            #   "dependencies": {
            #     "@postlight/parser": {
            #       "version": "2.2.3",
            #       "overridden": false
            #     }
            #   }
            # }
            package_listing = json.loads(json_output)
            if isinstance(package_listing, list):
                package_listing = package_listing[0] if package_listing else {}
            return package_listing["dependencies"][package]["version"]
        except Exception:
            pass

        try:
            assert package
            root_args = (
                ["root", f"--prefix={self.install_root}"]
                if self.install_root
                else ["root", "--global"]
            )
            modules_dir = Path(
                self.exec(
                    bin_name=npm_abspath,
                    cmd=root_args,
                    timeout=timeout,
                    quiet=True,
                ).stdout.strip(),
            )
            version_str = json.loads(
                (modules_dir / package / "package.json").read_text(),
            )["version"]
            return version_str
        except Exception:
            raise
        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_npm.py load @postlight/parser
    # ./binprovider_npm.py install @postlight/parser
    # ./binprovider_npm.py get_version @postlight/parser
    # ./binprovider_npm.py get_abspath @postlight/parser
    result = npm = NpmProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(npm, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)

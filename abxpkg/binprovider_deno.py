#!/usr/bin/env python3

__package__ = "abxpkg"

import json
import os
import sys
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Self

from platformdirs import user_cache_path
from pydantic import Field, TypeAdapter, computed_field, model_validator

from .base_types import (
    BinName,
    BinProviderName,
    InstallArgs,
    PATHStr,
    abxpkg_cache_dir_default,
    abxpkg_install_root_default,
)
from .binprovider import BinProvider, env_flag_is_true, log_method_call, remap_kwargs
from .logging import format_subprocess_output
from .semver import SemVer


USER_CACHE_PATH = user_cache_path("deno", "abxpkg")


class DenoProvider(BinProvider):
    """Deno runtime + package manager provider.

    ``deno_root`` mirrors ``DENO_INSTALL_ROOT``: when set, ``deno install -g``
    lays out binaries under ``<deno_root>/bin``. ``DENO_DIR`` is always derived
    from the provider cache path instead of being stored as separate config.

    Security:
    - npm lifecycle scripts are *opt-in* in Deno (the opposite of npm).
      ``postinstall_scripts=True`` adds ``--allow-scripts``; the default
      is to skip them.
    - ``--minimum-dependency-age=<minutes>`` for ``min_release_age`` (Deno 2.5+).
    """

    name: BinProviderName = "deno"
    _log_emoji = "🦕"
    INSTALLER_BIN: BinName = "deno"

    PATH: PATHStr = ""  # Starts empty; setup_PATH() lazily uses install_root/bin_dir only, or DENO_INSTALL_ROOT/~/.deno/bin in ambient mode.
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABXPKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABXPKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    # Mirrors $DENO_INSTALL_ROOT, defaults to ~/.deno when None.
    # Default: ABXPKG_DENO_ROOT > ABXPKG_LIB_DIR/deno > None.
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("deno"),
        validation_alias="deno_root",
    )
    # detect_euid_to_use() fills this from install_root/bin in managed mode; ambient mode
    # leaves it unset so setup_PATH() uses DENO_INSTALL_ROOT/~/.deno instead.
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        env: dict[str, str] = {"DENO_TLS_CA_STORE": "system"}
        if self.install_root:
            env["DENO_INSTALL_ROOT"] = str(self.install_root)
        env["DENO_DIR"] = str(self.cache_dir)
        return env

    @property
    def cache_dir(self) -> Path:
        """Return the Deno module cache dir, derived from install_root when managed."""
        # Deno's shared download/build cache is always derived from the
        # install_root when one is pinned, otherwise from the standard
        # platform cache dir.
        if self.install_root is not None:
            return self.install_root / ".cache"
        return abxpkg_cache_dir_default("deno") or Path(USER_CACHE_PATH)

    def supports_min_release_age(self, action, no_cache: bool = False) -> bool:
        if action not in ("install", "update"):
            return False
        threshold = SemVer.parse("2.5.0")
        try:
            installer = self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return False
        version = installer.loaded_version if installer else None
        return bool(version and threshold and version >= threshold)

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        return action in ("install", "update")

    def default_install_args_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> InstallArgs:
        if str(bin_name) == "puppeteer":
            return ("puppeteer", "@puppeteer/browsers")
        if str(bin_name) in {"browsers", "puppeteer-browsers"}:
            return ("@puppeteer/browsers",)
        return TypeAdapter(InstallArgs).validate_python(
            super().default_install_args_handler(bin_name, **context)
            or [str(bin_name)],
        )

    @staticmethod
    def _strip_registry_prefix_pkg(spec: str) -> str:
        """Strip ``@version`` from an npm/jsr spec, preserving ``@scope/name``."""
        if spec.startswith("@") and "/" in spec:
            scope, _, after = spec[1:].partition("/")
            return "@" + scope + "/" + after.split("@", 1)[0]
        return spec.split("@", 1)[0]

    def default_docs_url_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> str | None:
        try:
            install_args = self.get_install_args(str(bin_name), quiet=True)
        except Exception:
            install_args = [str(bin_name)]
        # Prefer explicit registry-prefixed specs first, then any URL, and
        # only fall back to deno.land/x if no specific target was given.
        for arg in install_args or [str(bin_name)]:
            if not arg or arg.startswith("-"):
                continue
            if arg.startswith("npm:"):
                pkg = self._strip_registry_prefix_pkg(arg[4:])
                if pkg:
                    return f"https://www.npmjs.com/package/{pkg}"
            elif arg.startswith("jsr:"):
                pkg = self._strip_registry_prefix_pkg(arg[4:])
                if pkg:
                    return f"https://jsr.io/{pkg}"
            elif "://" in arg:
                return arg
        fallback = self._docs_url_package_name(bin_name)
        if fallback:
            return f"https://deno.land/x/{fallback}"
        return None

    @computed_field
    @property
    def is_valid(self) -> bool:
        return super().is_valid

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Derive Deno's managed bin_dir from install_root when one is configured."""
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "bin"
        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from install_root/bin_dir, or DENO_INSTALL_ROOT/~/.deno/bin in ambient mode."""
        if self.bin_dir:
            self.PATH = self._merge_PATH(self.bin_dir)
        else:
            default_root = (
                Path(
                    os.environ.get("DENO_INSTALL_ROOT")
                    or (Path("~").expanduser() / ".deno"),
                )
                / "bin"
            )
            self.PATH = self._merge_PATH(default_root, PATH=self.PATH)
        super().setup_PATH(no_cache=no_cache)

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
        # Ensure install_root and the derived DENO_DIR cache path exist before
        # deno uses them.
        if self.install_root:
            self.install_root.mkdir(parents=True, exist_ok=True)
            assert self.bin_dir is not None
            self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
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
        if self.bin_dir:
            self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def default_search_handler(
        self,
        bin_name: BinName,
        min_version: SemVer | None = None,
        min_release_age: float | None = None,
        timeout: int | None = None,
        **context,
    ) -> list:
        """Search the npm registry directly (deno install -g supports npm: specifiers).

        # same npm-registry-search implementation copy-pasted across
        # YarnProvider, BunProvider, DenoProvider — each provider owns
        # its own copy so they stay isolated and don't import shared
        # helpers per repo policy.
        """
        from .binary import Binary

        # Deno can install from JSR or npm; we hit the npm registry's search
        # endpoint here so the resulting Binary uses ``npm:<name>`` install_args.
        url = (
            "https://registry.npmjs.org/-/v1/search?text="
            + urllib.parse.quote(str(bin_name))
            + "&size=25"
        )
        results: list = []
        seen: set[str] = set()

        def append_result(pkg: dict) -> None:
            pkg_name = pkg.get("name", "")
            if (
                not pkg_name
                or not (pkg_name[0].isalpha() or pkg_name[0] == "@")
                or pkg_name in seen
                or str(bin_name).lower() not in pkg_name.lower()
            ):
                return
            version_str = pkg.get("version", "")
            description = pkg.get("description", "") or pkg_name
            seen.add(pkg_name)
            results.append(
                Binary(
                    name=pkg_name,
                    description=f"{version_str} - {description}".strip(" -"),
                    binproviders=[self],
                    overrides={self.name: {"install_args": [f"npm:{pkg_name}"]}},
                ),
            )

        try:
            with urllib.request.urlopen(
                url,
                timeout=timeout or self.version_timeout,
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}

        for entry in data.get("objects", []):
            append_result(entry.get("package", {}))

        if str(bin_name) not in seen:
            exact_url = (
                "https://registry.npmjs.org/"
                + urllib.parse.quote(
                    str(bin_name),
                    safe="",
                )
                + "/latest"
            )
            try:
                with urllib.request.urlopen(
                    exact_url,
                    timeout=timeout or self.version_timeout,
                ) as resp:
                    append_result(json.loads(resp.read().decode("utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
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

        cmd: list[str] = ["install"]
        if no_cache:
            cmd.append("--reload")
        # Deno always needs the broad runtime capability set for these CLIs.
        cmd.extend(["--allow-all", "-g"])
        if not any(arg in ("-f", "--force") for arg in install_args):
            cmd.append("--force")
        if not any(
            arg in ("-n", "--name") or arg.startswith("--name=") for arg in install_args
        ):
            cmd.extend(["-n", bin_name])
        if postinstall_scripts and not any(
            arg == "--allow-scripts" or arg.startswith("--allow-scripts=")
            for arg in install_args
        ):
            cmd.append("--allow-scripts")
        if (
            min_release_age is not None
            and min_release_age > 0
            and not any(
                arg == "--minimum-dependency-age"
                or arg.startswith("--minimum-dependency-age=")
                for arg in install_args
            )
        ):
            cmd.append(
                f"--minimum-dependency-age={max(int(min_release_age * 24 * 60), 1)}",
            )
        # Auto-prefix bare names with the default scheme (npm: or jsr:).
        for arg in install_args:
            if (
                arg
                and not arg.startswith(("-", ".", "/"))
                and ":" not in arg.split("/")[0]
            ):
                # Bare package names resolve through the npm registry by default.
                cmd.append(f"npm:{arg}")
            else:
                cmd.append(arg)

        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
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
        # ``deno install -gf`` re-installs from scratch, which is the
        # idiomatic update path for global executables.
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
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        proc = self.exec(
            bin_name=installer_bin,
            cmd=["uninstall", "-g", bin_name],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", [bin_name], proc)
        return True


if __name__ == "__main__":
    # Usage:
    # ./binprovider_deno.py load cowsay
    # ./binprovider_deno.py install cowsay
    result = deno = DenoProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(deno, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)

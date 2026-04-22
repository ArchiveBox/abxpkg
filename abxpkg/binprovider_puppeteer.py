#!/usr/bin/env python3

__package__ = "abxpkg"

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Self
from collections.abc import Iterable

from pydantic import Field, TypeAdapter, computed_field, model_validator

from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abxpkg_install_root_default,
)
from .binary import Binary
from .binprovider import (
    BinProvider,
    EnvProvider,
    env_flag_is_true,
    log_method_call,
    remap_kwargs,
)
from .binprovider_npm import NpmProvider
from .logging import (
    format_command,
    format_subprocess_output,
    get_logger,
    log_subprocess_output,
)
from .semver import SemVer

logger = get_logger(__name__)

CLAUDE_SANDBOX_NO_PROXY = (
    "localhost,127.0.0.1,169.254.169.254,metadata.google.internal,"
    ".svc.cluster.local,.local"
)


class PuppeteerProvider(BinProvider):
    name: BinProviderName = "puppeteer"
    _log_emoji = "🎭"
    INSTALLER_BIN: BinName = "puppeteer-browsers"

    PATH: PATHStr = ""  # Starts empty; setup_PATH() fills it with bin_dir and any install_root/npm helper bins.
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABXPKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(default=None, repr=False)

    # Default: ABXPKG_PUPPETEER_ROOT > ABXPKG_LIB_DIR/puppeteer > None.
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("puppeteer"),
        validation_alias="puppeteer_root",
    )
    # Only set in managed mode: setup()/default_abspath_handler() use it to expose stable
    # browser launch shims under ``<install_root>/bin``; global mode leaves it unset.
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        # In managed mode we pin ``PUPPETEER_CACHE_DIR`` to
        # ``<install_root>/cache``. In unmanaged mode we leave the
        # ambient env (or puppeteer-browsers' own ``~/.cache/puppeteer``
        # default) untouched.
        env: dict[str, str] = {}
        if self.install_root is not None:
            env["PUPPETEER_CACHE_DIR"] = str(self.install_root / "cache")
        # @puppeteer/browsers downloads browsers from
        # storage.googleapis.com. In sandboxed environments the egress
        # proxy's NO_PROXY often includes ``.googleapis.com`` / ``.google.com``,
        # which forces the direct connection — which then fails DNS
        # resolution or times out. Override NO_PROXY / no_proxy to a
        # safe sandbox allowlist so the download goes through the proxy
        # instead. Callers that need their own NO_PROXY can still set it
        # via the CLI override flags; our value only fills in the default.
        ambient_no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
        if not ambient_no_proxy or (
            ".googleapis.com" in ambient_no_proxy.lower()
            or ".google.com" in ambient_no_proxy.lower()
        ):
            env["NO_PROXY"] = CLAUDE_SANDBOX_NO_PROXY
            env["no_proxy"] = CLAUDE_SANDBOX_NO_PROXY
        return env

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        return action in ("install", "update")

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "bin"
        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use with bin_dir and any install_root/npm helper bin dirs."""
        lib_dir = os.environ.get("ABXPKG_LIB_DIR")
        hermetic = self.install_root is not None and (
            not lib_dir
            or not str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
        )
        path_entries: list[Path] = []
        if self.bin_dir is not None:
            path_entries.append(self.bin_dir)
        if hermetic and self.install_root is not None:
            path_entries.append(self.install_root / "npm" / "node_modules" / ".bin")
        if path_entries:
            self.PATH = self._merge_PATH(
                *path_entries,
                PATH=self.PATH,
                prepend=True,
            )
        super().setup_PATH(no_cache=no_cache)

    def INSTALLER_BINARY(self, no_cache: bool = False):
        from . import DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

        # Prefer the puppeteer-browsers bootstrapped by an earlier install
        # under ``<install_root>/npm/node_modules/.bin``. Without this, a
        # fresh provider copy (e.g. the one Binary.load() builds via
        # get_provider_with_overrides) can't locate puppeteer-browsers and
        # ``_list_installed_browsers()`` silently returns empty.
        lib_dir = os.environ.get("ABXPKG_LIB_DIR")
        if (
            self.install_root is not None
            and lib_dir
            and str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
        ):
            local_cli = (
                Path(lib_dir) / "npm" / "node_modules" / ".bin" / self.INSTALLER_BIN
            )
        elif self.install_root is not None:
            local_cli = (
                self.install_root / "npm" / "node_modules" / ".bin" / self.INSTALLER_BIN
            )
        else:
            local_cli = None

        if (
            local_cli is not None
            and local_cli.is_file()
            and os.access(local_cli, os.X_OK)
        ):
            if (
                not no_cache
                and self._INSTALLER_BINARY
                and self._INSTALLER_BINARY.loaded_abspath == local_cli
                and self._INSTALLER_BINARY.is_valid
            ):
                return self._INSTALLER_BINARY
            if not no_cache:
                cached = self.load_cached_binary(self.INSTALLER_BIN, local_cli)
                if cached and cached.loaded_abspath:
                    self._INSTALLER_BINARY = cached
                    return cached
            env_provider = EnvProvider(
                PATH=str(local_cli.parent),
                install_root=None,
                bin_dir=None,
            )
            loaded_local = env_provider.load(
                bin_name=self.INSTALLER_BIN,
                no_cache=no_cache,
            )
            if loaded_local and loaded_local.loaded_abspath:
                if loaded_local.loaded_version and loaded_local.loaded_sha256:
                    self.write_cached_binary(
                        self.INSTALLER_BIN,
                        loaded_local.loaded_abspath,
                        loaded_local.loaded_version,
                        loaded_local.loaded_sha256,
                        resolved_provider_name=(
                            loaded_local.loaded_binprovider.name
                            if loaded_local.loaded_binprovider is not None
                            else self.name
                        ),
                        cache_kind="dependency",
                    )
                self._INSTALLER_BINARY = loaded_local
                return self._INSTALLER_BINARY

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

    def _cli_binary(
        self,
        *,
        postinstall_scripts: bool,
        min_release_age: float,
        no_cache: bool = False,
    ) -> Binary:
        lib_dir = os.environ.get("ABXPKG_LIB_DIR")
        if (
            self.install_root is not None
            and lib_dir
            and str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
        ):
            npm_install_root = Path(lib_dir) / "npm"
        elif self.install_root is not None:
            npm_install_root = self.install_root / "npm"
        else:
            npm_install_root = None
        cli_provider = NpmProvider(
            install_root=npm_install_root,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        return Binary(
            name="puppeteer-browsers",
            binproviders=[cli_provider],
            overrides={"npm": {"install_args": ["@puppeteer/browsers"]}},
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        ).install(no_cache=no_cache)

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version=None,
        no_cache: bool = False,
    ) -> None:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(
                    self.install_root,
                    self.bin_dir,
                    self.install_root / "cache"
                    if self.install_root is not None
                    else None,
                    self.install_root / "npm"
                    if self.install_root is not None
                    else None,
                ),
                preserve_root=True,
            )
        try:
            cached = self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            cached = None
        if cached and cached.loaded_abspath:
            lib_dir = os.environ.get("ABXPKG_LIB_DIR")
            hermetic = self.install_root is not None and (
                not lib_dir
                or not str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
            )
            path_entries: list[Path] = []
            if self.bin_dir is not None:
                path_entries.append(self.bin_dir)
            if hermetic and self.install_root is not None:
                path_entries.append(self.install_root / "npm" / "node_modules" / ".bin")
            if path_entries:
                self.PATH = self._merge_PATH(
                    *path_entries,
                    PATH="",
                    prepend=True,
                )
            return
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
        min_release_age = 0.0 if min_release_age is None else min_release_age

        if self.install_root is not None:
            self.install_root.mkdir(parents=True, exist_ok=True)
            (self.install_root / "cache").mkdir(parents=True, exist_ok=True)
        if self.bin_dir is not None:
            self.bin_dir.mkdir(parents=True, exist_ok=True)

        cli_binary = self._cli_binary(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            no_cache=no_cache,
        )
        self._INSTALLER_BINARY = cli_binary  # bootstrap: seed cache after npm install
        lib_dir = os.environ.get("ABXPKG_LIB_DIR")
        hermetic = self.install_root is not None and (
            not lib_dir
            or not str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
        )
        path_entries: list[Path] = []
        if self.bin_dir is not None:
            path_entries.append(self.bin_dir)
        if hermetic and self.install_root is not None:
            path_entries.append(self.install_root / "npm" / "node_modules" / ".bin")
        if path_entries:
            self.PATH = self._merge_PATH(
                *path_entries,
                PATH="",
                prepend=True,
            )

    def _browser_name(
        self,
        bin_name: str,
        install_args: Iterable[str],
    ) -> str:
        for arg in install_args:
            arg_str = str(arg)
            if arg_str.startswith("-"):
                continue
            return arg_str.split("@", 1)[0]
        return bin_name

    def _normalize_install_args(self, install_args: Iterable[str]) -> list[str]:
        normalized: list[str] = []
        skip_next = False
        for arg in install_args:
            arg_str = str(arg)
            if skip_next:
                skip_next = False
                continue
            if arg_str == "--path":
                skip_next = True
                continue
            if arg_str.startswith("--path="):
                continue
            normalized.append(arg_str)
        if self.install_root is not None:
            normalized.append(f"--path={self.install_root / 'cache'}")
        return normalized

    def _list_installed_browsers(
        self,
        no_cache: bool = False,
    ) -> list[tuple[str, str, Path]]:
        try:
            installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        except Exception:
            return []
        if not installer_bin:
            return []
        cmd = ["list"]
        if self.install_root is not None:
            cmd.append(f"--path={self.install_root / 'cache'}")
        proc = self.exec(
            bin_name=installer_bin,
            cmd=cmd,
            cwd=self.install_root or ".",
            quiet=True,
            timeout=self.version_timeout,
        )
        if proc.returncode != 0:
            return []

        matches: list[tuple[str, str, Path]] = []
        pattern = re.compile(
            r"^(?P<browser>[^@\s]+)@(?P<version>\S+)(?:\s+\([^)]+\))?\s+(?P<path>.+)$",
        )
        for line in proc.stdout.splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            matches.append(
                (
                    match.group("browser"),
                    match.group("version"),
                    Path(match.group("path")),
                ),
            )
        return matches

    def _parse_installed_browser_path(
        self,
        output: str,
        browser_name: str,
    ) -> Path | None:
        pattern = re.compile(
            r"^(?P<browser>[^@\s]+)@(?P<version>\S+)(?:\s+\([^)]+\))?\s+(?P<path>.+)$",
            re.MULTILINE,
        )
        matches = [
            (
                match.group("version"),
                Path(match.group("path")),
            )
            for match in pattern.finditer(output or "")
            if match.group("browser") == browser_name
        ]
        parsed_matches = [
            (parsed_version, path)
            for version, path in matches
            if (parsed_version := SemVer.parse(version)) is not None
        ]
        if parsed_matches:
            return max(parsed_matches, key=lambda item: item[0])[1]
        if len(matches) == 1:
            return matches[0][1]
        return None

    def _resolve_installed_browser_path(
        self,
        bin_name: str,
        install_args: Iterable[str] | None = None,
        no_cache: bool = False,
    ) -> Path | None:
        # Pick up the caller's configured install_args so
        # ``bin_name=chrome`` + ``install_args=["chromium@latest"]``
        # resolves to ``browser_name="chromium"`` (matching what
        # ``puppeteer-browsers list`` reports), instead of falling
        # back to ``[bin_name]`` which would look for the alias name.
        if install_args is None:
            install_args = self.get_install_args(bin_name, quiet=True) or [bin_name]
        browser_name = self._browser_name(bin_name, install_args)
        candidates = [
            (version, path)
            for candidate_browser, version, path in self._list_installed_browsers(
                no_cache=no_cache,
            )
            if candidate_browser == browser_name
        ]
        parsed_candidates = [
            (parsed_version, path)
            for version, path in candidates
            if (parsed_version := SemVer.parse(version)) is not None
        ]
        if parsed_candidates:
            return max(parsed_candidates, key=lambda item: item[0])[1]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0][1]
        # Multiple cached builds but none parse as ``SemVer`` (e.g. chromium's
        # integer build IDs like ``1618539``). Fall back to the newest one
        # by file mtime so post-install lookups land on the freshly-
        # downloaded version rather than ``None``.
        candidates.sort(
            key=lambda item: (
                item[1].stat().st_mtime if item[1].exists() else 0,
                item[0],
            ),
        )
        return candidates[-1][1]

    def _refresh_symlink(self, bin_name: str, target: Path) -> Path:
        bin_dir = self.bin_dir
        assert bin_dir is not None
        link_path = bin_dir / bin_name
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink(missing_ok=True)
        if os.name == "posix" and ".app/Contents/MacOS/" in str(target):
            link_path.write_text(
                f'#!/bin/sh\nexec {shlex.quote(str(target))} "$@"\n',
                encoding="utf-8",
            )
            link_path.chmod(0o755)
            return link_path
        link_path.symlink_to(target)
        return link_path

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        if str(bin_name) == self.INSTALLER_BIN:
            try:
                installer_binary = self.INSTALLER_BINARY(no_cache=no_cache)
                abspath = installer_binary.loaded_abspath if installer_binary else None
                if abspath:
                    return TypeAdapter(HostBinPath).validate_python(abspath)
            except Exception:
                return None
            return None

        # Authoritative lookup: ask puppeteer-browsers where the browser
        # actually lives — never trust the managed ``bin_dir`` shim as a
        # source of truth, it may point at a browser that was removed
        # out-of-band. When the CLI reports nothing, we report nothing.
        resolved = self._resolve_installed_browser_path(str(bin_name))
        if not resolved or not resolved.exists():
            return None

        # Refresh the convenience shim under ``bin_dir`` so ``PATH`` users
        # get a stable entry pointing at the freshly-resolved executable.
        # In global/unmanaged mode (``install_root=None``) we have no
        # managed shim dir, so just return the resolved path directly.
        # When the shim refresh fails (read-only FS etc.) we also fall
        # back to the resolved path.
        if self.bin_dir is None:
            return resolved
        try:
            return self._refresh_symlink(str(bin_name), resolved)
        except OSError:
            return resolved

    def _cleanup_partial_browser_cache(
        self,
        install_output: str,
        browser_name: str,
    ) -> bool:
        if self.install_root is None:
            return False
        cache_dir = self.install_root / "cache"
        targets: set[Path] = set()
        browser_cache_dir = cache_dir / browser_name

        missing_dir_match = re.search(
            r"browser folder \(([^)]+)\) exists but the executable",
            install_output,
        )
        if missing_dir_match:
            targets.add(Path(missing_dir_match.group(1)))

        missing_zip_match = re.search(r"open '([^']+\.zip)'", install_output)
        if missing_zip_match:
            targets.add(Path(missing_zip_match.group(1)))

        build_id_match = re.search(
            rf"All providers failed for {re.escape(browser_name)} (\S+)",
            install_output,
        )
        if build_id_match and browser_cache_dir.exists():
            build_id = build_id_match.group(1)
            targets.update(browser_cache_dir.glob(f"*{build_id}*"))

        removed_any = False
        resolved_cache = cache_dir.resolve(strict=False)
        for target in targets:
            resolved_target = target.resolve(strict=False)
            if not (
                resolved_target == resolved_cache
                or resolved_cache in resolved_target.parents
            ):
                continue
            if target.is_dir():
                logger.info("$ %s", format_command(["rm", "-rf", str(target)]))
                shutil.rmtree(target, ignore_errors=True)
                removed_any = True
            elif target.exists():
                logger.info("$ %s", format_command(["rm", "-f", str(target)]))
                target.unlink(missing_ok=True)
                removed_any = True
        return removed_any

    def _should_repair_cli_install(self, output: str) -> bool:
        lowered = (output or "").lower()
        return (
            "this.shim.parser.camelcase is not a function" in lowered
            or "yargs/build/lib/command.js" in lowered
        )

    def _get_install_failure_hint(self, install_output: str) -> str | None:
        lowered = (install_output or "").lower()
        if (
            "storage.googleapis.com" in lowered
            and "getaddrinfo" in lowered
            and "eai_again" in lowered
        ):
            return (
                "Puppeteer failed to download a browser from storage.googleapis.com. "
                "Override NO_PROXY/no_proxy to remove .googleapis.com and .google.com. "
                f'Example NO_PROXY="{CLAUDE_SANDBOX_NO_PROXY}"'
            )
        return None

    def _has_sudo(self) -> bool:
        try:
            return self._sudo_binary() is not None
        except Exception:
            return False

    def _sudo_binary(self, *, no_cache: bool = False) -> Binary | None:
        return Binary(
            name="sudo",
            binproviders=[
                EnvProvider(postinstall_scripts=True, min_release_age=0),
            ],
            postinstall_scripts=True,
            min_release_age=0,
        ).load(no_cache=no_cache)

    def _run_install_with_sudo(
        self,
        install_args: list[str],
        no_cache: bool = False,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            installer_binary = self._INSTALLER_BINARY
            if installer_binary is None or installer_binary.loaded_abspath is None:
                installer_binary = self.INSTALLER_BINARY(no_cache=no_cache)
            installer_bin = installer_binary.loaded_abspath
            assert installer_bin
        except Exception:
            return None
        sudo_binary = self._sudo_binary()
        if sudo_binary is None or sudo_binary.loaded_abspath is None:
            return None

        proc = self.exec(
            bin_name=sudo_binary.loaded_abspath,
            cmd=["-E", str(installer_bin), "install", *install_args],
            cwd=self.install_root or ".",
            timeout=self.install_timeout,
        )
        if proc.returncode == 0 and self.install_root is not None:
            cache_dir = self.install_root / "cache"
            if cache_dir.exists():
                uid = os.getuid()
                gid = os.getgid()
                chown_proc = self.exec(
                    bin_name=sudo_binary.loaded_abspath,
                    cmd=["chown", "-R", f"{uid}:{gid}", str(cache_dir)],
                    cwd=self.install_root or ".",
                    timeout=30,
                    quiet=True,
                )
                if chown_proc.returncode != 0:
                    log_subprocess_output(
                        logger,
                        f"{self.__class__.__name__} sudo chown",
                        chown_proc.stdout,
                        chown_proc.stderr,
                    )
        return proc

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> str:
        self.setup(no_cache=no_cache)
        if str(bin_name) == self.INSTALLER_BIN:
            return f"Bootstrapped {self.INSTALLER_BIN} via npm"
        install_args = list(install_args or self.get_install_args(bin_name))
        browser_name = self._browser_name(bin_name, install_args)
        normalized_install_args = self._normalize_install_args(install_args)

        if self.dry_run:
            return f"DRY_RUN would install {browser_name} via @puppeteer/browsers"

        installer_binary = self._INSTALLER_BINARY
        if installer_binary is None or installer_binary.loaded_abspath is None:
            installer_binary = self.INSTALLER_BINARY(no_cache=no_cache)
        installer_bin = installer_binary.loaded_abspath
        assert installer_bin
        proc = self.exec(
            bin_name=installer_bin,
            cmd=["install", *normalized_install_args],
            cwd=self.install_root or ".",
            timeout=timeout if timeout is not None else self.install_timeout,
        )

        install_output = f"{proc.stdout}\n{proc.stderr}"
        if (
            proc.returncode != 0
            and "--install-deps" in normalized_install_args
            and "requires root privileges" in install_output
            and os.geteuid() != 0
            and self._has_sudo()
        ):
            sudo_proc = self._run_install_with_sudo(
                normalized_install_args,
                no_cache=no_cache,
            )
            if sudo_proc is not None:
                proc = sudo_proc
                install_output = f"{proc.stdout}\n{proc.stderr}"

        if proc.returncode != 0 and self._should_repair_cli_install(install_output):
            cli_binary = self._cli_binary(postinstall_scripts=True, min_release_age=0)
            self._INSTALLER_BINARY = (
                cli_binary  # bootstrap: seed cache after npm install
            )
            installer_binary = self._INSTALLER_BINARY
            if installer_binary is None or installer_binary.loaded_abspath is None:
                installer_binary = self.INSTALLER_BINARY(no_cache=no_cache)
            installer_bin = installer_binary.loaded_abspath
            assert installer_bin
            proc = self.exec(
                bin_name=installer_bin,
                cmd=["install", *normalized_install_args],
                cwd=self.install_root or ".",
                timeout=timeout if timeout is not None else self.install_timeout,
            )
            install_output = f"{proc.stdout}\n{proc.stderr}"

        if proc.returncode != 0 and self._cleanup_partial_browser_cache(
            install_output,
            browser_name,
        ):
            proc = self.exec(
                bin_name=installer_bin,
                cmd=["install", *normalized_install_args],
                cwd=self.install_root or ".",
                timeout=timeout if timeout is not None else self.install_timeout,
            )
            install_output = f"{proc.stdout}\n{proc.stderr}"

        if proc.returncode != 0:
            install_hint = self._get_install_failure_hint(install_output)
            if install_hint:
                raise RuntimeError(install_hint) from None
            self._raise_proc_error("install", bin_name, proc)

        installed_path = self._parse_installed_browser_path(
            install_output,
            browser_name,
        )
        installed_path = installed_path or self._resolve_installed_browser_path(
            bin_name,
            install_args,
            no_cache=no_cache,
        )
        if not installed_path or not installed_path.exists():
            raise FileNotFoundError(
                f"{self.__class__.__name__} could not resolve installed browser path for {bin_name}",
            )

        if self.bin_dir is not None:
            self._refresh_symlink(bin_name, installed_path)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> str:
        return self.default_install_handler(
            bin_name,
            install_args=install_args,
            timeout=timeout,
            no_cache=no_cache,
            **context,
        )

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> bool:
        # Resolve the real browser directory via the CLI directly
        # (``_resolve_installed_browser_path`` shells out to
        # ``puppeteer-browsers list``) so we don't round-trip through
        # ``load()`` → ``default_abspath_handler`` and have it refresh
        # the managed shim right as we're about to delete it. Honours
        # managed ``install_root``, ambient ``PUPPETEER_CACHE_DIR``,
        # and puppeteer-browsers' own default uniformly.
        install_args = list(install_args or self.get_install_args(bin_name))
        browser_name = self._browser_name(bin_name, install_args)
        resolved = self._resolve_installed_browser_path(str(bin_name))
        if resolved is not None:
            for parent in Path(resolved).resolve().parents:
                if parent.name == browser_name:
                    logger.info("$ %s", format_command(["rm", "-rf", str(parent)]))
                    shutil.rmtree(parent, ignore_errors=True)
                    break

        # Finally, drop the convenience shim under ``bin_dir``. Doing
        # this last avoids the "unlink → load() refresh → rmtree →
        # dangling shim" ordering bug.
        if self.bin_dir is not None:
            bin_path = self.bin_dir / bin_name
            if bin_path.exists() or bin_path.is_symlink():
                logger.info("$ %s", format_command(["rm", "-f", str(bin_path)]))
            bin_path.unlink(missing_ok=True)
        return True

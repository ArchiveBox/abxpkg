#!/usr/bin/env python3

__package__ = "abxpkg"

import json
import os
import re
import shutil
import urllib.request
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import Field, computed_field, model_validator

from .base_types import (
    DEFAULT_ABXPKG_LIB_DIR,
    BinName,
    BinProviderName,
    PATHStr,
    abxpkg_install_root_default,
    bin_abspath,
)
from .binprovider import (
    BinProvider,
    BinProviderOverrides,
    env_flag_is_true,
    log_method_call,
    remap_kwargs,
)
from .logging import format_command, format_subprocess_output, get_logger

logger = get_logger(__name__)


# Ultimate fallback when neither the constructor arg nor
# ``ABXPKG_CHROMEWEBSTORE_ROOT`` nor ``ABXPKG_LIB_DIR`` is set.
DEFAULT_CHROMEWEBSTORE_ROOT = DEFAULT_ABXPKG_LIB_DIR / "chromewebstore"
CHROMEWEBSTORE_UTILS_PATH = Path(__file__).with_name("chromewebstore_utils.js")


class ChromeWebstoreProvider(BinProvider):
    name: BinProviderName = "chromewebstore"
    _log_emoji = "🧩"
    INSTALLER_BIN: BinName = "node"
    INSTALLER_BINPROVIDERS: ClassVar[tuple[BinProviderName, ...] | None] = ("env",)

    PATH: PATHStr = ""  # Intentionally unused for resolution; extension wrappers resolve from bin_dir directly and installers resolve from ambient env.
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABXPKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(default=None, repr=False)

    # Default: ABXPKG_CHROMEWEBSTORE_ROOT > ABXPKG_LIB_DIR/chromewebstore > None.
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("chromewebstore"),
        validation_alias="extensions_root",
    )
    # detect_euid_to_use() fills this with the managed extensions dir and the install/
    # uninstall handlers read it for unpacked CRX payloads plus ``*.extension.json`` metadata.
    bin_dir: Path | None = Field(default=None, validation_alias="extensions_dir")
    overrides: BinProviderOverrides = {
        "*": {
            "abspath": "self.chromewebstore_abspath_handler",
            "version": "self.chromewebstore_version_handler",
            "install_args": "self.chromewebstore_install_args_handler",
            "install": "self.chromewebstore_install_handler",
            "update": "self.chromewebstore_install_handler",
            "uninstall": "self.chromewebstore_uninstall_handler",
            "docs_url": "self.default_docs_url_handler",
            "search": "self.chromewebstore_search_handler",
        },
    }

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.bin_dir:
            return {}
        return {"CHROMEWEBSTORE_EXTENSIONS_DIR": str(self.bin_dir)}

    @computed_field
    @property
    def is_valid(self) -> bool:
        return bool(
            (
                bin_abspath(self.INSTALLER_BIN, PATH=self.PATH)
                or bin_abspath(self.INSTALLER_BIN)
            )
            and CHROMEWEBSTORE_UTILS_PATH.exists(),
        )

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Fill in the managed extension cache root and unpacked extensions dir."""
        if self.install_root is None:
            self.install_root = DEFAULT_CHROMEWEBSTORE_ROOT
        if self.bin_dir is None:
            self.bin_dir = self.install_root / "extensions"
        return self

    def supports_postinstall_disable(self, action, no_cache: bool = False) -> bool:
        return True

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version=None,
        no_cache: bool = False,
    ) -> None:
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(bin_dir, install_root),
                preserve_root=True,
            )
        install_root.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)

    def chromewebstore_install_args_handler(
        self,
        bin_name: str,
        **context,
    ) -> list[str]:
        """Default to ``<webstore_id> --name=<bin_name>`` install args for extensions."""
        return [bin_name, f"--name={bin_name}"]

    @staticmethod
    def _docs_url_name_slug(name: str) -> str:
        """Slugify an extension name into the URL-safe form used by the Web Store."""
        cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
        # collapse runs of hyphens and trim leading/trailing ones
        while "--" in cleaned:
            cleaned = cleaned.replace("--", "-")
        return cleaned.strip("-")

    def default_docs_url_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> str | None:
        # Prefer the cached webstore_id/extension_name if we have them (set
        # after install); otherwise fall back to install_args, which is the
        # configured ``[<webstore_id>, --name=<extension_name>]`` pair.
        try:
            install_args = list(self.get_install_args(bin_name, quiet=True))
        except Exception:
            install_args = []
        cached = self._cached_extension(str(bin_name), install_args)
        webstore_id = str(
            cached.get("webstore_id") or (install_args[0] if install_args else ""),
        ).strip()
        if not webstore_id:
            return None
        extension_name = str(
            cached.get("name")
            or cached.get("extension_name")
            or self._extension_name(str(bin_name), install_args),
        )
        slug = self._docs_url_name_slug(extension_name)
        if slug and slug != webstore_id:
            return f"https://chromewebstore.google.com/detail/{slug}/{webstore_id}"
        return f"https://chromewebstore.google.com/detail/{webstore_id}"

    def chromewebstore_search_handler(
        self,
        bin_name: BinName,
        min_version=None,
        min_release_age=None,
        timeout: int | None = None,
        **context,
    ) -> list:
        """Resolve a Chrome Web Store extension by its 32-char ID.

        The Web Store has no JSON search API, but the public detail page
        ``https://chromewebstore.google.com/detail/<id>`` always returns
        the canonical extension name in its ``<title>`` tag, so we hit
        that to translate an extension ID into a human-readable name.
        Non-ID queries return an empty list — ID-based lookup is the
        only thing the Web Store reliably exposes.
        """
        from .binary import Binary

        query = str(bin_name).strip()
        if not re.fullmatch(r"[a-p]{32}", query):
            return []
        url = f"https://chromewebstore.google.com/detail/{query}"
        try:
            with urllib.request.urlopen(
                url,
                timeout=timeout or self.version_timeout,
            ) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception:
            return []
        match = re.search(r"<title>([^<]+?)\s*-\s*Chrome Web Store</title>", html)
        if not match:
            return []
        extension_name = match.group(1).strip()
        # Use the stable webstore ID as ``Binary.name`` (BinName accepts
        # only filename-safe chars/length); the human title goes in
        # ``description`` and the ``--name=`` install_arg so install
        # writes shims/metadata under the title-derived alias.
        return [
            Binary(
                name=query,
                description=f"{extension_name} ({query})",
                binproviders=[self],
                overrides={
                    self.name: {
                        "install_args": [query, f"--name={extension_name}"],
                    },
                },
            ),
        ]

    def _cached_extension(
        self,
        bin_name: str,
        install_args: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Load the persisted extension metadata JSON for a cached extension, if any."""
        bin_dir = self.bin_dir
        assert bin_dir is not None
        cache_path = bin_dir / f"{bin_name}.extension.json"
        if not cache_path.exists():
            return {}
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(cached, dict):
            return {}
        requested_webstore_id = str(
            (install_args or self.get_install_args(bin_name, quiet=True) or [bin_name])[
                0
            ],
        )
        if str(cached.get("webstore_id") or "") != requested_webstore_id:
            return {}
        return cached

    def _extension_name(self, bin_name: str, install_args: list[str]) -> str:
        """Resolve the human-friendly extension name from install args or the bin_name."""
        if len(install_args) > 1:
            raw_name = str(install_args[1])
            if raw_name.startswith("--name="):
                return raw_name.split("=", 1)[1] or bin_name
            return raw_name
        return bin_name

    def _extension_spec(self, bin_name: str) -> tuple[str, str, Path, Path, Path]:
        """Return the cached extension id/name and the derived on-disk paths for it."""
        bin_dir = self.bin_dir
        assert bin_dir is not None
        install_args = list(self.get_install_args(bin_name, quiet=True))
        cached = self._cached_extension(bin_name, install_args)
        webstore_id = str(
            cached["webstore_id"]
            if "webstore_id" in cached
            else (install_args[0] if install_args else bin_name),
        )
        extension_name = str(
            cached["name"]
            if "name" in cached
            else self._extension_name(bin_name, install_args),
        )
        unpacked_path = Path(
            cached["unpacked_path"]
            if "unpacked_path" in cached
            else (bin_dir / f"{webstore_id}__{extension_name}"),
        )
        crx_path = Path(
            cached["crx_path"]
            if "crx_path" in cached
            else (bin_dir / f"{webstore_id}__{extension_name}.crx"),
        )
        manifest_path = unpacked_path / "manifest.json"
        return webstore_id, extension_name, unpacked_path, crx_path, manifest_path

    def _sanitize_unpacked_extension(self, unpacked_path: Path) -> None:
        # Chrome Web Store CRX payloads include `_metadata` for signed store installs,
        # but CDP Extensions.loadUnpacked rejects it. Keep this in the provider so
        # every consumer gets one stable, loadable unpacked artifact instead of
        # copying extensions into runtime-specific temp dirs.
        signed_store_metadata = unpacked_path / "_metadata"
        if signed_store_metadata.exists():
            shutil.rmtree(signed_store_metadata, ignore_errors=True)

    def chromewebstore_abspath_handler(self, bin_name: str, **context) -> str | None:
        """Resolve an installed extension to its shared metadata file.

        Chrome Web Store extensions are directory payloads, but abxpkg's
        loaded_abspath contract is a readable file. The metadata JSON is the
        stable file runtime hooks already scan, and it points at the real
        unpacked extension directory through ``unpacked_path``.
        """
        bin_dir = self.bin_dir
        assert bin_dir is not None
        _, _, unpacked_path, _, manifest_path = self._extension_spec(bin_name)
        cache_path = bin_dir / f"{bin_name}.extension.json"
        if manifest_path.exists():
            self._sanitize_unpacked_extension(unpacked_path)
            return str(cache_path)
        return None

    def chromewebstore_version_handler(
        self,
        bin_name: str,
        abspath: str | Path | None = None,
        **context,
    ) -> str | None:
        """Read the installed extension version from its unpacked manifest.json."""
        manifest_path = (
            Path(abspath) if abspath else self.get_abspath(bin_name, quiet=True)
        )
        if not manifest_path or not Path(manifest_path).exists():
            return None
        if Path(manifest_path).name.endswith(".extension.json"):
            cached = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            manifest_path = Path(cached["unpacked_path"]) / "manifest.json"
            if not manifest_path.exists():
                return None
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        return str(manifest.get("version") or "")

    @remap_kwargs({"packages": "install_args"})
    def chromewebstore_install_handler(
        self,
        bin_name: str,
        install_args: list[str] | tuple[str, ...] | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> str:
        """Download, unpack, and cache a Chrome Web Store extension via the packaged JS helper."""
        install_args = list(install_args or self.get_install_args(bin_name))
        if self.dry_run:
            return f"DRY_RUN would install Chrome Web Store extension {bin_name}"

        webstore_id = str(install_args[0] if install_args else bin_name)
        extension_name = self._extension_name(bin_name, install_args)
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        from .binary import Binary

        unzip = Binary(name="unzip").install(no_cache=no_cache)
        if not unzip.loaded_abspath:
            raise RuntimeError("abxpkg could not resolve or install unzip")
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                str(CHROMEWEBSTORE_UTILS_PATH),
                "installExtensionWithCache",
                webstore_id,
                extension_name,
                str(bin_dir),
                str(unzip.loaded_abspath),
                *(["--no-cache"] if no_cache else []),
            ],
            cwd=install_root,
            timeout=timeout if timeout is not None else self.install_timeout,
            env={
                **os.environ,
                # Make node 22+ honor HTTP(S)_PROXY env vars when fetching
                # extensions; ``undici``'s ``fetch`` does not consult them
                # without this flag, which silently breaks downloads on any
                # host that runs behind an outbound HTTP proxy.
                "NODE_USE_ENV_PROXY": "1",
            },
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", bin_name, proc)

        cache_path = bin_dir / f"{bin_name}.extension.json"
        if not cache_path.exists():
            raise FileNotFoundError(
                f"{self.__class__.__name__} did not produce cache metadata at {cache_path}",
            )

        _, _, unpacked_path, _, _ = self._extension_spec(bin_name)
        self._sanitize_unpacked_extension(unpacked_path)

        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def chromewebstore_uninstall_handler(
        self,
        bin_name: str,
        install_args: list[str] | tuple[str, ...] | None = None,
        **context,
    ) -> bool:
        """Remove the cached metadata, CRX file, and unpacked extension directory."""
        bin_dir = self.bin_dir
        assert bin_dir is not None
        cache_path = bin_dir / f"{bin_name}.extension.json"
        _, _, unpacked_path, crx_path, _ = self._extension_spec(bin_name)

        if cache_path.exists():
            cache_path.unlink(missing_ok=True)
        if crx_path.exists():
            crx_path.unlink(missing_ok=True)
        if unpacked_path.exists():
            logger.info("$ %s", format_command(["rm", "-rf", str(unpacked_path)]))
            shutil.rmtree(unpacked_path, ignore_errors=True)
        return True

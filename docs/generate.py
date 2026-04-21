#!/usr/bin/env python3
"""Build the abxpkg landing page (GitHub Pages)."""

from __future__ import annotations

import argparse
import inspect
import os
import shutil
import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from pydantic.fields import PydanticUndefined

import abxpkg
from abxpkg.binprovider import DEFAULT_ENV_PATH, BinProvider
from abxpkg.base_types import DEFAULT_LIB_DIR


SITE_DIR = Path(__file__).resolve().parent
REPO_ROOT = SITE_DIR.parent
TEMPLATE_DIR = SITE_DIR
DEFAULT_OUTPUT_DIR = SITE_DIR
ASSETS_DIR = SITE_DIR / "css"
GITHUB_REPO = "https://github.com/ArchiveBox/abxpkg"
DEFAULT_GITHUB_REF = os.environ.get("ABXPKG_GITHUB_REF", "main")
HOME_DIR = Path.home()

# Categories for grouping providers on the landing page.
CATEGORY_FALLBACK = "fallback"
CATEGORY_SYSTEM = "system"
CATEGORY_LANGUAGE = "language"
CATEGORY_BROWSER = "browser"
CATEGORY_DRIVER = "driver"
CATEGORY_SCRIPT = "script"

CATEGORY_LABELS = {
    CATEGORY_FALLBACK: "PATH Fallback",
    CATEGORY_SYSTEM: "System / OS",
    CATEGORY_LANGUAGE: "Language Ecosystem",
    CATEGORY_BROWSER: "Browser Runtime",
    CATEGORY_DRIVER: "Orchestrator",
    CATEGORY_SCRIPT: "Shell Scripts",
}

CATEGORY_ORDER = [
    CATEGORY_FALLBACK,
    CATEGORY_SYSTEM,
    CATEGORY_LANGUAGE,
    CATEGORY_BROWSER,
    CATEGORY_SCRIPT,
    CATEGORY_DRIVER,
]


# Hand-curated metadata that enriches what can be introspected at runtime.
# Keyed by the provider's ``.name`` (lower-case short name).
PROVIDER_METADATA: dict[str, dict[str, Any]] = {
    "env": {
        "emoji": "🌍",
        "display_title": "EnvProvider",
        "category": CATEGORY_FALLBACK,
        "summary": (
            "Read-only fallback that searches the existing $PATH for binaries "
            "already installed on the host. Does not install anything."
        ),
        "tags": ["path-only", "no-install", "read-only"],
        "source_file": "abxpkg/binprovider.py",
        "example_binary": "curl",
        "example_arg": "--version",
    },
    "apt": {
        "emoji": "🐧",
        "display_title": "AptProvider",
        "category": CATEGORY_SYSTEM,
        "summary": (
            "Installs packages via Debian / Ubuntu's apt-get. Always runs as root "
            "and targets the host package database. Shells out to apt-get directly."
        ),
        "tags": ["linux", "root", "no-hermetic"],
        "source_file": "abxpkg/binprovider_apt.py",
        "example_binary": "wget",
    },
    "brew": {
        "emoji": "🍺",
        "display_title": "BrewProvider",
        "category": CATEGORY_SYSTEM,
        "summary": (
            "Installs packages via Homebrew on macOS/Linuxbrew. Uses the host "
            "brew prefix for discovery and shells out to brew directly. No "
            "isolated hermetic Homebrew cellar."
        ),
        "tags": ["macos", "linuxbrew", "postinstall-opt-in"],
        "source_file": "abxpkg/binprovider_brew.py",
        "example_binary": "yt-dlp",
    },
    "pip": {
        "emoji": "🐍",
        "display_title": "PipProvider",
        "category": CATEGORY_LANGUAGE,
        "summary": (
            "Installs Python wheels via pip. Set pip_venv for a hermetic venv "
            "(auto-created on first use). Honors PIP_BINARY. Supports wheels-only "
            "installs via postinstall_scripts=False."
        ),
        "tags": ["python", "venv-support", "security-controls"],
        "source_file": "abxpkg/binprovider_pip.py",
        "example_binary": "yt-dlp",
    },
    "uv": {
        "emoji": "🚀",
        "display_title": "UvProvider",
        "category": CATEGORY_LANGUAGE,
        "summary": (
            "Installs Python packages via uv. Two modes: hermetic venv mode "
            "(install_root=Path(...)) or global tool mode (uv tool install). Full "
            "support for postinstall_scripts=False and min_release_age."
        ),
        "tags": ["python", "venv-support", "tool-mode", "security-controls"],
        "source_file": "abxpkg/binprovider_uv.py",
        "example_binary": "ruff",
    },
    "npm": {
        "emoji": "📦",
        "display_title": "NpmProvider",
        "category": CATEGORY_LANGUAGE,
        "summary": (
            "Installs JavaScript packages via npm. Set npm_prefix for a hermetic "
            "prefix under <prefix>/node_modules/.bin. Supports both "
            "postinstall_scripts=False and min_release_age (on npm builds that "
            "ship --min-release-age)."
        ),
        "tags": ["javascript", "prefix-support", "security-controls"],
        "source_file": "abxpkg/binprovider_npm.py",
        "example_binary": "prettier",
    },
    "pnpm": {
        "emoji": "📦",
        "display_title": "PnpmProvider",
        "category": CATEGORY_LANGUAGE,
        "summary": (
            "Installs JavaScript packages via pnpm. Set pnpm_prefix for a hermetic "
            "prefix. PNPM_HOME is auto-populated so pnpm add -g works without "
            "polluting the user's shell config. Requires pnpm 10.16+ for "
            "min_release_age."
        ),
        "tags": ["javascript", "prefix-support", "security-controls"],
        "source_file": "abxpkg/binprovider_pnpm.py",
        "example_binary": "prettier",
    },
    "yarn": {
        "emoji": "🧶",
        "display_title": "YarnProvider",
        "category": CATEGORY_LANGUAGE,
        "summary": (
            "Installs JavaScript packages via Yarn 4 / Yarn Berry. Always operates "
            "inside a workspace dir (auto-initialized with a stub package.json and "
            ".yarnrc.yml using nodeLinker: node-modules). Yarn 4.10+ required for "
            "security flags."
        ),
        "tags": ["javascript", "workspace", "security-controls"],
        "source_file": "abxpkg/binprovider_yarn.py",
        "example_binary": "prettier",
    },
    "bun": {
        "emoji": "🥖",
        "display_title": "BunProvider",
        "category": CATEGORY_LANGUAGE,
        "summary": (
            "Installs JavaScript packages via Bun. Set bun_prefix to mirror "
            "$BUN_INSTALL. Supports both min_release_age (Bun 1.3+) and "
            "postinstall_scripts=False via --ignore-scripts."
        ),
        "tags": ["javascript", "prefix-support", "security-controls"],
        "source_file": "abxpkg/binprovider_bun.py",
        "example_binary": "prettier",
    },
    "deno": {
        "emoji": "🦕",
        "display_title": "DenoProvider",
        "category": CATEGORY_LANGUAGE,
        "summary": (
            "Installs JavaScript/TypeScript packages via Deno. Mirrors "
            "$DENO_INSTALL_ROOT. Deno's npm lifecycle scripts are opt-in "
            "(reverse of npm). Supports min_release_age on Deno 2.5+."
        ),
        "tags": ["javascript", "typescript", "security-controls"],
        "source_file": "abxpkg/binprovider_deno.py",
        "example_binary": "prettier",
    },
    "cargo": {
        "emoji": "🦀",
        "display_title": "CargoProvider",
        "category": CATEGORY_LANGUAGE,
        "summary": (
            "Installs Rust crates via cargo install. Set cargo_root or use "
            "cargo_home (default $CARGO_HOME or ~/.cargo). Passes --locked by "
            "default; min_version becomes cargo install --version >=..."
        ),
        "tags": ["rust", "hermetic-support"],
        "source_file": "abxpkg/binprovider_cargo.py",
        "example_binary": "ripgrep",
    },
    "gem": {
        "emoji": "💎",
        "display_title": "GemProvider",
        "category": CATEGORY_LANGUAGE,
        "summary": (
            "Installs Ruby gems via gem install. Set gem_home / gem_bindir for "
            "hermetic installs. Generated wrapper scripts are patched to activate "
            "the configured GEM_HOME instead of the host default."
        ),
        "tags": ["ruby", "hermetic-support"],
        "source_file": "abxpkg/binprovider_gem.py",
        "example_binary": "jekyll",
    },
    "goget": {
        "emoji": "🐹",
        "display_title": "GoGetProvider",
        "category": CATEGORY_LANGUAGE,
        "summary": (
            "Installs Go binaries via go install. Set gopath or gobin for a "
            "hermetic workspace. Default install arg is <bin_name>@latest. Note: "
            "the provider name is goget, not go_get."
        ),
        "tags": ["go", "hermetic-support"],
        "source_file": "abxpkg/binprovider_goget.py",
        "example_binary": "golangci-lint",
    },
    "nix": {
        "emoji": "❄️",
        "display_title": "NixProvider",
        "category": CATEGORY_SYSTEM,
        "summary": (
            "Installs packages via nix profile install using flakes. Set "
            "nix_profile for a custom profile, nix_state_dir for isolated "
            "state/cache. Default install arg is nixpkgs#<bin_name>."
        ),
        "tags": ["nix", "flakes", "hermetic-support"],
        "source_file": "abxpkg/binprovider_nix.py",
        "example_binary": "ffmpeg",
    },
    "docker": {
        "emoji": "🐳",
        "display_title": "DockerProvider",
        "category": CATEGORY_SYSTEM,
        "summary": (
            "Pulls Docker images and writes a local shim wrapper that runs "
            "docker run .... Binary version is parsed from the image tag, so "
            "semver-like tags work best. Pass image refs as install_args."
        ),
        "tags": ["docker", "image", "shim"],
        "source_file": "abxpkg/binprovider_docker.py",
        "example_binary": "hello-world",
    },
    "chromewebstore": {
        "emoji": "🧩",
        "display_title": "ChromeWebstoreProvider",
        "category": CATEGORY_BROWSER,
        "summary": (
            "Downloads, unpacks, and caches Chrome Web Store extensions using the "
            "packaged JS runtime under abxpkg/js/chrome/. The resolved binary path "
            "is the unpacked manifest.json."
        ),
        "tags": ["chrome", "extensions"],
        "source_file": "abxpkg/binprovider_chromewebstore.py",
        "example_binary": "ublock-origin",
    },
    "puppeteer": {
        "emoji": "🎭",
        "display_title": "PuppeteerProvider",
        "category": CATEGORY_BROWSER,
        "summary": (
            "Bootstraps @puppeteer/browsers through NpmProvider, then uses its CLI "
            "to install managed browsers. Resolution uses semantic version ordering, "
            "not lexicographic string sorting."
        ),
        "tags": ["browsers", "npm-bootstrap"],
        "source_file": "abxpkg/binprovider_puppeteer.py",
        "example_binary": "chrome",
    },
    "playwright": {
        "emoji": "🎬",
        "display_title": "PlaywrightProvider",
        "category": CATEGORY_BROWSER,
        "summary": (
            "Bootstraps playwright via npm, runs playwright install --with-deps, "
            "then symlinks resolved browser executables into bin_dir. Pinning "
            "playwright_root also pins PLAYWRIGHT_BROWSERS_PATH. Defaults euid=0 "
            "so --with-deps can install system packages."
        ),
        "tags": ["browsers", "npm-bootstrap", "sudo"],
        "source_file": "abxpkg/binprovider_playwright.py",
        "example_binary": "chromium",
    },
    "bash": {
        "emoji": "🧪",
        "display_title": "BashProvider",
        "category": CATEGORY_SCRIPT,
        "summary": (
            "Runs literal shell-script overrides for install/update/uninstall. "
            "Exports INSTALL_ROOT, BIN_DIR, BASH_INSTALL_ROOT, and BASH_BIN_DIR "
            "into the shell environment for those commands."
        ),
        "tags": ["shell", "literal-overrides"],
        "source_file": "abxpkg/binprovider_bash.py",
        "example_binary": "custom-script",
    },
    "pyinfra": {
        "emoji": "🛠️",
        "display_title": "PyinfraProvider",
        "category": CATEGORY_DRIVER,
        "summary": (
            "Delegates installs to pyinfra operations. installer_module='auto' "
            "resolves to operations.brew.packages on macOS and "
            "operations.server.packages on Linux. No hermetic prefix support."
        ),
        "tags": ["driver", "infra"],
        "source_file": "abxpkg/binprovider_pyinfra.py",
        "example_binary": "wget",
    },
    "ansible": {
        "emoji": "📘",
        "display_title": "AnsibleProvider",
        "category": CATEGORY_DRIVER,
        "summary": (
            "Delegates installs via ansible-runner. installer_module='auto' "
            "resolves to community.general.homebrew on macOS and "
            "ansible.builtin.package on Linux. No hermetic prefix support."
        ),
        "tags": ["driver", "infra"],
        "source_file": "abxpkg/binprovider_ansible.py",
        "example_binary": "wget",
    },
}


# Global env vars (apply to more than one provider or to the CLI).
GLOBAL_ENV_VARS: list[dict[str, str]] = [
    {
        "name": "ABXPKG_DRY_RUN",
        "default": "0",
        "description": (
            "Flips the shared dry_run default. Provider subprocesses are logged "
            "and skipped, install()/update() return a placeholder, uninstall() "
            "returns True. Beats DRY_RUN if both are set."
        ),
    },
    {
        "name": "DRY_RUN",
        "default": "0",
        "description": (
            "Alternative dry-run toggle. ABXPKG_DRY_RUN wins if both are set."
        ),
    },
    {
        "name": "ABXPKG_NO_CACHE",
        "default": "0",
        "description": (
            "Flips the shared no_cache default. install() skips the initial "
            "load() check and forces a fresh install path; load()/update()/"
            "uninstall() bypass cached probe results."
        ),
    },
    {
        "name": "ABXPKG_INSTALL_TIMEOUT",
        "default": "120",
        "description": (
            "Seconds to wait for install()/update()/uninstall() handler subprocesses."
        ),
    },
    {
        "name": "ABXPKG_VERSION_TIMEOUT",
        "default": "10",
        "description": (
            "Seconds to wait for version / metadata probes (--version, npm show, "
            "pip show, etc.)."
        ),
    },
    {
        "name": "ABXPKG_POSTINSTALL_SCRIPTS",
        "default": "unset",
        "description": (
            "Hydrates the provider-level default for postinstall_scripts on "
            "supporting providers (pip, uv, npm, pnpm, yarn, bun, deno, brew, "
            "chromewebstore, puppeteer). When unset, action execution falls back "
            "to the provider/action default."
        ),
    },
    {
        "name": "ABXPKG_MIN_RELEASE_AGE",
        "default": "7",
        "description": (
            "Hydrates the provider-level minimum release age (days) on supporting "
            "providers (pip, uv, npm, pnpm, yarn, bun, deno). When unset, action "
            "execution falls back to the provider/action default."
        ),
    },
    {
        "name": "ABXPKG_BINPROVIDERS",
        "default": "DEFAULT_PROVIDER_NAMES",
        "description": (
            "Comma-separated provider names to enable (and their order) for the "
            "abxpkg CLI. Defaults to DEFAULT_PROVIDER_NAMES from abxpkg.__init__. "
            "Example: env,uv,pip,apt,brew"
        ),
    },
    {
        "name": "ABXPKG_LIB_DIR",
        "default": "<platform default abx lib dir>",
        "description": (
            "Centralized library root. When set, providers with abxpkg-managed "
            "install roots default to $ABXPKG_LIB_DIR/<provider name>. Accepts "
            "relative, tilde, and absolute paths. --global is a thin alias for "
            "--lib=None."
        ),
    },
]

# Per-provider env vars that don't show up via simple introspection. These are
# the <NAME>_ROOT and <NAME>_BINARY overrides documented in the README.
PROVIDER_ENV_VARS: dict[str, list[dict[str, str]]] = {
    "pip": [
        {"name": "ABXPKG_PIP_ROOT", "description": "Overrides pip_venv path."},
        {"name": "PIP_BINARY", "description": "Pin the exact pip executable used."},
    ],
    "uv": [
        {"name": "ABXPKG_UV_ROOT", "description": "Overrides uv_venv path."},
        {"name": "UV_BINARY", "description": "Pin the exact uv executable used."},
    ],
    "npm": [
        {"name": "ABXPKG_NPM_ROOT", "description": "Overrides npm_prefix path."},
        {"name": "NPM_BINARY", "description": "Pin the exact npm executable used."},
    ],
    "pnpm": [
        {"name": "ABXPKG_PNPM_ROOT", "description": "Overrides pnpm_prefix path."},
        {"name": "PNPM_BINARY", "description": "Pin the exact pnpm executable used."},
    ],
    "yarn": [
        {"name": "ABXPKG_YARN_ROOT", "description": "Overrides yarn_prefix path."},
        {"name": "YARN_BINARY", "description": "Pin the exact yarn executable used."},
    ],
    "bun": [
        {"name": "ABXPKG_BUN_ROOT", "description": "Overrides bun_prefix path."},
        {"name": "BUN_BINARY", "description": "Pin the exact bun executable used."},
    ],
    "deno": [
        {"name": "ABXPKG_DENO_ROOT", "description": "Overrides deno_root path."},
        {"name": "DENO_BINARY", "description": "Pin the exact deno executable used."},
    ],
    "cargo": [
        {"name": "ABXPKG_CARGO_ROOT", "description": "Overrides cargo_root path."},
        {"name": "CARGO_HOME", "description": "Standard cargo home directory."},
    ],
    "gem": [
        {"name": "ABXPKG_GEM_ROOT", "description": "Overrides gem_home path."},
        {"name": "GEM_HOME", "description": "Standard gem home directory."},
    ],
    "goget": [
        {"name": "ABXPKG_GOGET_ROOT", "description": "Overrides gopath."},
        {"name": "GOPATH", "description": "Standard Go workspace path."},
    ],
    "nix": [
        {"name": "ABXPKG_NIX_ROOT", "description": "Overrides nix_profile path."},
        {"name": "ABXPKG_NIX_PROFILE", "description": "Legacy alias for nix_profile."},
    ],
    "docker": [
        {
            "name": "ABXPKG_DOCKER_ROOT",
            "description": "Overrides docker_root / install_root.",
        },
    ],
    "brew": [
        {
            "name": "ABXPKG_BREW_ROOT",
            "description": "Overrides brew_prefix (discovery only).",
        },
    ],
    "bash": [
        {"name": "ABXPKG_BASH_ROOT", "description": "Overrides bash_root state dir."},
    ],
    "chromewebstore": [
        {
            "name": "ABXPKG_CHROMEWEBSTORE_ROOT",
            "description": "Overrides extensions_root cache dir.",
        },
    ],
    "puppeteer": [
        {
            "name": "ABXPKG_PUPPETEER_ROOT",
            "description": "Overrides puppeteer_root path.",
        },
    ],
    "playwright": [
        {
            "name": "ABXPKG_PLAYWRIGHT_ROOT",
            "description": "Overrides playwright_root AND pins PLAYWRIGHT_BROWSERS_PATH to it.",
        },
    ],
}


def github_tree_url(relative_path: str) -> str:
    return f"{GITHUB_REPO}/tree/{DEFAULT_GITHUB_REF}/{relative_path}"


def github_blob_url(relative_path: str) -> str:
    return f"{GITHUB_REPO}/blob/{DEFAULT_GITHUB_REF}/{relative_path}"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def normalize_doc_path(path: Path | str) -> str:
    text = str(path)
    home = str(HOME_DIR)
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~/" + text[len(home + os.sep) :]
    return text


def normalize_doc_value(value: Any) -> Any:
    if isinstance(value, Path):
        return Path(normalize_doc_path(value))
    if isinstance(value, str):
        return normalize_doc_path(value)
    if isinstance(value, list):
        return [normalize_doc_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_doc_value(item) for item in value)
    if isinstance(value, dict):
        return {
            normalize_doc_value(key): normalize_doc_value(val)
            for key, val in value.items()
        }
    return value


def format_json_value_html(value: Any) -> Markup:
    """Return syntax-highlighted HTML for a provider field default."""
    value = normalize_doc_value(value)
    if value is None:
        return Markup('<span class="cfg-null">None</span>')
    if isinstance(value, bool):
        cls = "cfg-bool-true" if value else "cfg-bool-false"
        return Markup(f'<span class="{cls}">{str(value)}</span>')
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return Markup(f'<span class="cfg-number">{_esc(str(value))}</span>')
    if isinstance(value, str):
        value = value.replace(normalize_doc_path(DEFAULT_ENV_PATH), "$PATH")
        escaped = _esc(value)
        return Markup(f'<span class="cfg-string">&quot;{escaped}&quot;</span>')
    if isinstance(value, Path):
        escaped = _esc(str(value))
        return Markup(f'<span class="cfg-path">Path(&quot;{escaped}&quot;)</span>')
    if isinstance(value, (list, tuple)):
        if not value:
            return Markup('<span class="cfg-bracket">[]</span>')
        items = "".join(
            f'<div class="cfg-array-item">{format_json_value_html(item)}</div>'
            for item in value
        )
        return Markup(
            '<span class="cfg-bracket">[</span>'
            f'<div class="cfg-array">{items}</div>'
            '<span class="cfg-bracket">]</span>',
        )
    if isinstance(value, dict):
        if not value:
            return Markup('<span class="cfg-bracket">{}</span>')
        rows = "".join(
            '<div class="cfg-obj-row">'
            f'<span class="cfg-obj-key">{_esc(str(k))}</span>: '
            f"{format_json_value_html(v)}"
            "</div>"
            for k, v in value.items()
        )
        return Markup(
            '<span class="cfg-bracket">{</span>'
            f'<div class="cfg-obj">{rows}</div>'
            '<span class="cfg-bracket">}</span>',
        )
    return Markup(_esc(repr(value)))


def describe_annotation(annotation: Any) -> str:
    if annotation is None:
        return "None"
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    s = str(annotation)
    s = s.replace("typing.", "").replace("pathlib.", "")
    if len(s) > 60:
        s = s[:57] + "..."
    return s


def collect_provider_fields(
    cls: type[BinProvider],
    instance: BinProvider,
) -> list[dict[str, Any]]:
    """Return declared per-field metadata for a provider.

    Use model-field defaults instead of a live provider instance so the docs
    stay reproducible and do not bake host-specific resolved paths into the
    generated site.
    """
    fields: list[dict[str, Any]] = []
    for name, field in cls.model_fields.items():
        if field.default is not PydanticUndefined:
            value = field.default
        elif field.default_factory is not None:
            try:
                value = getattr(instance, name)
            except Exception:
                value = "<dynamic>"
        else:
            value = None
        # Skip noisy derived/mostly-internal base fields.
        if name in {"overrides"}:
            continue
        fields.append(
            {
                "key": name,
                "type": describe_annotation(field.annotation),
                "default": format_json_value_html(value),
                "description": (field.description or "").strip(),
            },
        )
    return fields


def build_provider(cls: type[BinProvider]) -> dict[str, Any]:
    instance = cls()
    short_name = instance.name
    meta = PROVIDER_METADATA.get(short_name, {})
    fields = collect_provider_fields(cls, instance)
    env_vars = PROVIDER_ENV_VARS.get(short_name, [])
    source_file = meta.get("source_file") or inspect.getfile(cls).split("abxpkg/")[-1]
    source_file = (
        f"abxpkg/{source_file}"
        if not source_file.startswith("abxpkg/")
        else source_file
    )
    category = meta.get("category", CATEGORY_LANGUAGE)

    installer_bin = cls.model_fields.get("INSTALLER_BIN").default
    default_path = cls.model_fields.get("PATH").default

    example_binary = meta.get("example_binary", "my-tool")
    example_arg = meta.get("example_arg", "--version")

    commands = {
        "cli_quick": f"abxpkg --binproviders={short_name} install {example_binary}",
        "cli_env": (
            f"env ABXPKG_BINPROVIDERS={short_name} abxpkg install {example_binary}"
        ),
        "python": (
            f"from abxpkg import Binary, {short_name}\n\n"
            f"bin = Binary(name={example_binary!r}, binproviders=[{short_name}]).install()\n"
            f"bin.exec(cmd=[{example_arg!r}])"
        ),
    }

    tags = list(meta.get("tags", []))
    summary = meta.get("summary", "")
    emoji_attr = getattr(cls, "_log_emoji", None)
    emoji = getattr(emoji_attr, "default", emoji_attr) or "📦"
    display_title = meta.get("display_title", cls.__name__)

    search_parts = [
        short_name,
        display_title,
        summary,
        category,
        CATEGORY_LABELS.get(category, category),
        *tags,
        *[f["key"] for f in fields],
        *[e["name"] for e in env_vars],
    ]
    search_text = " ".join(str(s) for s in search_parts if s).lower()

    return {
        "short_name": short_name,
        "class_name": cls.__name__,
        "display_title": display_title,
        "emoji": emoji,
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, category),
        "summary": summary,
        "tags": tags,
        "installer_bin": installer_bin,
        "default_path": default_path,
        "source_file": source_file,
        "source_url": github_blob_url(source_file),
        "fields": fields,
        "field_count": len(fields),
        "env_vars": env_vars,
        "env_var_count": len(env_vars),
        "commands": commands,
        "example_binary": example_binary,
        "search_text": search_text,
    }


def resolve_global_env_vars() -> list[dict[str, str]]:
    env_vars = copy.deepcopy(GLOBAL_ENV_VARS)
    resolved_defaults = {
        "ABXPKG_BINPROVIDERS": ",".join(abxpkg.DEFAULT_PROVIDER_NAMES),
        "ABXPKG_LIB_DIR": normalize_doc_path(DEFAULT_LIB_DIR),
    }
    for env_var in env_vars:
        if env_var["name"] in resolved_defaults:
            env_var["default"] = resolved_defaults[env_var["name"]]
    return env_vars


def collect_providers() -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for attrname in sorted(dir(abxpkg)):
        obj = getattr(abxpkg, attrname)
        if not inspect.isclass(obj):
            continue
        if not issubclass(obj, BinProvider) or obj is BinProvider:
            continue
        if obj.__name__ in seen:
            continue
        seen.add(obj.__name__)
        providers.append(build_provider(obj))

    category_rank = {name: idx for idx, name in enumerate(CATEGORY_ORDER)}
    providers.sort(
        key=lambda p: (
            category_rank.get(p["category"], 99),
            p["short_name"],
        ),
    )
    return providers


def group_by_category(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for provider in providers:
        groups.setdefault(provider["category"], []).append(provider)

    ordered: list[dict[str, Any]] = []
    for name in CATEGORY_ORDER:
        if name not in groups:
            continue
        ordered.append(
            {
                "name": name,
                "label": CATEGORY_LABELS.get(name, name),
                "providers": groups[name],
            },
        )
    return ordered


def copy_assets(output_dir: Path) -> None:
    target_dir = output_dir / "css"
    target_dir.mkdir(parents=True, exist_ok=True)
    for asset in ASSETS_DIR.glob("*.css"):
        destination = target_dir / asset.name
        if asset.resolve() == destination.resolve():
            continue
        shutil.copy2(asset, destination)


def render_site(output_dir: Path, template_name: str) -> Path:
    providers = collect_providers()
    categories = group_by_category(providers)
    global_env_vars = resolve_global_env_vars()

    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml", "html.j2", "xml.j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template(template_name)
    html = template.render(
        site={
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "github_repo": GITHUB_REPO,
            "github_ref": DEFAULT_GITHUB_REF,
            "package_version": getattr(abxpkg, "__version__", ""),
            "provider_count": len(providers),
            "field_count": sum(p["field_count"] for p in providers),
            "env_var_count": sum(p["env_var_count"] for p in providers)
            + len(global_env_vars),
            "providers": providers,
            "categories": categories,
            "global_env_vars": global_env_vars,
        },
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / "index.html"
    index_path.write_text(html + "\n", encoding="utf-8")
    copy_assets(output_dir)
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return index_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the abxpkg landing-page site.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory to write the generated GitHub Pages site into.",
    )
    parser.add_argument(
        "--template",
        default="index.html.j2",
        help="Template file to render from the docs/ directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = render_site(Path(args.output_dir), args.template)
    print(f"Generated abxpkg landing page at {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

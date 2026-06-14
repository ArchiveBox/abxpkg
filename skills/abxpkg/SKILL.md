---
name: abxpkg
description: Use this when working on binary/package provider resolution, installs, updates, CLI execution, env activation, provider cache, and provider tests.
---

# abxpkg

## Purpose

`abxpkg` is the binary/package provider library and CLI for resolving, installing, updating, running, and inspecting runtime dependencies.

## Shared Rules

- Keep this repo on branch `main`.
- Use `uv` and `uv run` for Python commands.
- Do not use system `python`, direct `.venv/bin/python`, or `pip` commands.
- Use real CLI commands, real providers, real installs, real subprocesses, real package metadata, and real files.
- Do not mock, monkeypatch, fake, simulate, skip, xfail, or weaken tests.
- Verify binary paths, versions, provider state, installed metadata, env output, filesystem contents, and side effects.
- Read `README.md` for the full provider, CLI, Python API, config, and release surface.

## Development Setup

```bash
uv sync
uv run abxpkg --help
uv run abxpkg version
```

## User-Facing Setup

```bash
uv tool install abxpkg
abxpkg version
```

## Basic Usage

```bash
uv run abxpkg load wget
uv run abxpkg install yt-dlp
uv run abxpkg run wget --version
uv run abxpkg env yt-dlp
uv run abxpkg search chromium
uv run abx yt-dlp --help
```

## Verification

```bash
uv run pytest tests/test_cli.py -q
uv run pytest tests/test_chromewebstoreprovider.py -q
uv run prek run --all-files
```

Provider-specific logic belongs in provider classes. Shared provider infrastructure should stay provider-agnostic.

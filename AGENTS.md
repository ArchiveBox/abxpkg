# abxpkg Agent Guide

`abxpkg` is the binary/package provider library and CLI for resolving, installing, updating, running, and inspecting runtime dependencies. Keep this repo on `main`.

## Shared Standards

- Use `uv` and `uv run` for Python commands. Do not use system `python`, direct `.venv/bin/python`, or `pip` commands.
- Prefer existing repo patterns, helper APIs, fixtures, scripts, and command surfaces.
- Keep edits focused and minimal. Do not add wrappers, shims, aliases, or extra abstraction layers unless the current code path requires them.
- Do not weaken assertions, skip tests, xfail tests, or accept flaky behavior.
- No mocks, monkeypatches, fakes, simulated handlers, fake binaries, fake providers, fake install processes, or direct shortcuts around user-facing flows.
- Tests and verification should use real CLI commands, real providers, real installs, real subprocesses, real package metadata, real files, and existing fixtures.
- Assertions must verify real correctness: exit codes, binary paths, versions, provider state, installed metadata, filesystem contents, env output, and side effects.
- Start behavior fixes with a red failing test when a test is requested or practical.
- Trace root causes from observed behavior. Do not paper over failures with retries, wider timeouts, broad fallbacks, or looser assertions.
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

<!-- pytest.mark.live_required -->
```bash
uv run abxpkg load wget
uv run abxpkg install yt-dlp
uv run abxpkg run wget --version
uv run abxpkg env yt-dlp
uv run abxpkg search chromium
uv run abx yt-dlp --help
```

Python API:

```python
from abxpkg import Binary, env, apt, brew

wget = env.load("wget") or apt.install("wget") or brew.install("wget")
binary = Binary(name="wget", binproviders=[env, apt, brew]).install()
print(binary.abspath, binary.version, binary.is_valid)
```

## Verification

Use targeted tests and real providers:

<!-- pytest.mark.live_required -->
```bash
uv run pytest tests/test_cli.py -q
uv run prek run --all-files
```

Provider-specific logic belongs in provider classes. Shared provider infrastructure should stay provider-agnostic.

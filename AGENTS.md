# abxpkg Guide

This is a python package and CLI tool to help install and run binary package dependencies from a variety 
of providers (etc apt, brew, pip, npm, docker, env, bash script, etc.).

It provides nice safe pydantic type interfaces for `Binary`, `BinProvider`, `SemVer`, and more.

### Python API

```python
from abxpkg import Binary, apt, brew, env, BinProvider

wget = env.load('wget') or apt.install('wget') or brew.install('wget')

# or

class WgetBinary(Binary):
    name: str = 'wget'
    binproviders: list[InstanceOf[BinProvider]] = [env, apt, brew]

wget = WgetBinary().install()

print(wget.abspath, wget.version, wget.is_valid)
wget.update()
wget.exec(cmd=['--version'])
wget.uninstall()
```

### CLI

```
abxpkg --version
abxpkg install --binproviders=env,apt,brew wget
abxpkg load|install|update|uninstall|run [--flags...] [binary] [args...]

# abx alias cli:
abx wget --version    # just a thin wrapper around `abxpkg run --install [--flags...] [binary] [args...]`
```

also usable as a shebang in scripts similar to uv run -S, auto installs dependencies before running the script:
```javascript
#!/usr/bin/env abxpkg run --script node --abort-on-uncaught-exception
// /// script
// dependencies = [
//    {name = "node", binproviders = ["env", "apt", "brew"], min_version: "22.0.0"},
//    {name = "playwright", binproviders = ["playwright", "pnpm", "npm", "yarn"]},
//    {name = "chromium", binproviders = ["env", "playwright", "puppeteer", "apt"], min_version: "146.0.0"},
// ]
// [tool.abxpkg]
// ABXPKG_LIB_DIR=/tmp/abxlib
// ABXPKG_MIN_RELEASE_AGE=14
// ABXPKG_POSTINSTALL_SCRIPTS=False
// ///

import {playwright} from 'playwright';
...
```

Read the `./README.md` and `./tests/` to understand the full API surface and behavior.

## Runtime

- always use `uv` and `uv run ...` never pip or `python ...` or `.venv/bin/python` directly.
- lint and typecheck by running `uv run prek run --all-files` (it's fast and comprehensive), never use `py_compile`

## Style

- never create one-line helpers, aliases or compat/legacy/handling code, this is a greenfield codebase and we want clean, consistent UX
- try to avoid inventing new layers of naming or introducing new concepts as much as possible, always reuse existing types and interfaces, and exact naming whenever possible
- prefer flat inline logic even if it's slightly longer, avoid creating tons of `_helpers`, separate files, unnecessary aliases
- keep LoC as low as possible, but don't skimp on comments and docstrings
- make sure all mutable state fields on classes/models have comments explaining their lifecycle + how/why/when they get changed

## Patterns

- leverage pydantic v2 APIs and it's own runtime validation whenever possible, avoid writing manual validation/serialization logic whenever possible
- binproviders much each own all of their own binprovider-specific logic, central files should have no knowledge or mention of any specific binproviders like apt/brew/pip/etc.
- you should almost never run `shutil.which(...)` or `subprocess.call([...])` directly, always use `Binary.load() .abspath, .version, .is_valid, .exec(...), etc.`
- make sure to update the README.md after any significant API surface change or new features. Try to update existing sections matching existing patterns, and never document legacy behavior or "what changed", only document the new behavior concisely (if even needed) in the best sections where it would fit in.

## Tests

```bash
uv run pytest -sx tests/
```

- make sure new changes are tested, but don't go overboard. prefer to extend existing tests or add to existing test files rather than 
creating new superfluous/duplicative tests
- NEVER mock, simulate, or fake behavior, binproviders, binaries, install processes or anything else, always use real live packages from real live binproviders, and use user-facing code to setup realistic e2e flows, then assert the writes and side effects are correct (the actual state, dont just check that attrs are present / no errors occurred)
effects are correct.
- NEVER skip tests in any environment other than apt on macos, that is the ONLY exception. 
- assume ALL binproviders (other than apt on macos) are always available in the host environment (e.g. brew, pip, npm, docker, gem, etc. are ALL available in all environments), let it hard fail naturally if any are missing/broken. do not skip or disable those failing tests.
- Exception for Windows: the Unix-only providers listed in
  `abxpkg.windows_compat.UNIX_ONLY_PROVIDER_NAMES` (apt / brew / nix /
  bash / ansible / pyinfra / docker) have no Windows implementation, so
  `tests/conftest.py::pytest_ignore_collect` skips their per-file test
  modules on Windows. Every other provider must still run its real
  install lifecycle on Windows and fail loudly if the host tooling is
  missing. The scoop provider takes brew's place as the Windows
  system-package source (see `binprovider_scoop.py`).
- it's ok to modify the host environment / run all tests with live installs, even when install_root/lib_dir=None and some providers mutate global system packages

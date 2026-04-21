<h1><a href="https://archivebox.github.io/abxpkg/"><code>abxpkg</code></a> &nbsp; &nbsp; &nbsp; &nbsp; 📦  <small><code>apt</code>&nbsp; <code>brew</code>&nbsp; <code>pip</code>&nbsp; <code>uv</code>&nbsp; <code>npm</code>&nbsp; <code>pnpm</code>&nbsp; <code>yarn</code>&nbsp; <code>bun</code>&nbsp; <code>deno</code>&nbsp; <code>cargo</code>&nbsp; <code>gem</code>&nbsp; <code>goget</code>&nbsp; <code>nix</code>&nbsp; <code>docker</code>&nbsp; <code>bash</code>&nbsp; <code>puppeteer</code>&nbsp; <code>playwright</code>&nbsp; <code>chromewebstore</code>&nbsp; <code>ansible</code>&nbsp; <code>pyinfra</code></small><br/><sub>Simple Python interfaces for package managers + installed binaries.</sub></h1>
<br/>

[![PyPI][pypi-badge]][pypi]
[![Python Version][version-badge]][pypi]
[![Django Version][django-badge]][pypi]
[![GitHub][licence-badge]][licence]
[![GitHub Last Commit][repo-badge]][repo]
<!--[![Downloads][downloads-badge]][pypi]-->

<br/>

**It's an ORM for your package managers, providing nice python types for packages + installers.**  
  
**This is a [Python library](https://pypi.org/project/abxpkg/) and all-in-one CLI for managing packages locally with a variety of package managers.**  
It's designed for when you have to detect or install binary or source dependencies at runtime.

Stop distributing your apps via `curl | sh`! Instead you can bake package installation into your app, or use our `uv`-style [`abxpkg run --script`](https://github.com/ArchiveBox/abxpkg/#shebang-line-in-scripts) shebang headers to auto-install dependencies for you.


```bash
pip install abxpkg

abxpkg --version
```

```python
from abxpkg import Binary, npm

curl = Binary(name="curl").load()
print(curl.abspath, curl.version, curl.exec(cmd=["--version"]))

npm.install("puppeteer")
```

> 📦 Provides consistent interfaces for runtime dependency resolution & installation across multiple package managers & OSs
> ✨ Built with [`pydantic`](https://pydantic-docs.helpmanual.io/) v2 for strong static typing guarantees and easy conversion to/from json
> 🌈 Usable with [`django`](https://docs.djangoproject.com/en/5.0/) >= 4.0, [`django-ninja`](https://django-ninja.dev/), and OpenAPI + [`django-jsonform`](https://django-jsonform.readthedocs.io/) to build UIs & APIs
> 🦄 Driver layer can be [`pyinfra`](https://github.com/pyinfra-dev/pyinfra) / [`ansible`](https://github.com/ansible/ansible) / or built-in `abxpkg` engine

<sub><i>Built by <a href="https://github.com/ArchiveBox">ArchiveBox</a> to install & auto-update our extractor dependencies at runtime (<code>chrome</code>, <code>wget</code>, <code>curl</code>, etc.) on `macOS`/`Linux`/`Docker`.</i></sub>

<br/>

**Source Code**: [https://github.com/ArchiveBox/abxpkg/](https://github.com/ArchiveBox/abxpkg/)  
**Documentation**: [https://github.com/ArchiveBox/abxpkg/blob/main/README.md](https://github.com/ArchiveBox/abxpkg/blob/main/README.md)

<br/>

```python
from abxpkg import Binary, apt, brew, pip, npm, env

# Provider singletons are available as simple imports — no manual instantiation needed
dependencies = [
    Binary(name='curl',       binproviders=[env, apt, brew]),
    Binary(name='yt-dlp',     binproviders=[env, pip, uv, apt, brew]),
    Binary(name='playwright', binproviders=[env, npm, pnpm]),
    Binary(name='chromium',   binproviders=[playwright, puppeteer, apt]),
    Binary(name='postgres',   binproviders=[docker, env, apt, brew]),
]
for binary in dependencies:
    binary = binary.install()

    print(binary.abspath, binary.version, binary.binprovider, binary.is_valid, binary.sha256, binary.mtime)
    # Path(...) SemVer(...) EnvProvider()/AptProvider()/BrewProvider()/PipProvider()/NpmProvider() True '<sha256>' 1712890123456789000

    binary.exec(cmd=['--version'])   # curl 7.81.0 (x86_64-apple-darwin23.0) libcurl/7.81.0 ...
```

<br/>

---

> [!TIP]
> **🔒 Stay safe from supply-chain attcaks with `abxpkg`:** We default to safe behavior (when providers allow):
> 
>  - `min_release_age=7` (we only install packages that have been published for 7 days or longer)
>  - `postinstall_scripts=False` (we don't run post-install scripts for packages by default)
>  - `install_root=<platform default abx lib dir>` (the CLI defaults to a dedicated provider-rooted library dir so host system stays clean)
>
> You can customize these defaults on `Binary` or `BinProvider`, or with `ABXPKG_MIN_RELEASE_AGE`/`ABXPKG_POSTINSTALL_SCRIPTS`/`ABXPKG_LIB_DIR` (see [Configuration](#Configuration) below).

---

## Usage

### Install

```bash
pip install abxpkg
# or
uv tool add abxpkg
```

### CLI

Installing `abxpkg` also provides an `abxpkg` CLI entrypoint:

```bash
abxpkg --version
abxpkg version
abxpkg list

abxpkg install yt-dlp
abxpkg update yt-dlp
abxpkg uninstall yt-dlp
abxpkg load yt-dlp
```

`abxpkg --version` and `abxpkg version` stream the package version first, then a host/env summary line, then one section per selected provider showing its current resolved runtime state (`INSTALLER_BINARY`, `PATH`, `ENV`, `install_root`, `bin_dir`, and any active cached dependency / installed binaries).

`abxpkg version <binary>` is a thin alias for `abxpkg load <binary>`.

`abxpkg list` prints the full active cache for the selected providers, grouping provider installer binaries first and normal cached binaries after a blank line. You can optionally pass binary names and/or provider names positionally to filter the output:

```bash
abxpkg list
abxpkg list yt-dlp chromium
abxpkg list env puppeteer chromium
```

#### Execute an installed binary via the configured providers

```bash
abxpkg run yt-dlp --help                          # resolves yt-dlp via the configured providers and execs it
abxpkg --binproviders=pip,brew run pip show black # restrict provider resolution (exercises PipProvider.exec)
abxpkg --binproviders=pip --install run yt-dlp    # load first, then install via selected providers if needed
abxpkg --binproviders=pip --update  run yt-dlp    # ensure the binary is available, then update before exec
abxpkg --binproviders=pip --no-cache --install run yt-dlp  # bypass cached/current-state checks during resolution + install
```

abxpkg options (e.g. `--binproviders`, `--lib`, `--install`, `--update`, `--no-cache`) must appear before the `run` subcommand; every argument after the binary name is forwarded verbatim to the underlying binary. `run` exits with the child's exit code, passes its `stdout`/`stderr` through unbuffered, and routes any abxpkg install/load logs to `stderr` only — no headers, no footers, no parsing.

#### `abx`: auto-install-and-run shortcut

Think `npx` / `uvx` / `pipx run` — but for **every** package manager abxpkg supports. `abx` is a thin alias for `abxpkg --install run ...`: it resolves the binary via the configured providers, installs it if missing, then execs it with the forwarded arguments.

```bash
abx yt-dlp --help                               # auto-install (if needed) and run yt-dlp
abx --update yt-dlp --help                      # ensure the binary is available, then update before running
abx --binproviders=env,uv,pip,apt,brew yt-dlp   # restrict provider resolution
```

Options before the binary name (`--lib`, `--binproviders`, `--dry-run`, `--debug`, `--no-cache`, `--update`) are forwarded to `abxpkg`; everything after the binary name is forwarded to the binary itself.

#### Shebang Line in Scripts

Inspired by [`uv`'s inline script metadata](https://docs.astral.sh/uv/guides/scripts/#declaring-script-dependencies), `abxpkg` lets you declare **arbitrary package dependencies** at the top of any script using a `/// script` metadata block.

```javascript
#!/usr/bin/env -S abxpkg run --script node

// /// script
// dependencies = [
//     {name = "node", binproviders = ["env", "apt", "brew"], min_version = "22.0.0"},
//     {name = "playwright", binproviders = ["pnpm", "npm"]},
//     {name = "chromium", binproviders = ["playwright", "puppeteer", "apt"], min_version = "131.0.0"},
// ]
// [tool.abxpkg]
// ABXPKG_POSTINSTALL_SCRIPTS = true
// ///

const { chromium } = require('playwright');

(async () => {
    const browser = await chromium.launch();
    const page = await browser.newPage();
    await page.goto('https://example.com');
    console.log(await page.title());
    await browser.close();
})();
```

The metadata parser is comment-syntax-agnostic — it looks for `/// script` and `///` delimiters and strips the first whitespace-delimited token from each line, so `#`, `//`, `--`, `;`, and any other single-token comment prefix all work.

#### Per-`Binary` / per-`BinProvider` options as CLI flags

Every [`Binary` / `BinProvider` configuration field](#configuration) is exposed as a CLI flag on the group and on subcommands (`install`, `update`, `uninstall`, `load`), and is also available to `run` / `abx` via group-level flags placed before the binary name. Providers that can't enforce a given option emit a warning to `stderr` and continue — no hard failure.

```bash
abxpkg --min-version=1.2.3 --min-release-age=7 install yt-dlp
abxpkg --postinstall-scripts=False --binproviders=apt,uv,pip install black
abxpkg --no-cache install black
abxpkg --install-root=/tmp/yt-dlp-root --bin-dir=/tmp/yt-dlp-bin install yt-dlp
abxpkg --overrides='{"pip":{"install_args":["yt-dlp[default]"]}}' install yt-dlp
abxpkg --install-timeout=600 --version-timeout=20 --euid=1000 install yt-dlp
abxpkg --global install yt-dlp
abx --min-version=2024.1.1 --min-release-age=0 yt-dlp --help
```

| Flag | Type | Meaning |
| --- | --- | --- |
| `--min-version=SEMVER` | `str` | Minimum acceptable version (set on `Binary.min_version`). |
| `--postinstall-scripts[=BOOL]` | `bool` | Allow post-install scripts. Bare `--postinstall-scripts` = `True`. Providers that can't disable them warn-and-ignore. |
| `--min-release-age=DAYS` | `float` | Minimum days since publication. Non-supporting providers warn-and-ignore. |
| `--no-cache[=BOOL]` | `bool` | Skip cached/current-state checks and force fresh install/update/load probes. Bare `--no-cache` = `True`. |
| `--overrides=JSON` | `dict` | Per-provider `Binary.overrides` patches for shared provider fields (`PATH`, `INSTALLER_BIN`, `install_root`, `bin_dir`, `euid`, `postinstall_scripts`, `min_release_age`, `dry_run`, `install_timeout`, `version_timeout`) plus per-binary handler replacements (`install_args`, `abspath`, `version`, `install`, `update`, `uninstall`). |
| `--global[=BOOL]` | `bool` | Thin alias for `--lib=None`. Bare `--global` = `True`. |
| `--install-root=PATH` | `Path` | Override the per-provider install directory. |
| `--bin-dir=PATH` | `Path` | Override the per-provider bin directory. |
| `--euid=UID` | `int` | Pin the UID used when providers shell out. |
| `--install-timeout=SECONDS` | `int` | Seconds to wait for install/update/uninstall subprocesses. |
| `--version-timeout=SECONDS` | `int` | Seconds to wait for version/metadata probes. |
| `--dry-run[=BOOL]` | `bool` | Show installer commands without executing them. Bare `--dry-run` = `True`. |
| `--debug[=BOOL]` | `bool` | Emit DEBUG logs to `stderr`. Bare `--debug` = `True`. Defaults to `ABXPKG_DEBUG` or `False`. |

Every value-taking flag also accepts the literal string `None` / `null` / `""` to reset to the provider's default resolution path. For `postinstall_scripts` / `min_release_age`, that means the action-specific effective default for that provider (`False` / `7` on supporting providers, `True` / `0` otherwise). The precedence is: explicit per-subcommand flag > group-level flag > environment variable > built-in default.

#### Select specific providers / re-order provider precedence

```bash
abxpkg install --binproviders=env,uv,pip,apt,brew prettier
# or
env ABXPKG_BINPROVIDERS=env,uv,pip,apt,brew abxpkg install yt-dlp
```

#### Customize where installed packages are located

```bash
abxpkg --lib=~/my-abx-lib install yt-dlp        # pin a custom provider-rooted library dir
abxpkg --lib=./vendor install yt-dlp            # store all packages under $PWD/vendor
abxpkg --lib=/tmp/abxlib install yt-dlp         # store all packages under /tmp/abxlib
abxpkg --global install yt-dlp                  # alias for --lib=None (use provider-native global mode where supported)

# or
env ABXPKG_LIB_DIR=/any/dir/path abxpkg install yt-dlp
```

#### Run in "dry mode" to see what commands will do before executing

```bash
abxpkg install --dry-run some-dangerous-package      # outputs commands that would be run without executing them
# or
env ABXPKG_DRY_RUN=1 abxpkg install some-dangerous-package
```

CLI result lines are written to `stdout`. Progress logging is written to `stderr` at `INFO` by default. Enable DEBUG logging with `ABXPKG_DEBUG=1` or `--debug`.

<br/>

### Python Library

#### Basic Usage

All built-in providers are available as lazy singletons — just import them by name:

```python
from abxpkg import apt, brew, pip, npm, env

apt.install('curl')
env.load('wget')
```

These are instantiated on first access and cached for reuse. If you need custom configuration, you can still instantiate provider classes directly:

```python
from pathlib import Path
from abxpkg import PipProvider

custom_pip = PipProvider(install_root=Path("/tmp/abxpkg-pip"), min_release_age=0)
```

Use the `Binary` class to declare a package that can be installed by one of several ordered providers, with an optional version floor:

```python
from abxpkg import Binary, SemVer, env, brew

curl = Binary(
    name="curl",
    min_version=SemVer("8.0.0"),
    binproviders=[env, brew],
).install()
```

`min_version` is enforced after a provider resolves or installs a binary — provider discovery can still succeed, but the final `Binary` is rejected if the loaded version is below the floor. Use `min_version=None` to disable the check.

Pass `no_cache=True` to `load()` / `install()` / `update()` / `uninstall()` when you want to bypass cached/current-state checks. For `install()`, `no_cache=True` skips the initial `load()` check and forces a fresh install path. The equivalent CLI and env controls are `--no-cache` and `ABXPKG_NO_CACHE=1`.

#### Advanced Usage

<details>
<summary><h4>Define a reusable <code>Binary</code> subclass with per-provider overrides</h4></summary>

```python
from pydantic import InstanceOf
from abxpkg import BinProvider, Binary, BinProviderName, BinName, HandlerDict, BrewProvider
from abxpkg import env, pip, apt

class CustomBrewProvider(BrewProvider):
    name: BinProviderName = 'custom_brew'

    def get_macos_packages(self, bin_name: str, **context) -> list[str]:
        return ['yt-dlp'] if bin_name == 'ytdlp' else [bin_name]

class YtdlpBinary(Binary):
    name: BinName = 'ytdlp'
    description: str = 'YT-DLP (Replacement for YouTube-DL) Media Downloader'

    # define the providers this binary supports
    binproviders: list[InstanceOf[BinProvider]] = [env, pip, apt, CustomBrewProvider()]

    # customize installed package names for specific package managers
    overrides: dict[BinProviderName, HandlerDict] = {
        'pip': {'install_args': ['yt-dlp[default,curl-cffi]']},   # literal values
        'apt': {'install_args': lambda: ['yt-dlp', 'ffmpeg']},    # any pure Callable
        'custom_brew': {'install_args': 'self.get_macos_packages'},  # or a string ref to a method on self
    }


ytdlp = YtdlpBinary().install()
print(ytdlp.binprovider)    # EnvProvider(...) / PipProvider(...) / AptProvider(...) / CustomBrewProvider(...)
print(ytdlp.abspath)        # Path(...)
print(ytdlp.version)        # SemVer(...)
print(ytdlp.is_valid)       # True

# Lifecycle actions preserve the Binary type and refresh/clear loaded metadata as needed
ytdlp = ytdlp.update()
assert ytdlp.is_valid
ytdlp = ytdlp.uninstall()
assert ytdlp.abspath is None and ytdlp.version is None
```

</details>

<details>
<summary><h4>Use <code>Binary</code> objects as a stable typed interface to interact with installed packages</h4></summary>

```python
from abxpkg import Binary, apt, brew, env

# Use providers directly for package manager operations
apt.install('wget')
print(apt.PATH, apt.get_abspaths('wget'), apt.get_version('wget'))

# our Binary API provides a nice type-checkable, validated, serializable handle
ffmpeg = Binary(name='ffmpeg', binproviders=[env, apt, brew]).load()
print(ffmpeg)                       # Binary(name='ffmpeg', abspath=Path(...), version=SemVer(...), sha256='...', mtime=1712890123456789000)
print(ffmpeg.abspaths)              # show all matching binaries found via each provider PATH
print(ffmpeg.model_dump(mode='json'))  # JSON-ready dict
print(ffmpeg.model_json_schema())   # ... OpenAPI-ready JSON schema showing all available fields
```

```python
from pydantic import InstanceOf
from abxpkg import Binary, BinProvider, BrewProvider, EnvProvider

# You can also instantiate provider classes manually for custom configuration,
# or define binaries as classes for type checking
class CurlBinary(Binary):
    name: str = 'curl'
    binproviders: list[InstanceOf[BinProvider]] = [BrewProvider(), EnvProvider()]

curl = CurlBinary().install()
assert isinstance(curl, CurlBinary)                                 # CurlBinary is a unique type you can use in annotations now
print(curl.abspath, curl.version, curl.binprovider, curl.is_valid)  # Path(...) SemVer(...) BrewProvider()/EnvProvider() True
curl.exec(cmd=['--version'])                                        # curl 8.4.0 (x86_64-apple-darwin23.0) libcurl/8.4.0 ...
```

</details>


<details>
<summary><h4>Customize binary resolution/install/other behavior via per-provider or per-binary overrides</h4></summary>

```python
import os
import platform
from pydantic import InstanceOf
from abxpkg import BinProvider, Binary, BinProviderName, BinName, HandlerDict
from abxpkg import env, apt

class DockerBinary(Binary):
    name: BinName = 'docker'
    binproviders: list[InstanceOf[BinProvider]] = [env, apt]

    overrides: dict[BinProviderName, HandlerDict] = {
        'env': {
            # prefer podman if installed, fall back to docker
            'abspath': lambda: os.which('podman') or os.which('docker') or os.which('docker-ce'),
        },
        'apt': {
            # vary the installed package name based on CPU architecture
            'install_args': {
                'amd64': ['docker'],
                'armv7l': ['docker-ce'],
                'arm64': ['docker-ce'],
            }.get(platform.machine(), 'docker'),
        },
    }

docker = DockerBinary().install()
```

</details>

<details>
<summary><h4>Subclass <code>BinProvider</code> to add support for a new package manager</h4></summary>

```python
from pathlib import Path
from abxpkg import (
    BinProvider,
    BinProviderName,
    BinName,
    HostBinPath,
    InstallArgs,
    SemVer,
    bin_abspath,
)

class CargoProvider(BinProvider):
    name: BinProviderName = 'cargo'
    INSTALLER_BIN: BinName = 'cargo'
    PATH: str = str(Path.home() / '.cargo/bin')

    def default_install_args_handler(self, bin_name: BinName, **context) -> InstallArgs:
        return [bin_name]

    def default_install_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        timeout: int | None = None,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)
        installer = self.INSTALLER_BINARY()
        assert installer and installer.loaded_abspath
        proc = self.exec(bin_name=installer.loaded_abspath, cmd=['install', *install_args], timeout=timeout)
        if proc.returncode != 0:
            self._raise_proc_error('install', install_args, proc)
        return proc.stdout.strip() or proc.stderr.strip()

    def default_abspath_handler(self, bin_name: BinName, **context) -> HostBinPath | None:
        return bin_abspath(bin_name, PATH=self.PATH)

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        **context,
    ) -> SemVer | None:
        return self._version_from_exec(bin_name, abspath=abspath, timeout=timeout)


cargo = CargoProvider()
rg = cargo.install(bin_name='ripgrep')
print(rg.binprovider)    # CargoProvider(...)
print(rg.version)        # SemVer(...)
```

</details>

<details>
<summary><h4>Configure python <code>logging</code> to customize the stderr/stdout logging</h4></summary>

`abxpkg` uses the standard Python `logging` module. By default it stays quiet unless your application configures logging explicitly.

```python
import logging
from abxpkg import Binary, env, configure_logging

configure_logging(logging.INFO)

python = Binary(name='python', binproviders=[env]).load()
```

To enable Rich logging:

```bash
pip install "abxpkg[rich]"
```

```python
import logging
from abxpkg import Binary, EnvProvider, configure_rich_logging

configure_rich_logging(logging.DEBUG)

python = Binary(name='python', binproviders=[EnvProvider()]).load()
```

Debug logging is hardened so logging itself does not become the failure. If a provider/model object has a broken or overly-expensive `repr()`, `abxpkg` falls back to a short `ClassName(...)` summary instead of raising while formatting log output.

`configure_rich_logging(...)` uses `rich.logging.RichHandler` under the hood, so log levels, paths, arguments, and command lines render with terminal colors when supported.

You can also manage it with standard logging primitives:

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("abxpkg").setLevel(logging.DEBUG)
```

</details>

<details>
<summary><h4>Django integration: store <code>BinProvider</code> / <code>Binary</code> in DB models and render them in the Admin</h4></summary>

With a few more packages, you get type-checked Django fields & forms that support `BinProvider` and `Binary`.

> [!TIP]
> For the full Django experience, we recommend installing these 3 excellent packages:
> - [`django-admin-data-views`](https://github.com/MrThearMan/django-admin-data-views)
> - [`django-pydantic-field`](https://github.com/surenkov/django-pydantic-field)
> - [`django-jsonform`](https://django-jsonform.readthedocs.io/)
> `pip install abxpkg django-admin-data-views django-pydantic-field django-jsonform`

**Django model fields:**

```python
from django.db import models
from abxpkg import BinProvider, Binary, SemVer
from django_pydantic_field import SchemaField

class Dependency(models.Model):
    label = models.CharField(max_length=63)
    default_binprovider: BinProvider = SchemaField()
    binaries: list[Binary] = SchemaField(default=[])
    min_version: SemVer = SchemaField(default=(0, 0, 1))
```

Saving a `Binary` using the model:

```python
from abxpkg import Binary, env

curl = Binary(name='curl').load()

obj = Dependency(
    label='runtime tools',
    default_binprovider=env,   # store BinProvider values directly
    binaries=[curl],            # store Binary/SemVer values directly
)
obj.save()
```

When fetching back from the DB, `Binary` fields are auto-deserialized and immediately usable:

```python
obj = Dependency.objects.get(label='runtime tools')
assert obj.binaries[0].abspath == curl.abspath
obj.binaries[0].exec(cmd=['--version'])
```

For a full example see the bundled [`django_example_project/`](https://github.com/ArchiveBox/abxpkg/tree/main/django_example_project).

**Django Admin integration:**

<img height="220" alt="Django Admin binaries list view" src="https://github.com/ArchiveBox/abxpkg/assets/511499/a9980217-f39e-434e-b266-20cd6feb17c3" align="top"><img height="220" alt="Django Admin binaries detail view" src="https://github.com/ArchiveBox/abxpkg/assets/511499/d4d9086e-c8f4-4b6e-8ee8-8c8a864715b0" align="top">

```python
# settings.py
INSTALLED_APPS = [
    # ...
    'admin_data_views',
    'abxpkg',
]

ABXPKG_GET_ALL_BINARIES = 'project.views.get_all_binaries'
ABXPKG_GET_BINARY = 'project.views.get_binary'

ADMIN_DATA_VIEWS = {
    "NAME": "Environment",
    "URLS": [
        {
            "route": "binaries/",
            "view": "abxpkg.views.binaries_list_view",
            "name": "binaries",
            "items": {
                "route": "<str:key>/",
                "view": "abxpkg.views.binary_detail_view",
                "name": "binary",
            },
        },
    ],
}
```

If you override the default site admin, register the views manually:

```python
from abxpkg.admin import register_admin_views

custom_admin = YourSiteAdmin()
register_admin_views(custom_admin)
```

</details>

---

### Configuration

All abxpkg env vars are read once at import time and only apply when set. Explicit constructor kwargs always override these defaults.

**Behavioral controls** (apply across all providers):

| Variable | Default | Effect |
| --- | --- | --- |
| `ABXPKG_DRY_RUN` / `DRY_RUN` | `0` | Flips the shared `dry_run` default. `ABXPKG_DRY_RUN` wins if both are set. Provider subprocesses are logged and skipped, `install()` / `update()` return a placeholder, `uninstall()` returns `True`. |
| `ABXPKG_NO_CACHE` | `0` | Flips the shared `no_cache` default. When enabled, `install()` skips the initial `load()` check and forces a fresh install path, while `load()` / `update()` / `uninstall()` bypass cached probe results. |
| `ABXPKG_DEBUG` | `0` | Enables DEBUG-level CLI logging on `stderr` for `abxpkg` / `abx`. The matching CLI flag is `--debug`. Default CLI logging level is `INFO`. |
| `ABXPKG_INSTALL_TIMEOUT` | `120` | Seconds to wait for `install()` / `update()` / `uninstall()` handler subprocesses. |
| `ABXPKG_VERSION_TIMEOUT` | `10` | Seconds to wait for version / metadata probes (`--version`, `npm show`, `pip show`, etc.). |
| `ABXPKG_POSTINSTALL_SCRIPTS` | unset | Hydrates the provider-level default for the `postinstall_scripts` kwarg on every provider that supports it (`pip`, `uv`, `npm`, `pnpm`, `yarn`, `bun`, `deno`, `brew`, `chromewebstore`, `puppeteer`). When left unset, action execution resolves to the provider/action default (`False` on supporting providers, `True` otherwise). |
| `ABXPKG_MIN_RELEASE_AGE` | `7` | Hydrates the provider-level default (in days) for the `min_release_age` kwarg on every provider that supports it (`pip`, `uv`, `npm`, `pnpm`, `yarn`, `bun`, `deno`). When left unset, action execution resolves to the provider/action default (`7` on supporting providers, `0` otherwise). |
| `ABXPKG_BINPROVIDERS` | shared default order | Comma-separated list of provider names to enable (and their order) for the `abxpkg` CLI. By default this uses `DEFAULT_PROVIDER_NAMES` from `abxpkg.__init__` (which excludes `ansible` / `pyinfra`, and also excludes `apt` on macOS). |

**Install-root controls** (one global default + one per-provider override):

| Variable | Applies to | Effect |
| --- | --- | --- |
| `ABXPKG_LIB_DIR` | providers whose default `install_root` is abxpkg-managed | Centralized library root. When set, each matching provider points its default `install_root` at `$ABXPKG_LIB_DIR/<provider name>` (e.g. `<lib>/env`, `<lib>/npm`, `<lib>/pip`, `<lib>/gem`, `<lib>/playwright`). Accepts relative (`./lib`), tilde (`~/.config/abx/lib`), and absolute (`/tmp/abxlib`) paths. `--global` is a thin alias for `--lib=None`, which clears this root for the current CLI invocation. |
| `ABXPKG_<BINPROVIDER>_ROOT` | the matching provider's `install_root` | Generic per-provider override; beats `ABXPKG_LIB_DIR/<provider name>`. Examples: `ABXPKG_PIP_ROOT`, `ABXPKG_UV_ROOT`, `ABXPKG_NPM_ROOT`, `ABXPKG_GOGET_ROOT`, `ABXPKG_CHROMEWEBSTORE_ROOT`. The `<BINPROVIDER>` token is the provider name uppercased. |

Install-root precedence (most specific wins): explicit `install_root=` / provider alias kwarg > `ABXPKG_<NAME>_ROOT` > `ABXPKG_LIB_DIR/<name>` > provider-specific built-in default / native global mode.

**Provider-specific binary overrides:**

Each provider also honors a `<NAME>_BINARY=/abs/path/to/<name>` env var to pin the exact executable it shells out to — `PIP_BINARY`, `UV_BINARY`, `NPM_BINARY`, `PNPM_BINARY`, `YARN_BINARY`, `BUN_BINARY`, `DENO_BINARY`, etc.

**Per-`Binary` / per-`BinProvider` fields** (constructor kwargs, most-specific wins):

- `min_version` can be set on any individual `Binary`.
- `min_release_age` can be set on `Binary` or `BinProvider`, or via `ABXPKG_MIN_RELEASE_AGE` (days).
- `postinstall_scripts` can be set on `Binary` or `BinProvider`, or via `ABXPKG_POSTINSTALL_SCRIPTS`.
- `no_cache` can be passed per-call to `load()` / `install()` / `update()` / `uninstall()`, or enabled globally for the CLI via `ABXPKG_NO_CACHE`.
- `install_root` / `bin_dir` can be set on any `BinProvider` with an isolated install location, or default to `ABXPKG_<NAME>_ROOT` / `ABXPKG_LIB_DIR/<provider name>` / the provider's own built-in default.
- `dry_run` can be set on `BinProvider` or passed per-call to `install()` / `update()` / `uninstall()`, or via `ABXPKG_DRY_RUN` / `DRY_RUN`.
- `install_timeout` can be set on `BinProvider` or via `ABXPKG_INSTALL_TIMEOUT` (seconds).
- `version_timeout` can be set on `BinProvider` or via `ABXPKG_VERSION_TIMEOUT` (seconds).
- `euid` can be set on `BinProvider` to pin the UID used to `sudo`/drop into when running provider subprocesses; otherwise it's auto-detected from `install_root` ownership.
- `overrides` is a `dict[BinProviderName, HandlerDict]` (on `Binary`) or `dict[BinName, HandlerDict]` (on `BinProvider`) mapping to per-provider field patches and per-binary handler replacements. Supported keys are `PATH`, `INSTALLER_BIN`, `euid`, `install_root`, `bin_dir`, `dry_run`, `postinstall_scripts`, `min_release_age`, `install_timeout`, `version_timeout`, `install_args` / `packages`, `abspath`, `version`, `install`, `update`, and `uninstall`. See [Advanced Usage](#define-a-reusable-binary-subclass-with-per-provider-overrides) for examples.

Precedence is always: explicit action kwarg > `Binary(...)` field > `BinProvider(...)` field > env var > built-in default.

<br/>

---
---

<br/>

## API Reference

### [`BinProvider`](https://github.com/ArchiveBox/abxpkg/blob/main/abxpkg/binprovider.py#:~:text=class%20BinProvider)

**Built-in implementations:** `EnvProvider`, `AptProvider`, `BrewProvider`, `PipProvider`, `UvProvider`, `NpmProvider`, `PnpmProvider`, `YarnProvider`, `BunProvider`, `DenoProvider`, `CargoProvider`, `GemProvider`, `GoGetProvider`, `NixProvider`, `DockerProvider`, `PyinfraProvider`, `AnsibleProvider`, `BashProvider`, `ChromeWebstoreProvider`, `PuppeteerProvider`, `PlaywrightProvider`

This type represents a provider of binaries, e.g. a package manager like `apt` / `pip` / `npm`, or `env` (which only resolves binaries already present in `$PATH`).

#### 🧩 Shared API

Every provider exposes the same lifecycle surface:

- `load()` / `install()` / `update()` / `uninstall()`
- `get_install_args()` to resolve package names / formulae / image refs / module specs
- `get_abspath()` / `get_abspaths()` / `get_version()` / `get_sha256()`

Shared base defaults come from [`abxpkg/binprovider.py`](./abxpkg/binprovider.py) and apply unless a concrete provider overrides them:

```python
INSTALLER_BIN = "env"              # base-class placeholder; real providers override this
PATH = str(Path(sys.executable).parent)
postinstall_scripts = None           # some providers override this with ABXPKG_POSTINSTALL_SCRIPTS
min_release_age = None               # some providers override this with ABXPKG_MIN_RELEASE_AGE
install_timeout = 120                # or ABXPKG_INSTALL_TIMEOUT=120
version_timeout = 10                 # or ABXPKG_VERSION_TIMEOUT=10
dry_run = False                      # or ABXPKG_DRY_RUN=1 / DRY_RUN=1
```

- `dry_run`: use `provider.get_provider_with_overrides(dry_run=True)`, pass `dry_run=True` directly to `install()` / `update()` / `uninstall()`, or set `ABXPKG_DRY_RUN=1` / `DRY_RUN=1`. If both env vars are set, `ABXPKG_DRY_RUN` wins. Provider subprocesses are logged and skipped, `install()` / `update()` return a placeholder loaded binary, and `uninstall()` returns `True` without mutating the host.
- `no_cache`: use `--no-cache` / `ABXPKG_NO_CACHE=1` on the CLI, or pass `no_cache=True` directly to `load()` / `install()` / `update()` / `uninstall()`. For `install()`, this skips the initial `load()` check and forces a fresh install path.
- `install_timeout`: shared provider-level timeout used by `install()`, `update()`, and `uninstall()` handler execution paths. Can also be set with `ABXPKG_INSTALL_TIMEOUT`.
- `version_timeout`: shared provider-level timeout used by version / metadata probes such as `--version`, `npm show`, `npm list`, `pip show`, `go version -m`, and brew lookups. Can also be set with `ABXPKG_VERSION_TIMEOUT`.
- `postinstall_scripts` and `min_release_age` are standard provider/binary/action kwargs. Supporting providers hydrate defaults from `ABXPKG_POSTINSTALL_SCRIPTS` and `ABXPKG_MIN_RELEASE_AGE`; when those remain unset/`None`, install/update/uninstall resolve them to effective action defaults (`False` / `7` on supporting providers, `True` / `0` otherwise).
- Providers that do not support one of those controls leave the provider default as `None`. If you pass an explicit unsupported value during `install()` / `update()`, it is logged as a warning and ignored.
- Precedence is: explicit action args > `Binary(...)` defaults > provider defaults.

For the full list of env vars that hydrate these defaults, see [Configuration](#configuration) above.

Supported override keys are the same everywhere:

```python
from pathlib import Path
from abxpkg import PipProvider

provider = PipProvider(install_root=Path("/tmp/venv")).get_provider_with_overrides(
    overrides={
        "black": {
            "install_args": ["black==24.4.2"],
            "version": "self.default_version_handler",
            "abspath": "self.default_abspath_handler",
        },
    },
    dry_run=True,
    version_timeout=30,
)
```

- `install_args` / `packages`: package-manager arguments for that provider. `packages` is the legacy alias.
- `abspath`, `version`, `install`, `update`, `uninstall`: literal values, callables, or `"self.method_name"` references that replace the provider handler for a specific binary.
- `PATH`, `INSTALLER_BIN`, `euid`, `install_root`, `bin_dir`, `dry_run`, `postinstall_scripts`, `min_release_age`, `install_timeout`, `version_timeout`: shared provider field patches applied to the copied provider instance before handler resolution.

Providers with isolated install locations also expose a shared constructor surface:

- `install_root`: shared provider root for package state, metadata, caches, venvs, project dirs, profiles, or downloaded assets, depending on the provider.
- `bin_dir`: shared executable output dir when a provider separates package state from runnable binaries.
- `provider.install_root` / `provider.bin_dir`: normalized computed properties you can inspect after construction, regardless of which provider-specific args were used.
- Legacy provider-specific args still work. The shared aliases are additive, not replacements.
- Providers that do not have an isolated install location reject `install_root` / `bin_dir` at construction time instead of silently ignoring them.
- When an explicit install root or bin dir is configured, that provider-specific bin location wins during binary discovery and subprocess execution instead of being left behind ambient host `PATH` entries.

<br/>

### Supported `BinProvider`s

<details>
<summary><h4>🌍 <code>EnvProvider</code> (<code>env</code>)</h4></summary>

Source: [`abxpkg/binprovider.py`](./abxpkg/binprovider.py) • Tests: [`tests/test_envprovider.py`](./tests/test_envprovider.py)

```python
INSTALLER_BIN = "which"
PATH = DEFAULT_ENV_PATH              # current PATH + current Python bin dir
```

- Install root: defaults to `ABXPKG_ENV_ROOT`, or `ABXPKG_LIB_DIR/env`, or the platform default abx lib dir under `env/`. `env` is still read-only: it only resolves binaries that already exist on the host PATH, but when an install root is configured it also keeps a managed `bin/` symlink dir and `derived.env` cache there.
- Auto-switching: none.
- Security: `min_release_age` and `postinstall_scripts` are unsupported here and are ignored with a warning if explicitly passed to `install()` / `update()`.
- Overrides: `abspath` / `version` are the useful ones here. `python` has a built-in override to the current `sys.executable` and interpreter version.
- Notes: resolved `abspath`s always point at the real underlying host binary, not the managed `env/bin/<name>` symlink. `install()` / `update()` return explanatory no-op messages, and `uninstall()` is a no-op.

</details>

<details>
<summary><h4>🐧 <code>AptProvider</code> (<code>apt</code>)</h4></summary>

Source: [`abxpkg/binprovider_apt.py`](./abxpkg/binprovider_apt.py) • Tests: [`tests/test_aptprovider.py`](./tests/test_aptprovider.py)

```python
INSTALLER_BIN = "apt-get"
PATH = ""                            # populated from `dpkg -L bash` bin dirs
euid = 0                             # always runs as root
```

- Install root: **no hermetic prefix support**. Installs into the host package database.
- Auto-switching: none. Shells out to `apt-get` directly.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` becomes `apt-get install -y -qq --no-install-recommends ...`; `update()` uses `apt-get install --only-upgrade ...`; `uninstall()` uses `apt-get remove -y -qq ...`.
- Notes: direct mode runs `apt-get update -qq` at most once per day and requests privilege escalation when needed.

</details>

<details>
<summary><h4>🍺 <code>BrewProvider</code> (<code>brew</code>)</h4></summary>

Source: [`abxpkg/binprovider_brew.py`](./abxpkg/binprovider_brew.py) • Tests: [`tests/test_brewprovider.py`](./tests/test_brewprovider.py)

```python
INSTALLER_BIN = "brew"
PATH = "/home/linuxbrew/.linuxbrew/bin:/opt/homebrew/bin:/usr/local/bin"
brew_prefix = guessed host prefix    # /opt/homebrew, /usr/local, or linuxbrew
```

- Install root: `brew_prefix` is the Homebrew prefix used for discovery and shelling out to `brew`. By default it resolves from `ABXPKG_BREW_ROOT`, or `ABXPKG_LIB_DIR/brew`, or a guessed host prefix (`/opt/homebrew`, `/usr/local`, or linuxbrew). `bin_dir` is used for linked formula binaries when abxpkg manages them separately.
- Auto-switching: none. Shells out to `brew` directly.
- `dry_run`: shared behavior.
- Security: `min_release_age` is unsupported and is ignored with a warning if explicitly requested. `postinstall_scripts=False` is supported on `brew install` via `--skip-post-install`, and `ABXPKG_POSTINSTALL_SCRIPTS` hydrates the provider default here. Homebrew has no equivalent flag for `brew upgrade`, so updates run without it.
- Overrides: `install_args` maps to formula / cask args passed to `brew install`, `brew upgrade`, and `brew uninstall`.
- Notes: direct mode runs `brew update` at most once per day. Explicit `--skip-post-install` args in `install_args` win over derived defaults for installs.

</details>

<details>
<summary><h4>🐍 <code>PipProvider</code> (<code>pip</code>)</h4></summary>

Source: [`abxpkg/binprovider_pip.py`](./abxpkg/binprovider_pip.py) • Tests: [`tests/test_pipprovider.py`](./tests/test_pipprovider.py), [`tests/test_security_controls.py`](./tests/test_security_controls.py)

```python
INSTALLER_BIN = "pip"
PATH = ""                            # auto-built from global/user Python bin dirs
install_root = None                  # None = ambient/global mode, Path(...) = provider root
```

- Install root: `install_root=None` uses the system/user Python environment. Set `install_root=Path(...)` for a hermetic provider root whose actual virtualenv lives at `<install_root>/venv`, with executables under `<install_root>/venv/bin` and provider metadata like `derived.env` kept at `<install_root>`.
- Auto-switching: none. Shells out to `pip` directly. Honors `PIP_BINARY=/abs/path/to/pip`. Use `UvProvider` for uv-backed installs.
- `dry_run`: shared behavior.
- Security: supports `postinstall_scripts=False` (always) and `min_release_age` (on pip >= 26.0 or in a freshly bootstrapped pip venv). Hydrated from `ABXPKG_POSTINSTALL_SCRIPTS` and `ABXPKG_MIN_RELEASE_AGE`. For stricter enforcement on hosts with older system pip, use `UvProvider` instead.
- Overrides: `install_args` is passed as pip requirement specs; unpinned specs get a `>=min_version` floor when `min_version` is supplied.
- Notes: `postinstall_scripts=False` adds `pip --only-binary :all:` (wheels only, no arbitrary sdist build scripts). `min_release_age` is enforced with `pip --uploaded-prior-to=<ISO8601>` on pip >= 26.0 (see pypa/pip#13625); older pip silently skips the flag. Explicit conflicting flags already present in `install_args` win over the derived defaults. `get_version` / `get_abspath` fall back to parsing `pip show <package>` output when the console script can't report its own version.

</details>

<details>
<summary><h4>🚀 <code>UvProvider</code> (<code>uv</code>)</h4></summary>

Source: [`abxpkg/binprovider_uv.py`](./abxpkg/binprovider_uv.py) • Tests: [`tests/test_uvprovider.py`](./tests/test_uvprovider.py)

```python
INSTALLER_BIN = "uv"
PATH = ""                            # prepends <install_root>/venv/bin or the uv tool bin dir
install_root = None                  # None = global uv tool mode, Path(...) = provider root
```

- Install root: **two modes, picked by whether `install_root` is set.**
  - *Hermetic venv mode (`install_root=Path(...)`)*: treats `install_root` as a provider root, creates the real venv at `<install_root>/venv` via `uv venv`, and installs packages into it with `uv pip install --python <install_root>/venv/bin/python ...`. Binaries land in `<install_root>/venv/bin/<name>`, while provider metadata like `derived.env` stays at `<install_root>`. This matches `PipProvider`'s layout.
  - *Global tool mode (`install_root=None`)*: delegates to `uv tool install` which creates a fresh venv per tool under `UV_TOOL_DIR` (default `~/.local/share/uv/tools`) and writes shims into `UV_TOOL_BIN_DIR` (default `~/.local/bin`). Pass `bin_dir=Path(...)` to override the shim dir. This is the idiomatic "install a CLI tool globally" path.
- Auto-switching: none. Honors `UV_BINARY=/abs/path/to/uv`. If `uv` isn't on the host, the provider is unavailable.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`, and hydrates their provider defaults from `ABXPKG_MIN_RELEASE_AGE` and `ABXPKG_POSTINSTALL_SCRIPTS`. In both modes, `postinstall_scripts=False` becomes `--no-build` (wheels-only, no arbitrary sdist build scripts) and `min_release_age` becomes `--exclude-newer=<ISO8601>` (uv 0.4+). Explicit conflicting flags already present in `install_args` win over the derived defaults.
- Overrides: `install_args` is passed as requirement specs; unpinned specs get a `>=min_version` floor when `min_version` is supplied.
- Notes: update in venv mode is `uv pip install --upgrade`; update in global mode is `uv tool install --force` (re-installs the tool's venv). Uninstall in venv mode uses `uv pip uninstall --python <venv>/bin/python`; in global mode it uses `uv tool uninstall <name>`.

</details>

<details>
<summary><h4>📦 <code>NpmProvider</code> (<code>npm</code>)</h4></summary>

Source: [`abxpkg/binprovider_npm.py`](./abxpkg/binprovider_npm.py) • Tests: [`tests/test_npmprovider.py`](./tests/test_npmprovider.py), [`tests/test_security_controls.py`](./tests/test_security_controls.py)

```python
INSTALLER_BIN = "npm"
PATH = ""                            # auto-built from npm local + global bin dirs
install_root = None                  # None = global install, Path(...) = prefix/project root
```

- Install root: `install_root=None` installs globally (walks up from the host's `npm prefix` / `npm prefix -g` to seed `PATH`). Set `install_root=Path(...)` to install under `<prefix>/node_modules/.bin`; that prefix bin dir becomes the provider's active executable search path.
- Auto-switching: none. Shells out to `npm` directly and expects `npm` to be installed on the host. Honors `NPM_BINARY=/abs/path/to/npm`. Use `PnpmProvider` for pnpm.
- `dry_run`: shared behavior.
- Security: supports both `postinstall_scripts=False` and `min_release_age`, hydrated from `ABXPKG_POSTINSTALL_SCRIPTS` and `ABXPKG_MIN_RELEASE_AGE`. `min_release_age` requires an npm build that ships `--min-release-age` (detected once by probing `npm install --help`).
- Overrides: `install_args` is passed as npm package specs; unpinned specs get rewritten to `pkg@>=<min_version>` when `min_version` is supplied.
- Notes: `postinstall_scripts=False` adds `--ignore-scripts`; `min_release_age` adds `--min-release-age=<days>`; and installs always include npm's standard non-interactive flags (`--force --no-audit --no-fund --loglevel=error`). `puppeteer` is special-cased to install both `puppeteer` and `@puppeteer/browsers`, and `puppeteer-browsers` resolves to `@puppeteer/browsers`. Explicit conflicting flags already present in `install_args` win over the derived defaults. `get_version` / `get_abspath` fall back to parsing `npm show --json <package>` and `npm list --json --depth=0` output when the console script can't report its own version.

</details>

<details>
<summary><h4>📦 <code>PnpmProvider</code> (<code>pnpm</code>)</h4></summary>

Source: [`abxpkg/binprovider_pnpm.py`](./abxpkg/binprovider_pnpm.py) • Tests: [`tests/test_pnpmprovider.py`](./tests/test_pnpmprovider.py)

```python
INSTALLER_BIN = "pnpm"
PATH = ""                            # auto-built from pnpm local + global bin dirs
install_root = None                  # None = global install, Path(...) = prefix/project root
```

- Install root: `install_root=None` installs globally. Set `install_root=Path(...)` to install under `<prefix>/node_modules/.bin`; that prefix bin dir becomes the provider's active executable search path.
- Shells out to `pnpm` directly. Honors `PNPM_BINARY=/abs/path/to/pnpm`. Use `NpmProvider` for `npm`.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`, and hydrates their provider defaults from `ABXPKG_MIN_RELEASE_AGE` and `ABXPKG_POSTINSTALL_SCRIPTS`. `min_release_age` requires pnpm 10.16+, and `supports_min_release_age()` returns `False` on older hosts (then it logs a warning and continues).
- Overrides: `install_args` is passed as pnpm package specs; unpinned specs get rewritten to `pkg@>=<min_version>` when `min_version` is supplied.
- Notes: pnpm has no `--min-release-age` CLI flag; this provider passes `--config.minimumReleaseAge=<minutes>` (the camelCase / kebab-case form pnpm exposes via its `--config.<key>=<value>` override). Installs always include `--loglevel=error`, and `PNPM_HOME` is auto-populated so `pnpm add -g` works without polluting the user's shell config. `puppeteer` is special-cased to install both `puppeteer` and `@puppeteer/browsers`, and `puppeteer-browsers` resolves to `@puppeteer/browsers`.

</details>

<details>
<summary><h4>🧶 <code>YarnProvider</code> (<code>yarn</code>)</h4></summary>

Source: [`abxpkg/binprovider_yarn.py`](./abxpkg/binprovider_yarn.py) • Tests: [`tests/test_yarnprovider.py`](./tests/test_yarnprovider.py)

```python
INSTALLER_BIN = "yarn"
PATH = ""                            # prepends <install_root>/node_modules/.bin
install_root = None                  # project dir, defaults to ABXPKG_YARN_ROOT or ABXPKG_LIB_DIR/yarn
```

- Install root: Yarn operates inside a project directory. Set `install_root=Path(...)` for an isolated project dir; that directory is auto-initialized with a stub `package.json` and `.yarnrc.yml` (`nodeLinker: node-modules` so binaries land in `<install_root>/node_modules/.bin`). When unset, the provider relies on `$ABXPKG_YARN_ROOT` or `$ABXPKG_LIB_DIR/yarn`; if neither is configured, the provider is unavailable.
- Auto-switching: none. Honors `YARN_BINARY=/abs/path/to/yarn`. Both Yarn classic (1.x) and Yarn Berry (2+) work for basic install/update/uninstall, but only Yarn 4.10+ supports the security flags.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`, and hydrates their provider defaults from `ABXPKG_MIN_RELEASE_AGE` and `ABXPKG_POSTINSTALL_SCRIPTS`. Both controls require Yarn 4.10+; on older hosts `supports_min_release_age()` / `supports_postinstall_disable()` return `False` and explicit values are logged-and-ignored.
- Overrides: `install_args` is passed as Yarn package specs; unpinned specs get rewritten to `pkg@>=<min_version>` when `min_version` is supplied.
- Notes: Yarn has no `--ignore-scripts` / `--minimum-release-age` CLI flags; the provider writes `npmMinimalAgeGate: 7d` (or whatever days value is configured) and `enableScripts: false` into `<install_root>/.yarnrc.yml` and additionally passes `--mode skip-build` to `yarn add` / `yarn up` when `postinstall_scripts=False`. Updates use `yarn up <pkg>` (Berry) or `yarn upgrade <pkg>` (classic). `YARN_GLOBAL_FOLDER` and `YARN_CACHE_FOLDER` are pointed at the provider cache dir so installs share a single cache across workspaces. `puppeteer` is special-cased to install both `puppeteer` and `@puppeteer/browsers`, and `puppeteer-browsers` resolves to `@puppeteer/browsers`.

</details>

<details>
<summary><h4>🥖 <code>BunProvider</code> (<code>bun</code>)</h4></summary>

Source: [`abxpkg/binprovider_bun.py`](./abxpkg/binprovider_bun.py) • Tests: [`tests/test_bunprovider.py`](./tests/test_bunprovider.py)

```python
INSTALLER_BIN = "bun"
PATH = ""                            # prepends <install_root>/bin
install_root = None                  # mirrors $BUN_INSTALL, None = ~/.bun (host-default)
```

- Install root: `install_root=None` writes into the host `$BUN_INSTALL` (default `~/.bun`). Set `install_root=Path(...)` to install under `<install_root>/bin`; the provider also creates `<install_root>/install/global` for the global `node_modules` dir, which is where bun puts the actual package state. The bin dir becomes the provider's active executable search path.
- Auto-switching: none. Honors `BUN_BINARY=/abs/path/to/bun`.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`, and hydrates their provider defaults from `ABXPKG_MIN_RELEASE_AGE` and `ABXPKG_POSTINSTALL_SCRIPTS`. `min_release_age` requires Bun 1.3+, and `supports_min_release_age()` returns `False` on older hosts.
- Overrides: `install_args` is passed as Bun package specs; unpinned specs get rewritten to `pkg@>=<min_version>` when `min_version` is supplied.
- Notes: install/update use `bun add -g` (with `--force` as the update fallback). The provider passes `--ignore-scripts` for `postinstall_scripts=False` and `--minimum-release-age=<seconds>` (Bun's unit is seconds; this provider converts from days). `puppeteer` is special-cased to install both `puppeteer` and `@puppeteer/browsers`, and `puppeteer-browsers` resolves to `@puppeteer/browsers`. Explicit conflicting flags already present in `install_args` win over the derived defaults.

</details>

<details>
<summary><h4>🦕 <code>DenoProvider</code> (<code>deno</code>)</h4></summary>

Source: [`abxpkg/binprovider_deno.py`](./abxpkg/binprovider_deno.py) • Tests: [`tests/test_denoprovider.py`](./tests/test_denoprovider.py)

```python
INSTALLER_BIN = "deno"
PATH = ""                            # prepends <install_root>/bin
install_root = None                  # mirrors $DENO_INSTALL_ROOT, None = ~/.deno
```

- Install root: `install_root=None` writes into the host `$DENO_INSTALL_ROOT` (default `~/.deno`). Set `install_root=Path(...)` for a hermetic root with executables under `<install_root>/bin`; `DENO_DIR` is then derived as `<install_root>/.cache`.
- Auto-switching: none. Honors `DENO_BINARY=/abs/path/to/deno`.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False` / `True`, and hydrates their provider defaults from `ABXPKG_MIN_RELEASE_AGE` and `ABXPKG_POSTINSTALL_SCRIPTS`. `min_release_age` requires Deno 2.5+, and `supports_min_release_age()` returns `False` on older hosts.
- Overrides: `install_args` is passed as `deno install` package specs and is auto-prefixed with `npm:` when an unqualified bare name is supplied. Already-qualified specs (`npm:`, `jsr:`, `https://...`) are passed through verbatim. Unpinned specs get rewritten to `pkg@>=<min_version>` when `min_version` is supplied.
- Notes: install / update both run `deno install -g --force --allow-all -n <bin_name> <pkg>` because Deno's idiomatic update path is just a fresh global install. Deno's npm lifecycle scripts are *opt-in* (the opposite of npm), so the provider only adds `--allow-scripts` when `postinstall_scripts=True`. `min_release_age` is passed as `--minimum-dependency-age=<minutes>` (Deno's preferred unit; this provider converts from days). `puppeteer` is special-cased to install both `puppeteer` and `@puppeteer/browsers`, and `puppeteer-browsers` resolves to `@puppeteer/browsers`. `DENO_TLS_CA_STORE=system` is set so installs work on hosts with corporate / sandboxed CA bundles.

</details>

<details>
<summary><h4>🧪 <code>BashProvider</code> (<code>bash</code>)</h4></summary>

Source: [`abxpkg/binprovider_bash.py`](./abxpkg/binprovider_bash.py) • Tests: [`tests/test_bashprovider.py`](./tests/test_bashprovider.py)

```python
INSTALLER_BIN = "bash"
PATH = ""
install_root = $ABXPKG_BASH_ROOT or $ABXPKG_LIB_DIR/bash
bin_dir = <install_root>/bin
```

- Install root: set `install_root` for the state dir, and `bin_dir` for the executable output dir.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: this provider is driven by literal per-binary shell overrides for `install`, `update`, and `uninstall`.
- Notes: the provider exports `INSTALL_ROOT`, `BIN_DIR`, `BASH_INSTALL_ROOT`, and `BASH_BIN_DIR` into the shell environment for those commands.

</details>

<details>
<summary><h4>🦀 <code>CargoProvider</code> (<code>cargo</code>)</h4></summary>

Source: [`abxpkg/binprovider_cargo.py`](./abxpkg/binprovider_cargo.py) • Tests: [`tests/test_cargoprovider.py`](./tests/test_cargoprovider.py)

```python
INSTALLER_BIN = "cargo"
PATH = ""                            # prepends cargo_root/bin and cargo_home/bin
cargo_root = None                    # set this for hermetic installs
```

- Install root: set `install_root=Path(...)` or `cargo_root=Path(...)` for isolated installs under `<cargo_root>/bin`; otherwise installs go through `cargo_home`.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` is passed to `cargo install`; `min_version` becomes `cargo install --version >=...`.
- Notes: the provider also sets `CARGO_HOME`, `CARGO_TARGET_DIR`, and `CARGO_INSTALL_ROOT` when applicable.

</details>

<details>
<summary><h4>💎 <code>GemProvider</code> (<code>gem</code>)</h4></summary>

Source: [`abxpkg/binprovider_gem.py`](./abxpkg/binprovider_gem.py) • Tests: [`tests/test_gemprovider.py`](./tests/test_gemprovider.py)

```python
INSTALLER_BIN = "gem"
PATH = DEFAULT_ENV_PATH
install_root = None                  # defaults to $GEM_HOME or ~/.local/share/gem
bin_dir = None                       # defaults to <install_root>/bin
```

- Install root: set `install_root`, and optionally `bin_dir`, for hermetic installs; otherwise it uses `$GEM_HOME` or `~/.local/share/gem`.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` maps to `gem install ...`, `gem update ...`, and `gem uninstall ...`; `min_version` becomes `--version >=...`.
- Notes: generated wrapper scripts are patched so they activate the configured `GEM_HOME` instead of the host default.

</details>

<details>
<summary><h4>🐹 <code>GoGetProvider</code> (<code>goget</code>)</h4></summary>

Source: [`abxpkg/binprovider_goget.py`](./abxpkg/binprovider_goget.py) • Tests: [`tests/test_gogetprovider.py`](./tests/test_gogetprovider.py)

```python
INSTALLER_BIN = "go"
PATH = DEFAULT_ENV_PATH
install_root = None                  # defaults to $GOPATH or ~/go
bin_dir = None                       # defaults to <install_root>/bin
```

- Install root: set `install_root` for the Go install tree, and optionally `bin_dir` for the executable dir; otherwise installs land in `<install_root>/bin`.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` is passed to `go install ...`; the default is `["<bin_name>@latest"]`.
- Notes: `update()` is just `install()` again. Version detection prefers `go version -m <binary>` and falls back to the generic version probe. The provider name is `goget`, not `go_get`.

</details>

<details>
<summary><h4>❄️ <code>NixProvider</code> (<code>nix</code>)</h4></summary>

Source: [`abxpkg/binprovider_nix.py`](./abxpkg/binprovider_nix.py) • Tests: [`tests/test_nixprovider.py`](./tests/test_nixprovider.py)

```python
INSTALLER_BIN = "nix"
PATH = ""                            # prepends <install_root>/bin
install_root = $ABXPKG_NIX_PROFILE or ~/.nix-profile
```

- Install root: set `install_root=Path(...)` for a custom profile.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` is passed to `nix profile install ...`; default is `["nixpkgs#<bin_name>"]`.
- Notes: update/uninstall operate on the resolved profile element name rather than reusing the full flake ref.

</details>

<details>
<summary><h4>🐳 <code>DockerProvider</code> (<code>docker</code>)</h4></summary>

Source: [`abxpkg/binprovider_docker.py`](./abxpkg/binprovider_docker.py) • Tests: [`tests/test_dockerprovider.py`](./tests/test_dockerprovider.py)

```python
INSTALLER_BIN = "docker"
PATH = ""                            # prepends bin_dir
bin_dir = ($ABXPKG_DOCKER_ROOT or $ABXPKG_LIB_DIR/docker) / "bin"
```

- Install root: **partial only**. Images are pulled into Docker's host image store; the provider only controls the local shim dir and metadata dir. Use `install_root=Path(...)` for the shim/metadata root or `bin_dir=Path(...)` for the shim dir directly.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` is a list of Docker image refs. The first item is treated as the main image and becomes the generated shim target.
- Notes: default install args are `["<bin_name>:latest"]`. `install()` / `update()` run `docker pull`, write metadata JSON, and create an executable wrapper that runs `docker run ...`. Expects image refs as install args, typically via overrides on a `Binary`. It writes a local wrapper script for the binary and executes it via `docker run ...`; the binary version is parsed from the image tag, so semver-like tags work best.

</details>

<details>
<summary><h4>🧩 <code>ChromeWebstoreProvider</code> (<code>chromewebstore</code>)</h4></summary>

Source: [`abxpkg/binprovider_chromewebstore.py`](./abxpkg/binprovider_chromewebstore.py) • Tests: [`tests/test_chromewebstoreprovider.py`](./tests/test_chromewebstoreprovider.py)

```python
INSTALLER_BIN = "node"
PATH = ""
install_root = $ABXPKG_CHROMEWEBSTORE_ROOT or $ABXPKG_LIB_DIR/chromewebstore
bin_dir = <install_root>/extensions
```

- Install root: set `install_root` for the extension cache root, and `bin_dir` for the unpacked extension output dir.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` is unsupported and is ignored with a warning if explicitly requested. `postinstall_scripts=False` is supported as a standard kwarg and `ABXPKG_POSTINSTALL_SCRIPTS` hydrates the provider default here, but there is no extra install-time toggle beyond the packaged JS runtime path this provider already uses.
- Overrides: `install_args` are `[webstore_id, "--name=<extension_name>"]`.
- Notes: the packaged JS runtime under `abxpkg/js/chrome/` is used to download, unpack, and cache the extension, and the resolved binary path is the unpacked `manifest.json`. `no_cache=True` bypasses that metadata cache on the next install/update without deleting the unpacked extension tree.

</details>

<details>
<summary><h4>🎭 <code>PuppeteerProvider</code> (<code>puppeteer</code>)</h4></summary>

Source: [`abxpkg/binprovider_puppeteer.py`](./abxpkg/binprovider_puppeteer.py) • Tests: [`tests/test_puppeteerprovider.py`](./tests/test_puppeteerprovider.py)

```python
INSTALLER_BIN = "puppeteer-browsers"
PATH = ""
install_root = $ABXPKG_PUPPETEER_ROOT or $ABXPKG_LIB_DIR/puppeteer
bin_dir = <install_root>/bin
cache_dir = <install_root>/cache  # computed; None when install_root is unset
```

- Install root: set `install_root` for the root dir and `bin_dir` for symlinked executables. Leave it unset for ambient/global mode, where cache ownership stays with the host and `INSTALLER_BINARY` must already be resolvable from the ambient provider set.
- Cache dir / `PUPPETEER_CACHE_DIR`: `cache_dir` is a computed property. When `install_root` is pinned it's always `<install_root>/cache`. When `install_root` is unset it reads the ambient `$PUPPETEER_CACHE_DIR` env var so ``load()`` / ``uninstall()`` / scope checks target the same directory the user configured externally (``None`` when the env var is also unset — puppeteer-browsers then falls back to `~/.cache/puppeteer`). The resolved `cache_dir` is exported as `PUPPETEER_CACHE_DIR` to every subprocess the provider runs.
- Auto-switching: bootstraps `@puppeteer/browsers` through `NpmProvider` and then uses that CLI for browser installs.
- `dry_run`: shared behavior.
- Security: `min_release_age` is unsupported for browser installs and is ignored with a warning if explicitly requested. `postinstall_scripts=False` is supported for the underlying npm bootstrap path, and `ABXPKG_POSTINSTALL_SCRIPTS` hydrates the provider default here.
- Overrides: `install_args` are passed through to `@puppeteer/browsers install ...`, with the provider appending `--path=<cache_dir>`. Installing `puppeteer-browsers` itself is treated as the CLI bootstrap case, not as a browser target.
- Notes: installed-browser resolution uses semantic version ordering, not lexicographic string sorting. The provider records `node` and `puppeteer-browsers` as dependency cache entries when they are resolved through upstream providers.

</details>

<details>
<summary><h4>🎬 <code>PlaywrightProvider</code> (<code>playwright</code>)</h4></summary>

Source: [`abxpkg/binprovider_playwright.py`](./abxpkg/binprovider_playwright.py) • Tests: [`tests/test_playwrightprovider.py`](./tests/test_playwrightprovider.py)

```python
INSTALLER_BIN = "playwright"
PATH = ""
install_root = None              # abxpkg-managed root dir for bin_dir / nested npm prefix
bin_dir = <install_root>/bin     # symlink dir for resolved browsers
cache_dir = <install_root>/cache # computed; None when install_root is unset
euid = 0                         # routes exec() through sudo-first-then-fallback
```

- Install root: set `install_root` to pin the abxpkg-managed root dir (where `bin_dir` symlinks and the nested npm prefix live). Leave it unset to let playwright use its own OS-default browsers path (`~/.cache/ms-playwright` on Linux etc.) — in that case abxpkg maintains no symlink dir or npm prefix at all, the `playwright` npm CLI bootstraps against the host's npm default, and `load()` returns the resolved `executablePath()` directly. `bin_dir` overrides the symlink directory when `install_root` is pinned.
- Cache dir / `PLAYWRIGHT_BROWSERS_PATH`: `cache_dir` is a computed property. When `install_root` is pinned it's always `<install_root>/cache`. When `install_root` is unset it reads the ambient `$PLAYWRIGHT_BROWSERS_PATH` env var so ``load()`` / ``uninstall()`` / scope checks target the same directory the user configured externally (``None`` when the env var is also unset — playwright then falls back to `~/.cache/ms-playwright` on Linux). The resolved `cache_dir` is exported as `PLAYWRIGHT_BROWSERS_PATH` to every subprocess (including the `env KEY=VAL -- ...` wrapper used when we go through sudo). `uninstall()` deletes matching `<browser>-*/` dirs from the resolved `cache_dir` — and when neither `install_root` nor `$PLAYWRIGHT_BROWSERS_PATH` is set, it touches nothing, leaving playwright's OS-default cache alone.
- Auto-switching: bootstraps the `playwright` npm package through `NpmProvider`, then runs `playwright install --with-deps <install_args>` against it. Resolves each installed browser's real executable via the `playwright-core` Node.js API (`chromium.executablePath()` etc.) and writes a symlink into `bin_dir` when one is configured.
- `dry_run`: shared behavior — the install handler short-circuits to a placeholder without touching the host.
- Privilege handling: `--with-deps` installs system packages and requires root on Linux. ``euid`` defaults to ``0``, which routes every ``exec()`` call through the base ``BinProvider.exec`` sudo-first-then-fallback path — it tries ``sudo -n -- playwright install --with-deps ...`` first on non-root hosts, falls back to running the command directly if sudo fails or isn't available, and merges both stderr outputs into the final error if both attempts fail.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported for browser installs and are ignored with a warning if explicitly requested.
- Overrides: `install_args` are appended onto `playwright install` after `playwright_install_args` (defaults to `["--with-deps"]`) and passed through verbatim — use whatever browser names / flags the `playwright install` CLI accepts (`chromium`, `firefox`, `webkit`, `--no-shell`, `--only-shell`, `--force`, etc.).
- Notes: `update()` bumps the `playwright` npm package in `install_root` first (via `NpmProvider.update`) so its pinned browser versions refresh, then re-runs `playwright install --force <install_args>` to pull any new browser builds. `uninstall()` removes the relevant `<bin_name>-*/` directories from `install_root` alongside the bin-dir symlink, since `playwright uninstall` only drops *unused* browsers on its own. Both `update()` and `uninstall()` leave playwright's OS-default cache untouched when `install_root` is unset.

</details>

<details>
<summary><h4>🛠️ <code>PyinfraProvider</code> (<code>pyinfra</code>)</h4></summary>

Source: [`abxpkg/binprovider_pyinfra.py`](./abxpkg/binprovider_pyinfra.py) • Tests: [`tests/test_pyinfraprovider.py`](./tests/test_pyinfraprovider.py)

```python
INSTALLER_BIN = "pyinfra"
PATH = os.environ.get("PATH", DEFAULT_PATH)
pyinfra_installer_module = "auto"
pyinfra_installer_kwargs = {}
```

- Install root: **no hermetic prefix support**. It delegates to host package managers through pyinfra operations.
- Auto-switching: `installer_module="auto"` resolves to `operations.brew.packages` on macOS and `operations.server.packages` on Linux.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` is the package list passed to the selected pyinfra operation.
- Notes: privilege requirements depend on the underlying package manager and selected module. When pyinfra tries a privileged sudo path and then falls back, both error outputs are preserved if the final attempt also fails.

</details>

<details>
<summary><h4>📘 <code>AnsibleProvider</code> (<code>ansible</code>)</h4></summary>

Source: [`abxpkg/binprovider_ansible.py`](./abxpkg/binprovider_ansible.py) • Tests: [`tests/test_ansibleprovider.py`](./tests/test_ansibleprovider.py)

```python
INSTALLER_BIN = "ansible"
PATH = os.environ.get("PATH", DEFAULT_PATH)
ansible_installer_module = "auto"
ansible_playbook_template = ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE
```

- Install root: **no hermetic prefix support**. It delegates to the host via `ansible-runner`.
- Auto-switching: `installer_module="auto"` resolves to `community.general.homebrew` on macOS and `ansible.builtin.package` on Linux.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` becomes the playbook loop input for the chosen Ansible module.
- Notes: when using the Homebrew module, the provider auto-injects the detected brew search path into module kwargs. Privilege requirements still come from the underlying package manager, and failed sudo attempts are included in the final error if the fallback attempt also fails.

</details>

### [`Binary`](https://github.com/ArchiveBox/abxpkg/blob/main/abxpkg/binary.py#:~:text=class%20Binary)

Represents a single binary dependency aka a package (e.g. `wget`, `curl`, `ffmpeg`). Each `Binary` can declare one or more `BinProvider`s it supports, along with per-provider overrides.

`Binary`s implement the following interface:
- `load()`, `install()`, `update()`, `uninstall()` `->` `Binary`
- `binproviders`
- `binprovider` / `loaded_binprovider`
- `abspath` / `loaded_abspath`
- `abspaths` / `loaded_abspaths`
- `version` / `loaded_version`
- `sha256` / `loaded_sha256`
- `mtime` / `loaded_mtime`
- `euid` / `loaded_euid`

`Binary.install()` and `Binary.update()` return a fresh loaded `Binary`. `Binary.uninstall()` returns a `Binary` with `binprovider`, `abspath`, `version`, `sha256`, `mtime`, and `euid` cleared after removal. `Binary.load()`, `Binary.install()`, and `Binary.update()` all enforce `min_version` consistently. All four lifecycle methods also accept `no_cache=True` to bypass cached/current-state checks.

```python
from abxpkg import Binary, SemVer, env, brew

curl = Binary(
    name="curl",
    min_version=SemVer("8.0.0"),
    binproviders=[env, brew],
).install()

print(curl.binprovider)   # EnvProvider(...) or BrewProvider(...)
print(curl.abspath)       # Path('/usr/local/bin/curl')
print(curl.version)       # SemVer(8, 4, 0)
print(curl.is_valid)      # True

curl = curl.update()
curl = curl.uninstall()
```

For reusable `Binary` subclasses with per-provider overrides, see [Advanced Usage](#advanced-usage) above.

### [`SemVer`](https://github.com/ArchiveBox/abxpkg/blob/main/abxpkg/semver.py#:~:text=class%20SemVer)

```python
from abxpkg import SemVer

### Example: Use the SemVer type directly for parsing & verifying version strings
SemVer.parse('Google Chrome 124.0.6367.208+beta_234. 234.234.123')  # SemVer(124, 0, 6367)
SemVer.parse('2024.04.05')                                          # SemVer(2024, 4, 5)
SemVer.parse('1.9+beta')                                            # SemVer(1, 9, 0)
str(SemVer(1, 9, 0))                                                # '1.9.0'
```
<br/>

> These types are all meant to be used library-style to make writing your own apps easier.  
> e.g. you can use it to build things like [`playwright install --with-deps`](https://playwright.dev/docs/browsers#install-system-dependencies).


<br/>

---
---

<br/>
<br/>

## Development

`abxpkg` uses `uv` for local development, dependency sync, linting, and tests.

```bash
git clone https://github.com/ArchiveBox/abxpkg && cd abxpkg

# setup the venv and install packages
uv sync --all-extras
source .venv/bin/activate

# run formatting/lint/type checks
uv run prek run --all-files

# run the full test suite from tests/
uv run pytest -sx tests/

# build distributions
uv build && uv publish --username=__token__
```

- Tests live under [`tests/`](./tests/).
- Use `uv run pytest -sx tests/test_npmprovider.py` or a specific node like `uv run pytest -sx tests/test_npmprovider.py::TestNpmProvider::test_provider_dry_run_does_not_install_zx` when iterating on one provider.

<br/>
<br/>


*Note:* this package used to be called `pydantic-pkgr`, it was renamed to `abxpkg` on 2024-11-12.


## Other Packages We Like

- https://github.com/MrThearMan/django-signal-webhooks
- https://github.com/MrThearMan/django-admin-data-views
- https://github.com/lazybird/django-solo
- https://github.com/joshourisman/django-pydantic-settings
- https://github.com/surenkov/django-pydantic-field
- https://github.com/jordaneremieff/djantic

[coverage-badge]: https://coveralls.io/repos/github/ArchiveBox/abxpkg/badge.svg?branch=main
[status-badge]: https://img.shields.io/github/actions/workflow/status/ArchiveBox/abxpkg/test.yml?branch=main
[pypi-badge]: https://img.shields.io/pypi/v/abxpkg?v=1
[licence-badge]: https://img.shields.io/github/license/ArchiveBox/abxpkg?v=1
[repo-badge]: https://img.shields.io/github/last-commit/ArchiveBox/abxpkg?v=1
[issues-badge]: https://img.shields.io/github/issues-raw/ArchiveBox/abxpkg?v=1
[version-badge]: https://img.shields.io/pypi/pyversions/abxpkg?v=1
[downloads-badge]: https://img.shields.io/pypi/dm/abxpkg?v=1
[django-badge]: https://img.shields.io/pypi/djversions/abxpkg?v=1

[coverage]: https://coveralls.io/github/ArchiveBox/abxpkg?branch=main
[status]: https://github.com/ArchiveBox/abxpkg/actions/workflows/test.yml
[pypi]: https://pypi.org/project/abxpkg
[licence]: https://github.com/ArchiveBox/abxpkg/blob/main/LICENSE
[repo]: https://github.com/ArchiveBox/abxpkg/commits/main
[issues]: https://github.com/ArchiveBox/abxpkg/issues

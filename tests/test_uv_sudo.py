# ci-runner: hosted
"""Install managed uv packages with a real shared installer and private home."""

import subprocess
from pathlib import Path

import pytest


@pytest.mark.docker_required
@pytest.mark.parametrize("runtime_uid", [0, 1001])
def test_shared_uv_installs_for_collection_owner(test_machine, runtime_uid):
    docker = test_machine.require_docker_daemon()
    repo = Path(__file__).resolve().parents[1]
    script = r"""
import os
import subprocess
from pathlib import Path
subprocess.run(['useradd', '-u', '911', '-m', 'archivebox'], check=True)
subprocess.run(['useradd', '-u', '1001', '-m', 'runner'], check=True)
# Model the Debian package's shared service-owned uv and private user collection.
# Merely changing HOME would miss the actual child-process privilege failure.
os.chown('/usr/local/bin/uv', 911, 911)
collection = Path('/home/runner/archivebox/data')
collection.mkdir(parents=True)
for directory in (collection.parent, collection):
    os.chown(directory, 1001, 1001)
os.chmod('/home/runner', 0o700)
os.environ.update(HOME='/home/runner', USER='runner', LOGNAME='runner',
                  SUDO_UID='1001', SUDO_GID='1001',
                  XDG_CONFIG_HOME='/home/runner/.config',
                  XDG_CACHE_HOME='/home/runner/.cache')
runtime_uid = __RUNTIME_UID__
if runtime_uid:
    os.setegid(runtime_uid)
    os.seteuid(runtime_uid)
os.chdir(collection)
from abxpkg import UvProvider

assert os.getuid() == 0 and os.geteuid() == runtime_uid
assert Path('/usr/local/bin/uv').stat().st_uid == 911
root = Path('/home/runner/.config/abx/lib/uv')
assert not root.exists()
provider = UvProvider(install_root=root, postinstall_scripts=True, min_release_age=0)
installed = provider.install('cowsay')
assert installed is not None and installed.loaded_abspath is not None
assert provider.EUID == 1001
result = installed.exec(cmd=['-t', 'collection owner'], timeout=30)
assert result.returncode == 0, result.stderr
assert 'collection owner' in result.stdout
# The install must not leave a root/service-owned venv that fails on next use.
assert root.stat().st_uid == 1001
assert all(item.lstat().st_uid == 1001 for item in root.rglob('*'))
again = UvProvider(install_root=root).load('cowsay')
assert again is not None and again.loaded_abspath == installed.loaded_abspath
print('PASS: shared uv preserves collection ownership and executable output')
""".replace("__RUNTIME_UID__", str(runtime_uid))
    result = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "-i",
            "-v",
            f"{repo}:/src:ro",
            "ghcr.io/astral-sh/uv:python3.13-bookworm-slim",
            "bash",
            "-c",
            "uv pip install --system /src && uv run --no-project --no-config python -",
        ],
        input=script,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert (
        "PASS: shared uv preserves collection ownership and executable output"
        in result.stdout
    )

# ci-runner: hosted
"""Use a lib dir that was installed into and then mounted read-only."""

import subprocess
import uuid
from pathlib import Path

import pytest

INSTALL_SCRIPT = r"""
import os
import stat
from pathlib import Path
from abxpkg import EnvProvider, UvProvider

lib = Path('/abx-lib')
installed = UvProvider(
    install_root=lib / 'uv',
    postinstall_scripts=True,
    min_release_age=0,
).install('cowsay')
assert installed is not None and installed.loaded_abspath is not None
# Cache a record for the runtime request, then change the binary's metadata
# so that record is stale once the lib dir is read-only.
loaded = UvProvider(install_root=lib / 'uv').load('cowsay')
assert loaded is not None and loaded.loaded_abspath == installed.loaded_abspath
mode = stat.S_IMODE(os.stat(loaded.loaded_abspath).st_mode)
os.chmod(loaded.loaded_abspath, mode ^ stat.S_IXOTH)
# Image builds also project host tools into env/bin.
bash = EnvProvider(install_root=lib / 'env').load('bash')
assert bash is not None and bash.loaded_abspath == lib / 'env' / 'bin' / 'bash'
print(f'PASS: installed {installed.loaded_abspath}')
"""

READ_ONLY_SCRIPT = r"""
import os
from pathlib import Path
from abxpkg import EnvProvider, UvProvider

lib = Path('/abx-lib')
assert os.statvfs(lib).f_flag & os.ST_RDONLY
before = sorted(lib.rglob('*'))

uv = UvProvider(install_root=lib / 'uv')
# The stale record cannot be dropped from derived.env; loading resolves afresh.
cowsay = uv.load('cowsay')
assert cowsay is not None
assert str(cowsay.loaded_abspath) == os.environ['INSTALLED_ABSPATH']
uv.invalidate_cache('cowsay')
assert uv.load('cowsay').loaded_abspath == cowsay.loaded_abspath
result = cowsay.exec(cmd=['-t', 'read-only lib'], timeout=30)
assert result.returncode == 0, result.stderr
assert 'read-only lib' in result.stdout

# uv locks its cache even to read package metadata.
installer = uv.INSTALLER_BINARY()
assert installer is not None and installer.loaded_abspath is not None
shown = uv.exec(
    bin_name=installer.loaded_abspath,
    cmd=['pip', 'show', '--python', str(cowsay.loaded_abspath.with_name('python')), 'cowsay'],
    timeout=30,
)
assert shown.returncode == 0, shown.stderr
assert 'Name: cowsay' in shown.stdout

# Existing projections keep working; new ones cannot be created, so the host
# binary is used directly, also for a provider root the build never created.
bash = EnvProvider(install_root=lib / 'env').load('bash')
assert bash is not None and bash.loaded_abspath == lib / 'env' / 'bin' / 'bash'
for env_root in (lib / 'env', lib / 'never-installed'):
    uname = EnvProvider(install_root=env_root).load('uname')
    assert uname is not None and uname.loaded_abspath is not None
    assert not uname.loaded_abspath.is_relative_to(lib)
    result = uname.exec(cmd=['-s'], timeout=30)
    assert result.stdout.strip() == 'Linux', result.stderr

assert sorted(lib.rglob('*')) == before
print('PASS: read-only lib dir resolves installed and host binaries')
"""


@pytest.mark.docker_required
def test_read_only_lib_dir_resolves_installed_and_host_binaries(test_machine):
    docker = test_machine.require_docker_daemon()
    repo = Path(__file__).resolve().parents[1]
    volume = f"abxpkg-read-only-lib-{uuid.uuid4().hex[:12]}"

    def run_in_container(
        lib_mount: str,
        script: str,
        installed_abspath: str = "",
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "-i",
                "-e",
                "ABXPKG_LIB_DIR=/abx-lib",
                "-e",
                f"INSTALLED_ABSPATH={installed_abspath}",
                "-v",
                f"{repo}:/src:ro",
                "-v",
                lib_mount,
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

    subprocess.run(
        [docker, "volume", "create", volume],
        check=True,
        capture_output=True,
    )
    try:
        installed = run_in_container(f"{volume}:/abx-lib", INSTALL_SCRIPT)
        assert installed.returncode == 0, installed.stderr
        installed_abspath = installed.stdout.split("PASS: installed ", 1)[1].strip()
        assert installed_abspath.startswith("/abx-lib/uv/")

        read_only = run_in_container(
            f"{volume}:/abx-lib:ro",
            READ_ONLY_SCRIPT,
            installed_abspath,
        )
        assert read_only.returncode == 0, read_only.stderr
        assert (
            "PASS: read-only lib dir resolves installed and host binaries"
            in read_only.stdout
        )
    finally:
        subprocess.run(
            [docker, "volume", "rm", "-f", volume],
            check=False,
            capture_output=True,
        )

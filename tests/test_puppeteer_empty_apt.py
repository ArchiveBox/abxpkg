# ci-runner: hosted
"""Install and launch stable Chrome in a real slim image with empty APT lists."""

import subprocess
import uuid
from pathlib import Path

import pytest


@pytest.mark.docker_required
@pytest.mark.parametrize("run_as_uid", [0, 911])
def test_stable_chrome_installs_system_dependencies_with_empty_apt_lists(
    test_machine,
    run_as_uid,
):
    docker = test_machine.require_docker_daemon()
    repo = Path(__file__).resolve().parents[1]
    image = f"abxpkg-empty-apt-test:{uuid.uuid4().hex}"
    dockerfile = """
FROM ghcr.io/astral-sh/uv:0.10.6 AS uv
FROM docker.io/library/node:24.18.0-trixie-slim
COPY --from=uv /uv /usr/bin/uv
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends ca-certificates unzip sudo && useradd --uid 911 --create-home abxtest && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_PREFERENCE=only-managed UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_CACHE_DIR=/tmp/uv-cache ABXPKG_LIB_DIR=/opt/abxpkg ABXPKG_POSTINSTALL_SCRIPTS=true
WORKDIR /tmp
"""
    script = """
from pathlib import Path
from abxpkg import Binary, PuppeteerProvider
import fcntl
import threading
import psutil
import os
import pwd

run_as_uid = __RUN_AS_UID__
if run_as_uid:
    account = pwd.getpwuid(run_as_uid)
    Path('/opt/abxpkg').mkdir()
    os.chown('/opt/abxpkg', run_as_uid, account.pw_gid)
    os.setegid(account.pw_gid)
    os.seteuid(run_as_uid)
    os.environ['HOME'] = account.pw_dir
    os.environ['USER'] = account.pw_name
    os.environ['LOGNAME'] = account.pw_name
assert os.getuid() == 0 and os.geteuid() == run_as_uid

observed_satisfy = threading.Event()
unlocked_satisfy = []
finished = threading.Event()
def observe_real_apt():
    while not finished.wait(0.005):
        for proc in psutil.Process().children(recursive=True):
            try:
                args = proc.cmdline()
                if not args or Path(args[0]).name != 'apt-get' or 'satisfy' not in args:
                    continue
                observed_satisfy.set()
                with open('/tmp/abxpkg-apt.lock') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    else:
                        fcntl.flock(lock, fcntl.LOCK_UN)
                        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                            unlocked_satisfy.append(args)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
observer = threading.Thread(target=observe_real_apt)
observer.start()

lists = Path('/var/lib/apt/lists')
assert not any(p.is_file() and p.name != 'lock' for p in lists.iterdir())
browser = Binary(
    name='chromium', binproviders=[PuppeteerProvider(postinstall_scripts=True, min_release_age=0)],
    min_version='155.0.8059.79', max_version='155.0.8059.79',
    postinstall_scripts=True, min_release_age=0,
    overrides={'puppeteer': {'install_args': ['chrome@155.0.8059.79', '--install-deps']}},
)
try:
    browser = browser.install()
finally:
    finished.set()
    observer.join()
assert observed_satisfy.is_set(), 'No real apt-get satisfy observed'
assert not unlocked_satisfy, f'Puppeteer ran APT without its shared lock: {unlocked_satisfy[:1]}'
assert browser.loaded_abspath and browser.loaded_abspath.is_file()
assert browser.loaded_abspath.stat().st_uid == run_as_uid
proc = browser.exec(cmd=['--headless', '--no-sandbox', '--disable-gpu', '--dump-dom',
                        'data:text/html,<h1>Actual stable Chrome works</h1>'], timeout=30)
assert proc.returncode == 0, proc.stderr
assert '<h1>Actual stable Chrome works</h1>' in proc.stdout, proc.stdout
indexes = {p.name: p.stat().st_mtime_ns for p in lists.iterdir() if p.is_file() and p.name != 'lock'}
assert indexes
again = browser.install()
assert again.loaded_abspath == browser.loaded_abspath
assert indexes == {p.name: p.stat().st_mtime_ns for p in lists.iterdir() if p.is_file() and p.name != 'lock'}
print('PASS: empty-index install, stable Chrome DOM, unchanged indexes on reuse')
""".replace("__RUN_AS_UID__", str(run_as_uid))
    try:
        build = subprocess.run(
            [docker, "build", "-t", image, "-"],
            input=dockerfile,
            text=True,
            capture_output=True,
            timeout=300,
        )
        assert build.returncode == 0, build.stderr
        run = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "-i",
                "-v",
                f"{repo}:/src:ro",
                image,
                "uv",
                "run",
                "--no-project",
                "--python",
                "3.13",
                "--with",
                "/src",
                "--with",
                "psutil",
                "python",
                "-",
            ],
            input=script,
            text=True,
            capture_output=True,
            timeout=300,
        )
        assert run.returncode == 0, run.stderr
        assert (
            "PASS: empty-index install, stable Chrome DOM, unchanged indexes on reuse"
            in run.stdout
        )
    finally:
        subprocess.run(
            [docker, "image", "rm", image],
            capture_output=True,
            text=True,
            check=False,
        )

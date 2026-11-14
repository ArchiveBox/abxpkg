import os
import shutil
from pathlib import Path

from abxpkg import EnvProvider
from abxpkg.config import _fingerprints_match


def test_fingerprints_survive_subsecond_timestamp_loss(tmp_path):
    path = tmp_path / "metadata.py"
    shutil.copyfile(__file__, path)
    timestamp = 1_700_000_000_987_654_321
    os.utime(path, ns=(timestamp, timestamp))
    provider = EnvProvider()
    before = provider._fingerprint_paths([path])
    rounded = timestamp // 1_000_000_000 * 1_000_000_000
    os.utime(path, ns=(rounded, rounded))
    assert provider._fingerprint_paths([path]) == before
    assert _fingerprints_match(before)


def test_fingerprints_still_reject_timestamp_size_and_mode_changes(tmp_path):
    path = tmp_path / "metadata.py"
    shutil.copyfile(Path(__file__), path)
    provider = EnvProvider()
    before = provider._fingerprint_paths([path])
    original = path.stat()
    later = original.st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(later, later))
    assert not _fingerprints_match(before)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    path.chmod(original.st_mode ^ 0o100)
    assert not _fingerprints_match(before)
    path.chmod(original.st_mode)
    with path.open("ab") as output:
        output.write(b"\n")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert not _fingerprints_match(before)

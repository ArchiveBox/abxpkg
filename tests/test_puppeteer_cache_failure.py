"""Only explicit incomplete-cache diagnostics may discard a browser download."""

import pytest

from abxpkg import PuppeteerProvider


@pytest.mark.parametrize(
    "reason",
    [
        "Installing system dependencies requires root privileges",
        "apt-get satisfy failed with exit code 100",
    ],
)
def test_dependency_failure_preserves_browser_cache(tmp_path, reason):
    provider = PuppeteerProvider(install_root=tmp_path)
    build = tmp_path / "cache" / "chrome" / "linux-157.0.8103.38"
    build.mkdir(parents=True)
    receipt = build / "download.receipt"
    receipt.write_text("completed browser download\n")
    output = (
        f"All providers failed for chrome 157.0.8103.38:\n  - DefaultProvider: {reason}"
    )

    assert not provider._cleanup_partial_browser_cache(output, "chrome")
    assert receipt.read_text() == "completed browser download\n"


def test_explicit_missing_executable_discards_incomplete_cache(tmp_path):
    provider = PuppeteerProvider(install_root=tmp_path)
    build = tmp_path / "cache" / "chrome" / "linux-157.0.8103.38"
    build.mkdir(parents=True)
    (build / "interrupted-download").write_bytes(b"partial download")
    output = f"The browser folder ({build}) exists but the executable is missing"

    assert provider._cleanup_partial_browser_cache(output, "chrome")
    assert not build.exists()

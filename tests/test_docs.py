import re
import subprocess
from pathlib import Path

import abxpkg


def test_landing_page_renders_all_providers_in_fresh_process(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "docs/generate.py",
            "--output-dir",
            str(tmp_path),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Generated abxpkg landing page" in result.stdout
    html = (tmp_path / "index.html").read_text()
    provider_names = re.findall(r'data-hero-plugin="([^"]+)"', html)
    assert set(provider_names) == set(abxpkg.ALL_PROVIDER_NAMES)
    assert len(provider_names) == len(set(provider_names))
    for provider_name in provider_names:
        assert f"abxpkg --binproviders={provider_name} install " in html
    assert "INSTALLER_BIN" in html
    assert "ABXPKG_PIP_ROOT" in html
    assert (tmp_path / ".nojekyll").is_file()
    assert list((tmp_path / "css").glob("*.css"))

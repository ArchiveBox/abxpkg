"""Exercise discovery against the real test files and runner declarations."""

import json
import os
import subprocess
import unittest
from pathlib import Path


class MatrixTests(unittest.TestCase):
    def test_linux_coverage_and_scheduled_mac_assignments(self):
        root = Path(__file__).resolve().parents[1]
        files = sorted(root.glob("tests/test_*.py"))
        ordinary = [
            path
            for path in files
            if not any(
                marker in path.read_text()
                for marker in (
                    "@pytest.mark.root_required",
                    "@pytest.mark.docker_required",
                )
            )
        ]
        previous_mac = {str(path.relative_to(root)) for path in ordinary[1::2]}
        for platform in ("linux", "macos"):
            for capacity in (0, 3, 12):
                with self.subTest(platform=platform, capacity=capacity):
                    result = subprocess.run(
                        [
                            "uv",
                            "run",
                            "--no-project",
                            "python",
                            ".github/ci_test_matrix.py",
                        ],
                        cwd=root,
                        env={
                            **os.environ,
                            "CI_TEST_PLATFORM": platform,
                            "UGNAS_CI_MAX_JOBS": str(capacity),
                        },
                        text=True,
                        capture_output=True,
                        check=True,
                    )
                    matrix = json.loads(result.stdout.removeprefix("tests="))
                    paths = [entry["path"] for entry in matrix]
                    expected = (
                        {str(path.relative_to(root)) for path in files}
                        if platform == "linux"
                        else previous_mac
                    )
                    self.assertEqual(set(paths), expected)
                    self.assertEqual(len(paths), len(expected))
                    self.assertEqual(
                        {entry["os"] for entry in matrix},
                        {"ubuntu-24.04" if platform == "linux" else "macOS-15"},
                    )
                    self.assertEqual(
                        {entry["python_version"] for entry in matrix},
                        {"3.12", "3.13", "3.14"},
                    )
                    nas = [entry for entry in matrix if entry["ugnas"]]
                    self.assertEqual(len(nas), capacity if platform == "linux" else 0)
                    for entry in nas:
                        source = (root / entry["path"]).read_text()
                        self.assertNotIn("# ci-runner: hosted", source.splitlines()[:5])
                        self.assertNotIn("@pytest.mark.docker_required", source)


if __name__ == "__main__":
    unittest.main()

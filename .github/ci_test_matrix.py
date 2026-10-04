from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

PRIVILEGED_MARKER = re.compile(r"@pytest\.mark\.(root_required|docker_required)")
SUPPORTED_TARGETS = (
    {
        "os": "ubuntu-24.04",
        "os_name": "linux",
        "python_version": "3.12",
    },
    {
        "os": "macOS-15",
        "os_name": "macOS",
        "python_version": "3.12",
    },
    {
        "os": "ubuntu-24.04",
        "os_name": "linux",
        "python_version": "3.13",
    },
    {
        "os": "macOS-15",
        "os_name": "macOS",
        "python_version": "3.13",
    },
    {
        "os": "ubuntu-24.04",
        "os_name": "linux",
        "python_version": "3.14",
    },
    {
        "os": "macOS-15",
        "os_name": "macOS",
        "python_version": "3.14",
    },
)
LINUX_TARGETS = tuple(
    target for target in SUPPORTED_TARGETS if target["os_name"] == "linux"
)


def discover_tests() -> list[Path]:
    tests = sorted(Path("tests").glob("test_*.py"))
    if not tests:
        raise SystemExit("No test files were discovered")
    return tests


def build_matrix(test_paths: list[Path]) -> list[dict[str, object]]:
    platform = os.environ.get("CI_TEST_PLATFORM", "linux")
    if platform not in {"linux", "macos"}:
        raise SystemExit("CI_TEST_PLATFORM must be linux or macos")
    durations = json.loads(
        Path(__file__).with_name("ci_test_durations.json").read_text(),
    )
    matrix: list[dict[str, object]] = []
    expected_paths = []
    ordinary_index = 0
    privileged_index = 0

    for test_path in test_paths:
        is_privileged = bool(
            PRIVILEGED_MARKER.search(test_path.read_text(encoding="utf-8")),
        )
        if is_privileged:
            target = LINUX_TARGETS[privileged_index % len(LINUX_TARGETS)]
            privileged_index += 1
        else:
            target = SUPPORTED_TARGETS[ordinary_index % len(SUPPORTED_TARGETS)]
            ordinary_index += 1

        if platform == "macos" and target["os_name"] != "macOS":
            continue
        if platform == "linux":
            target = {**target, "os": "ubuntu-24.04", "os_name": "linux"}
        expected_paths.append(str(test_path))
        matrix.append(
            {
                "name": test_path.stem.removeprefix("test_"),
                "path": str(test_path),
                **target,
                "ugnas": False,
            },
        )

    assigned = Counter(entry["path"] for entry in matrix)
    expected = Counter(expected_paths)
    if assigned != expected:
        raise SystemExit("Every discovered test file must be assigned exactly once")

    used_targets = {
        (entry["os"], entry["python_version"])
        for entry in matrix
        if entry["path"]
        not in {
            str(test_path)
            for test_path in test_paths
            if PRIVILEGED_MARKER.search(test_path.read_text(encoding="utf-8"))
        }
    }
    expected_targets = {
        (
            "ubuntu-24.04" if platform == "linux" else "macOS-15",
            target["python_version"],
        )
        for target in SUPPORTED_TARGETS
    }
    if used_targets != expected_targets:
        raise SystemExit("Ordinary tests must cover every supported OS/Python target")

    # Run long files first; shorter files fill the bounded NAS pool in waves.
    # Estimates are measured test-step seconds, not cached test outcomes.
    matrix.sort(key=lambda entry: durations.get(entry["path"], 60), reverse=True)
    eligible = [
        entry
        for entry in reversed(matrix)
        if entry["os_name"] == "linux"
        and "# ci-runner: hosted"
        not in Path(str(entry["path"])).read_text().splitlines()[:5]
    ]
    capacity = max(0, int(os.environ.get("UGNAS_CI_MAX_JOBS", "3")))
    for entry in eligible[:capacity]:
        entry["ugnas"] = True
    return matrix


if __name__ == "__main__":
    print(f"tests={json.dumps(build_matrix(discover_tests()), separators=(',', ':'))}")

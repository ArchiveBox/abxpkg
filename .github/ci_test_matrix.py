from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


PRIVILEGED_MARKER = re.compile(r"@pytest\.mark\.(root_required|docker_required)")
SUPPORTED_TARGETS = (
    {
        "os": "ubuntu-24.04",
        "os_name": "linux",
        "python_version": "3.12.13",
    },
    {
        "os": "macOS-15",
        "os_name": "macOS",
        "python_version": "3.12.10",
    },
    {
        "os": "ubuntu-24.04",
        "os_name": "linux",
        "python_version": "3.13.14",
    },
    {
        "os": "macOS-15",
        "os_name": "macOS",
        "python_version": "3.13.13",
    },
    {
        "os": "ubuntu-24.04",
        "os_name": "linux",
        "python_version": "3.14.6",
    },
    {
        "os": "macOS-15",
        "os_name": "macOS",
        "python_version": "3.14.6",
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
    matrix: list[dict[str, object]] = []
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

        matrix.append(
            {
                "name": test_path.stem.removeprefix("test_"),
                "path": str(test_path),
                **target,
            },
        )

    assigned = Counter(entry["path"] for entry in matrix)
    expected = Counter(str(test_path) for test_path in test_paths)
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
        (target["os"], target["python_version"]) for target in SUPPORTED_TARGETS
    }
    if used_targets != expected_targets:
        raise SystemExit("Ordinary tests must cover every supported OS/Python target")

    return matrix


if __name__ == "__main__":
    print(f"tests={json.dumps(build_matrix(discover_tests()), separators=(',', ':'))}")

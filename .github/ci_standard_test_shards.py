from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


SHARD_COUNT = 8
PRIVILEGED_MARKER = re.compile(r"@pytest\.mark\.(root_required|docker_required)")
PLAYWRIGHT_TEST = Path("tests/test_playwrightprovider.py")

# Relative weights are based on successful Linux matrix runtimes. The greedy
# assignment keeps the slow provider files apart while still giving every
# exact OS/Python cell the same deterministic set of shards.
TEST_WEIGHTS = {
    "test_cli.py": 256,
    "test_cargoprovider.py": 210,
    "test_pipprovider.py": 210,
    "test_npmprovider.py": 170,
    "test_brewprovider.py": 166,
    "test_central_lib_dir.py": 159,
    "test_binprovider.py": 148,
    "test_ansibleprovider.py": 128,
    "test_pnpmprovider.py": 123,
    "test_yarnprovider.py": 118,
    "test_pyinfraprovider.py": 109,
    "test_binary.py": 99,
    "test_puppeteerprovider.py": 88,
    "test_envprovider.py": 83,
    "test_denoprovider.py": 79,
    "test_gogetprovider.py": 79,
    "test_bunprovider.py": 75,
    "test_installer_binary_contracts.py": 72,
    "test_nixprovider.py": 72,
    "test_security_controls.py": 71,
    "test_gemprovider.py": 63,
    "test_install.py": 49,
    "test_uvprovider.py": 47,
    "test_bashprovider.py": 45,
    "test_binary_service.py": 38,
    "test_chromewebstoreprovider.py": 35,
    "test_semver.py": 30,
    "test_module_api.py": 29,
}


def discover_standard_tests() -> list[Path]:
    tests = []
    for test_path in sorted(Path("tests").glob("test_*.py")):
        if not PRIVILEGED_MARKER.search(test_path.read_text(encoding="utf-8")):
            tests.append(test_path)
    if not tests:
        raise SystemExit("No standard test files were discovered")
    return tests


def build_shards(test_paths: list[Path]) -> list[dict[str, object]]:
    shard_count = min(SHARD_COUNT, len(test_paths))
    shards: list[list[Path]] = [[] for _ in range(shard_count)]
    shard_weights = [0] * shard_count

    weighted_tests = sorted(
        test_paths,
        key=lambda path: (-TEST_WEIGHTS.get(path.name, 1), str(path)),
    )
    for test_path in weighted_tests:
        shard_index = min(
            range(shard_count),
            key=lambda index: (shard_weights[index], index),
        )
        shards[shard_index].append(test_path)
        shard_weights[shard_index] += TEST_WEIGHTS.get(test_path.name, 1)

    assigned = Counter(test_path for shard in shards for test_path in shard)
    if set(assigned) != set(test_paths) or any(
        count != 1 for count in assigned.values()
    ):
        raise SystemExit("Standard test shard assignment must contain every file once")

    return [
        {
            "name": f"shard-{index + 1}",
            "paths": [str(path) for path in sorted(shard)],
        }
        for index, shard in enumerate(shards)
    ]


if __name__ == "__main__":
    standard_tests = discover_standard_tests()
    if PLAYWRIGHT_TEST not in standard_tests:
        raise SystemExit(
            f"Dedicated Playwright test was not discovered: {PLAYWRIGHT_TEST}",
        )

    ordinary_tests = [
        test_path for test_path in standard_tests if test_path != PLAYWRIGHT_TEST
    ]
    test_shards = build_shards(ordinary_tests)
    assigned: Counter[Path] = Counter()
    for shard in test_shards:
        shard_paths = shard["paths"]
        if not isinstance(shard_paths, list):
            raise SystemExit(f"Invalid shard path list: {shard!r}")
        assigned.update(Path(str(test_path)) for test_path in shard_paths)
    assigned[PLAYWRIGHT_TEST] += 1
    expected = Counter(standard_tests)
    if assigned != expected:
        raise SystemExit(
            "Ordinary shards plus the dedicated Playwright test must contain "
            "every standard test file exactly once",
        )

    print(f"test-shards={json.dumps(test_shards)}")
    print(f"playwright-test={PLAYWRIGHT_TEST}")

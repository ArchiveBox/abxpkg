#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

uv run --no-project python - "${1:-}" <<'PY'
from pathlib import Path
import re
import sys

path = Path('pyproject.toml')
text = path.read_text()
match = re.search(r'^version = "([^"]+)"$', text, re.MULTILINE)
if not match:
    raise SystemExit('Failed to find version in pyproject.toml')

current = match.group(1)
requested = sys.argv[1]
pattern = re.compile(r'\d+\.\d+\.\d+(?:rc\d+)?')
def parse(value: str) -> tuple[int, int, int, int]:
    major, minor, tail = value.split('.')
    patch, _, rc = tail.partition('rc')
    return (int(major), int(minor), int(patch), int(rc) if rc else 10_000)

if not pattern.fullmatch(current):
    raise SystemExit(f'Unsupported current version: {current}')
if requested:
    if not pattern.fullmatch(requested):
        raise SystemExit(f'Unsupported requested version: {requested}')
    version = requested
else:
    major, minor, patch = map(int, current.split('rc', 1)[0].split('.'))
    version = f'{major}.{minor}.{patch + 1}'

if version == current:
    raise SystemExit(f'Version is already {current}')
if parse(version) <= parse(current):
    raise SystemExit(f'New version {version} must be greater than {current}')
path.write_text(re.sub(r'^version = "[^"]+"$', f'version = "{version}"', text, count=1, flags=re.MULTILINE))
print(version)
PY

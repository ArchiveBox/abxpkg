#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="$REPO_ROOT/.github/ci_binaries.json"
export ABXPKG_LIB_DIR="${ABXPKG_LIB_DIR:-${RUNNER_TEMP:-$REPO_ROOT/.abxpkg}/lib}"

if [[ "$#" -eq 0 ]]; then
  echo "usage: $0 CONFIG_SECTION [...]" >&2
  exit 2
fi

env_json="$(
  args=()
  for section in "$@"; do
    args+=("--deps-from=$CONFIG_PATH:$section")
  done
  uv run --project "$REPO_ROOT" abxpkg env --install --json "${args[@]}"
)"

ABXPKG_CI_ENV_JSON="$env_json" uv run --project "$REPO_ROOT" python - <<'PY'
import json
import os
from pathlib import Path

from abxpkg.base_types import is_forbidden_convenience_lib_bin

values = json.loads(os.environ['ABXPKG_CI_ENV_JSON'])
values['ABXPKG_LIB_DIR'] = os.environ['ABXPKG_LIB_DIR']
projected_path = str(values.get('PATH', ''))
host_path = os.environ.get('PATH', '')
values['PATH'] = os.pathsep.join(dict.fromkeys(
    entry
    for entry in (*projected_path.split(os.pathsep), *host_path.split(os.pathsep))
    if entry and not is_forbidden_convenience_lib_bin(entry)
))

env_file = os.environ.get('GITHUB_ENV')
if not env_file:
    raise SystemExit('GITHUB_ENV is required')

with Path(env_file).open('a', encoding='utf-8') as env_out:
    for key, value in sorted(values.items()):
        text = str(value)
        if '\n' in text:
            raise SystemExit(f'{key} contains an unsupported newline')
        env_out.write(f'{key}={text}\n')

for key in sorted(key for key in values if key.startswith('CI_') and key.endswith('_BIN')):
    print(f'{key}={values[key]}')
PY

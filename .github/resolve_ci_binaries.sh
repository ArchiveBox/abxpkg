#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="$REPO_ROOT/.github/ci_binaries.json"
export ABXPKG_LIB_DIR="${ABXPKG_LIB_DIR:-${RUNNER_TEMP:-$REPO_ROOT/.abxpkg}/lib}"

if [[ "$#" -eq 0 ]]; then
  echo "usage: $0 CONFIG_SECTION [...]" >&2
  exit 2
fi

# Homebrew installs can replace shared libraries used by already-discovered
# Homebrew binaries. Resolve every system toolchain first, then discover and
# project host language runtimes from the final host state. This keeps host
# binaries first when they remain compatible and lets abxpkg select the managed
# provider when a package-manager mutation invalidated one.
requested_sections=("$@")
section_priority=(
  manager_binaries
  linux_binaries
  go_binaries
  cargo_binaries
  gem_binaries
  python_cli_binaries
  node_npm_binaries
  pnpm_binaries
  yarn_binaries
  bun_binaries
  deno_binaries
  host_utility_binaries
  docker_binaries
)
ordered_sections=()
for priority_section in "${section_priority[@]}"; do
  for requested_section in "${requested_sections[@]}"; do
    if [[ "$requested_section" == "$priority_section" ]]; then
      ordered_sections+=("$requested_section")
      break
    fi
  done
done
for requested_section in "${requested_sections[@]}"; do
  section_ordered=false
  for ordered_section in "${ordered_sections[@]}"; do
    if [[ "$requested_section" == "$ordered_section" ]]; then
      section_ordered=true
      break
    fi
  done
  if [[ "$section_ordered" == false ]]; then
    ordered_sections+=("$requested_section")
  fi
done

env_json="$(
  args=()
  for section in "${ordered_sections[@]}"; do
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

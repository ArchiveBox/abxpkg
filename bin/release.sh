#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

TAG_PREFIX="v"
PYPI_PACKAGE="abxpkg"
REQUIRED_WORKFLOWS=(
    "tests.yml|Run Tests"
)

source_optional_env() {
    if [[ -f "${REPO_DIR}/.env" ]]; then
        set -a
        # shellcheck disable=SC1091
        source "${REPO_DIR}/.env"
        set +a
    fi
}

repo_slug() {
    if [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
        printf '%s\n' "${GITHUB_REPOSITORY}"
        return
    fi

    git remote get-url origin | sed -E 's#^git@github\.com:##; s#^https://github\.com/##; s#\.git$##'
}

current_version() {
    uv run --no-project python - <<'PY'
from pathlib import Path
import re

text = Path('pyproject.toml').read_text()
match = re.search(r'^version = "([^"]+)"$', text, re.MULTILINE)
if not match:
    raise SystemExit('Failed to find version in pyproject.toml')
print(match.group(1))
PY
}

compare_versions() {
    uv run --no-project python - "$1" "$2" <<'PY'
import re
import sys

def parse(version: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?', version)
    if not match:
        raise SystemExit(f'Unsupported version format: {version}')
    major, minor, patch, rc = match.groups()
    return (int(major), int(minor), int(patch), int(rc) if rc is not None else 10_000)

left, right = sys.argv[1], sys.argv[2]
print('gt' if parse(left) > parse(right) else 'eq' if parse(left) == parse(right) else 'lt')
PY
}

latest_release_version() {
    local slug="$1"
    local raw_tags
    raw_tags="$(gh api "repos/${slug}/releases?per_page=100" --jq '.[].tag_name' || true)"
    RELEASE_TAGS="${raw_tags}" TAG_PREFIX_VALUE="${TAG_PREFIX}" uv run --no-project python - <<'PY'
import os
import re

def parse(version: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?', version)
    if not match:
        return (-1, -1, -1, -1)
    major, minor, patch, rc = match.groups()
    return (int(major), int(minor), int(patch), int(rc) if rc is not None else 10_000)

prefix = os.environ['TAG_PREFIX_VALUE']
versions = [tag.removeprefix(prefix) for tag in os.environ.get('RELEASE_TAGS', '').splitlines()]
versions = [version for version in versions if parse(version) != (-1, -1, -1, -1)]
print(max(versions, key=parse) if versions else '')
PY
}

latest_pypi_version() {
    local releases
    releases="$(curl -fsSL "https://pypi.org/pypi/${PYPI_PACKAGE}/json" | jq -r '.releases | keys[]' || true)"
    RELEASE_TAGS="${releases}" uv run --no-project python - <<'PY'
import os
import re

def parse(version: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?', version)
    if not match:
        return (-1, -1, -1, -1)
    major, minor, patch, rc = match.groups()
    return (int(major), int(minor), int(patch), int(rc) if rc is not None else 10_000)

versions = [version for version in os.environ.get('RELEASE_TAGS', '').splitlines() if parse(version) != (-1, -1, -1, -1)]
print(max(versions, key=parse) if versions else '')
PY
}

require_clean_exact_checkout() {
    local release_sha="$1"
    local release_branch="$2"

    if [[ ! "${release_sha}" =~ ^[0-9a-f]{40}$ ]]; then
        echo "RELEASE_SHA must be a full 40-character commit SHA" >&2
        return 1
    fi
    if [[ "$(git rev-parse HEAD)" != "${release_sha}" ]]; then
        echo "Refusing to release: checkout HEAD does not match RELEASE_SHA ${release_sha}" >&2
        return 1
    fi
    if [[ -n "$(git status --short)" ]]; then
        echo "Refusing to release from a dirty worktree" >&2
        return 1
    fi
    git fetch --quiet --no-tags origin "+refs/heads/${release_branch}:refs/remotes/origin/${release_branch}"
    if ! git merge-base --is-ancestor "${release_sha}" "refs/remotes/origin/${release_branch}"; then
        echo "Refusing to release ${release_sha}: it is not on ${release_branch}" >&2
        return 1
    fi
}

require_successful_workflows() {
    local slug="$1"
    local release_sha="$2"
    local workflow_spec workflow_file workflow_name runs state run_id attempts

    for workflow_spec in "${REQUIRED_WORKFLOWS[@]}"; do
        workflow_file="${workflow_spec%%|*}"
        workflow_name="${workflow_spec#*|}"
        attempts=0

        run_id=""
        while [[ "${attempts}" -lt 12 ]]; do
            runs="$(env -u GH_FORCE_TTY GH_PROMPT_DISABLED=1 GH_PAGER=cat NO_COLOR=1 gh run list \
                --repo "${slug}" \
                --workflow "${workflow_file}" \
                --event push \
                --commit "${release_sha}" \
                --limit 10 \
                --json databaseId,workflowName,headSha,status,conclusion,event)"
            state="$(jq -r --arg name "${workflow_name}" --arg sha "${release_sha}" '
                [.[] | select(.workflowName == $name and .headSha == $sha and .event == "push")]
                | if length == 1
                  then (.[0].databaseId | tostring)
                  elif length == 0 then "missing"
                  else "ambiguous"
                  end
            ' <<<"${runs}")"

            case "${state}" in
                missing)
                    attempts=$((attempts + 1))
                    if [[ "${attempts}" -lt 12 ]]; then
                        sleep 5
                    fi
                    continue
                    ;;
                ambiguous)
                    echo "Found multiple ${workflow_name} push runs for ${release_sha}; refusing an ambiguous release gate" >&2
                    return 1
                    ;;
                *)
                    run_id="${state}"
                    break
                    ;;
            esac
        done

        if [[ -z "${run_id}" ]]; then
            echo "Required workflow ${workflow_name} was not discovered for ${release_sha} after 12 attempts" >&2
            return 1
        fi

        echo "Watching required workflow: ${workflow_name} (${run_id})"
        env -u GH_FORCE_TTY GH_PROMPT_DISABLED=1 GH_PAGER=cat NO_COLOR=1 gh run watch \
            "${run_id}" \
            --repo "${slug}" \
            --exit-status
        echo "Required workflow passed: ${workflow_name} (${run_id})"
    done
}

wait_for_pypi() {
    local version="$1"
    local attempts=0
    until curl -fsSL "https://pypi.org/pypi/${PYPI_PACKAGE}/json" | jq -e --arg version "${version}" '.releases[$version] | length > 0' >/dev/null; do
        attempts=$((attempts + 1))
        if [[ "${attempts}" -ge 30 ]]; then
            echo "Timed out waiting for ${PYPI_PACKAGE}==${version} on PyPI" >&2
            return 1
        fi
        sleep 10
    done
}

build_artifacts() {
    local version="$1"

    rm -rf "${REPO_DIR}/dist"
    uv build --out-dir "${REPO_DIR}/dist"
    if ! compgen -G "${REPO_DIR}/dist/${PYPI_PACKAGE}-${version}*" >/dev/null; then
        echo "Missing build artifacts for ${PYPI_PACKAGE}==${version}" >&2
        return 1
    fi
}

publish_artifacts() {
    local version="$1"
    local artifacts=()

    shopt -s nullglob
    artifacts+=("${REPO_DIR}/dist/${PYPI_PACKAGE}-${version}"*)
    shopt -u nullglob

    if [[ "${#artifacts[@]}" -eq 0 ]]; then
        echo "Missing build artifacts for ${PYPI_PACKAGE}==${version}" >&2
        return 1
    fi
    if curl -fsSL "https://pypi.org/pypi/${PYPI_PACKAGE}/json" | jq -e --arg version "${version}" '.releases[$version] | length > 0' >/dev/null 2>&1; then
        echo "${PYPI_PACKAGE} ${version} already published on PyPI"
    else
        uv publish --trusted-publishing always "${artifacts[@]}"
    fi
    wait_for_pypi "${version}"
}

create_release() {
    local slug="$1"
    local version="$2"
    local release_sha="$3"

    if gh release view "${TAG_PREFIX}${version}" --repo "${slug}" >/dev/null 2>&1; then
        echo "GitHub release ${TAG_PREFIX}${version} already exists"
        return
    fi
    gh release create "${TAG_PREFIX}${version}" \
        --repo "${slug}" \
        --target "${release_sha}" \
        --title "${TAG_PREFIX}${version}" \
        --generate-notes
}

main() {
    local slug release_sha release_branch version latest pypi_latest relation released_tag release_target pypi_exists github_release_exists

    source_optional_env
    slug="$(repo_slug)"
    release_sha="${RELEASE_SHA:-$(git rev-parse HEAD)}"
    release_branch="${RELEASE_BRANCH:-main}"
    require_clean_exact_checkout "${release_sha}" "${release_branch}"

    version="$(current_version)"
    latest="$(latest_release_version "${slug}")"
    pypi_latest="$(latest_pypi_version)"
    if [[ -n "${pypi_latest}" && ( -z "${latest}" || "$(compare_versions "${pypi_latest}" "${latest}")" == "gt" ) ]]; then
        latest="${pypi_latest}"
    fi
    relation="gt"
    if [[ -n "${latest}" ]]; then
        relation="$(compare_versions "${version}" "${latest}")"
    fi

    if [[ "${relation}" == "lt" ]]; then
        echo "Current version ${version} is behind latest published version ${latest}" >&2
        return 1
    fi

    pypi_exists=false
    github_release_exists=false
    if curl -fsSL "https://pypi.org/pypi/${PYPI_PACKAGE}/json" | jq -e --arg version "${version}" '.releases[$version] | length > 0' >/dev/null 2>&1; then
        pypi_exists=true
    fi
    release_target="$(git ls-remote origin "refs/tags/${TAG_PREFIX}${version}" | cut -f1)"
    if gh release view "${TAG_PREFIX}${version}" --repo "${slug}" >/dev/null 2>&1; then
        github_release_exists=true
    fi
    if [[ "${relation}" == "eq" && "${pypi_exists}" == true && "${github_release_exists}" == true && -n "${release_target}" ]]; then
        echo "${PYPI_PACKAGE} ${version} is already released; nothing to publish"
        return
    fi
    if [[ "${relation}" == "eq" && ( -z "${release_target}" || "${release_target}" != "${release_sha}" ) ]]; then
        echo "Refusing to recover partial release ${version}: no release tag anchors it to ${release_sha}" >&2
        return 1
    fi

    require_successful_workflows "${slug}" "${release_sha}"
    build_artifacts "${version}"
    create_release "${slug}" "${version}" "${release_sha}"
    publish_artifacts "${version}"

    released_tag="$(gh release view "${TAG_PREFIX}${version}" --repo "${slug}" --json tagName,targetCommitish --jq '[.tagName, .targetCommitish] | @tsv')"
    if [[ "${released_tag}" != $'v'"${version}"$'\t'"${release_sha}" ]]; then
        echo "GitHub release does not target the tested SHA ${release_sha}: ${released_tag}" >&2
        return 1
    fi
    echo "Released ${PYPI_PACKAGE} ${version} from ${release_sha}"
}

main "$@"

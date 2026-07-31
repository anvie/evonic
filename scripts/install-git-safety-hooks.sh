#!/usr/bin/env bash
# Configure this checkout to use the repository-managed Git safety hooks.

set -euo pipefail

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo 'ERROR: Run this script from inside a Git repository.' >&2
    exit 1
}

cd "$repo_root"

for hook in pre-rebase post-checkout; do
    if [[ ! -x ".githooks/$hook" ]]; then
        echo "ERROR: Missing executable hook: .githooks/$hook" >&2
        exit 1
    fi
done

git config core.hooksPath .githooks
printf '%s\n' 'Configured core.hooksPath=.githooks for this repository.'
printf '%s\n' 'Installed safety hooks: pre-rebase, post-checkout.'

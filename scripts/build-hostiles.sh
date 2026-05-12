#!/usr/bin/env bash
# scripts/build-hostile.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HOSTILE_DIR="hostile_tests"

for file in "$HOSTILE_DIR"/*.py; do
    name="$(basename "$file" .py)"
    echo "==> building hostile/$name"
    builder --language python \
        --user-id evil \
        --submission-id "$name" \
        --code-file "$file"
done

echo "done. built:"
docker images --format '{{.Repository}}:{{.Tag}}' | grep snake-submission-evil- || true

#!/usr/bin/env bash
# scripts/run-hostile.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OPPONENT="${OPPONENT:-snake-submission-jesper-001}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-artifacts}"

# get list of hostile images
mapfile -t hostile_images < <(
    docker images --format '{{.Repository}}' | grep '^snake-submission-evil-' | sort -u
)

if [[ ${#hostile_images[@]} -eq 0 ]]; then
    echo "no hostile images found. run build-hostile.sh first." >&2
    exit 1
fi

for img in "${hostile_images[@]}"; do
    name="${img#snake-submission-evil-}"
    echo
    echo "======================================================"
    echo "== running: $name"
    echo "======================================================"
    runner --sim-image snake-sim:v1 \
        --agent "$img:agent1" \
        --agent "$OPPONENT:agent2" \
        --artifacts-dir "$ARTIFACTS_DIR/$name" \
        --sim-args --snake-count 0 \
        || echo "(runner exited non-zero — expected for hostile agents)"
done

echo
echo "all hostile tests done. artifacts in $ARTIFACTS_DIR/"


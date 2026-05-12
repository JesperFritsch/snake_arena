#!/usr/bin/env bash
# scripts/build-submission.sh
set -euo pipefail

usage() {
    echo "Usage: $0 <language> <user_id> <submission_id> <agent_file>" >&2
    exit 1
}

[[ $# -eq 4 ]] || usage

LANG="$1"
USER_ID="$2"
SUBMISSION_ID="$3"
AGENT_FILE="$4"

VERSION="${VERSION:-v1}"
REGISTRY="${REGISTRY:-snake}"

BASE_IMAGE="${REGISTRY}-base-${LANG}:${VERSION}"
TAG="${REGISTRY}-submission-${USER_ID}-${SUBMISSION_ID}:latest"

# verify base image exists
docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 \
    || { echo "base image $BASE_IMAGE not found; run build-base-images.sh first" >&2; exit 1; }

# verify user file exists
[[ -f "$AGENT_FILE" ]] || { echo "agent file not found: $AGENT_FILE" >&2; exit 1; }

# build context in a temp dir
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

cp "$AGENT_FILE" "$BUILD_DIR/snake.py"

cat > "$BUILD_DIR/Dockerfile" <<EOF
FROM $BASE_IMAGE
COPY snake.py /app/harness/usercode/snake.py
EOF

echo "==> building $TAG"
docker build -t "$TAG" "$BUILD_DIR"

echo "done: $TAG"
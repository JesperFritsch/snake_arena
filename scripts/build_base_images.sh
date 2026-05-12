#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VERSION="${VERSION:-v1}"
REGISTRY="${REGISTRY:-snake}"  # local tag prefix; replace later for a real registry

LANGUAGES=()
for dir in sandbox-images/*/; do
    lang="$(basename "$dir")"
    [[ -f "sandbox-images/${lang}/Dockerfile" ]] && LANGUAGES+=("$lang")
done

for lang in "${LANGUAGES[@]}"; do
    dockerfile="sandbox-images/${lang}/Dockerfile"
    tag="${REGISTRY}-base-${lang}:${VERSION}"

    echo "==> building ${tag}"
    docker build -f "$dockerfile" -t "$tag" .
done

echo "done. built: $(docker images --format '{{.Repository}}:{{.Tag}}' | grep "${REGISTRY}-base-" || true)"
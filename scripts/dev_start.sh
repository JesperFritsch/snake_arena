#!/usr/bin/env bash
# scripts/dev_start.sh — start the local dev environment.
#
# In Docker:  postgres, redis, file-server, test-runner
#             (+ match-runner, scheduler with --ranked)
# On host:    API (uvicorn auto-reload) and the Vite dev server
#
# Env comes from .env.dev (committed) and .env.dev.local (gitignored,
# optional, wins). Ctrl-C stops the API and frontend; the Docker services
# keep running — the stop command is printed on exit.
#
# Usage:
#     ./scripts/dev_start.sh [--ranked]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SERVICES=(postgres redis file-server test-runner match-runner scheduler)

ENV_FILES=(.env.dev)
[[ -f .env.dev.local ]] && ENV_FILES+=(.env.dev.local)

COMPOSE=(docker compose)
for f in "${ENV_FILES[@]}"; do COMPOSE+=(--env-file "$f"); done
COMPOSE+=(-f docker-compose.yml -f docker-compose.dev.yml)

# Export for everything started from here (migrate.sh, API).
set -a
for f in "${ENV_FILES[@]}"; do
    # shellcheck disable=SC1090
    source "$f"
done
set +a

# ── Docker services ──────────────────────────────────────────────────────────
echo "==> starting docker services: ${SERVICES[*]}"
mkdir -p sim-artifacts
"${COMPOSE[@]}" up -d --build "${SERVICES[@]}"

echo "==> waiting for postgres"
for _ in $(seq 60); do
    pg_isready -q -h 127.0.0.1 -p 5432 && break
    sleep 1
done
pg_isready -q -h 127.0.0.1 -p 5432 || { echo "ERROR: postgres not ready after 60s" >&2; exit 1; }

echo "==> applying migrations"
ENV_FILE="${ENV_FILES[-1]}" ./scripts/migrate.sh

# ── Host processes ───────────────────────────────────────────────────────────
if [[ ! -d frontend/node_modules ]]; then
    echo "==> installing frontend dependencies"
    (cd frontend && npm install)
fi

# Job control gives each background job its own process group, so cleanup
# can take down uvicorn's reloader/worker and npm's vite child together.
set -m
pids=()

cleanup() {
    trap - INT TERM EXIT
    echo
    echo "==> stopping API and frontend"
    for pid in "${pids[@]}"; do
        kill -TERM -- "-$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "docker services are still running. stop them with:"
    echo "    ${COMPOSE[*]} stop"
}
trap cleanup INT TERM EXIT

echo "==> starting API on http://${API_HOST}:${API_PORT}"
uv run api &
pids+=($!)

echo "==> starting frontend on http://localhost:5173"
(cd frontend && exec npm run dev) &
pids+=($!)

# Exit (and clean up the other one) as soon as either process dies.
wait -n || true

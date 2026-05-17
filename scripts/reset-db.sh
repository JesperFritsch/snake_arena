#!/usr/bin/env bash
# scripts/reset-db.sh
#
# Nuke and recreate the database, then apply all migrations.
#
# Reads connection details from .env (DATABASE_URL preferred). Falls back to
# standard PG* env vars if DATABASE_URL is not set.
#
# Usage:
#     ./scripts/reset-db.sh
#
# Optional overrides:
#     ENV_FILE        (default: .env)
#     DATABASE_URL    full connection string, e.g.
#                     postgres://user:pass@host:5432/snake_arena
#     MIGRATIONS_DIR  (default: ./migrations)

set -euo pipefail

ENV_FILE="${ENV_FILE:-.env}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-./migrations}"

# Source .env if it exists. `set -a` exports everything assigned while it's on,
# so plain `KEY=value` lines in .env become environment variables.
if [[ -f "$ENV_FILE" ]]; then
    echo "==> loading $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "ERROR: DATABASE_URL is not set (looked in env and $ENV_FILE)" >&2
    exit 1
fi

# Parse DATABASE_URL with Python — handles URL-encoded passwords, missing
# ports, and other edge cases more reliably than a regex.
parse_url() {
    python3 - "$DATABASE_URL" <<'PY'
import sys
from urllib.parse import urlparse, unquote
u = urlparse(sys.argv[1])
print(f"PG_HOST={u.hostname or 'localhost'}")
print(f"PG_PORT={u.port or 5432}")
print(f"PG_USER={unquote(u.username or '')}")
print(f"PG_PASSWORD={unquote(u.password or '')}")
print(f"PG_DB={(u.path or '/').lstrip('/')}")
PY
}

eval "$(parse_url)"

if [[ -z "$PG_DB" ]]; then
    echo "ERROR: no database name in DATABASE_URL" >&2
    exit 1
fi

# Export PGPASSWORD so psql doesn't prompt. PGHOST/PGPORT/PGUSER are read by
# psql automatically if exported.
export PGHOST="$PG_HOST"
export PGPORT="$PG_PORT"
export PGUSER="$PG_USER"
export PGPASSWORD="$PG_PASSWORD"

PSQL_ADMIN=(psql -d postgres -v ON_ERROR_STOP=1)
PSQL_TARGET=(psql -d "$PG_DB" -v ON_ERROR_STOP=1)

echo "==> target: $PG_USER@$PG_HOST:$PG_PORT/$PG_DB"

echo "==> dropping database '$PG_DB' (if it exists)"
"${PSQL_ADMIN[@]}" <<SQL >/dev/null
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = '$PG_DB' AND pid <> pg_backend_pid();
SQL
"${PSQL_ADMIN[@]}" -c "DROP DATABASE IF EXISTS \"$PG_DB\";"

echo "==> creating database '$PG_DB'"
"${PSQL_ADMIN[@]}" -c "CREATE DATABASE \"$PG_DB\";"

echo "==> applying migrations from '$MIGRATIONS_DIR'"
if [[ ! -d "$MIGRATIONS_DIR" ]]; then
    echo "ERROR: migrations dir not found: $MIGRATIONS_DIR" >&2
    exit 1
fi

shopt -s nullglob
migrations=("$MIGRATIONS_DIR"/*.sql)
shopt -u nullglob

if [[ ${#migrations[@]} -eq 0 ]]; then
    echo "ERROR: no .sql files in $MIGRATIONS_DIR" >&2
    exit 1
fi

for f in "${migrations[@]}"; do
    echo "    applying $(basename "$f")"
    "${PSQL_TARGET[@]}" -f "$f"
done

echo "==> done; '$PG_DB' is fresh"
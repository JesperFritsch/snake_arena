#!/usr/bin/env bash
# scripts/migrate.sh
#
# Apply any unapplied migrations from MIGRATIONS_DIR to the target database.
# Safe to run repeatedly — already-applied files are skipped.
#
# Works for both an empty server (creates the database) and an existing
# database with some migrations already applied. Each migration runs inside
# its own transaction together with the tracking-table insert, so a failure
# leaves no partial state.
#
# Bootstrap case: if schema_migrations is empty but the schema already exists
# (i.e. the database was built before this script was introduced), migrations
# that fail with "already exists" errors are automatically stamped as applied
# rather than aborting. Genuine failures on later migrations still abort.
#
# Usage:
#     ./scripts/migrate.sh
#
# Optional overrides:
#     ENV_FILE        (default: .env)
#     DATABASE_URL    full connection string
#     MIGRATIONS_DIR  (default: ./migrations)

set -euo pipefail

ENV_FILE="${ENV_FILE:-.env}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-./migrations}"

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

export PGHOST="$PG_HOST"
export PGPORT="$PG_PORT"
export PGUSER="$PG_USER"
export PGPASSWORD="$PG_PASSWORD"

echo "==> target: $PG_USER@$PG_HOST:$PG_PORT/$PG_DB"

# ── Create the database if it doesn't exist ──────────────────────────────────
DB_EXISTS=$(psql -d postgres -v ON_ERROR_STOP=1 -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '$PG_DB';")

if [[ "$DB_EXISTS" != "1" ]]; then
    echo "==> database '$PG_DB' not found — creating"
    psql -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$PG_DB\";" >/dev/null
else
    echo "==> database '$PG_DB' already exists"
fi

PSQL=(psql -d "$PG_DB" -v ON_ERROR_STOP=1)

# ── Ensure the tracking table exists ─────────────────────────────────────────
"${PSQL[@]}" <<'SQL' >/dev/null
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT        PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

# Was the tracker empty before this run? If so we may be bootstrapping an
# existing database that predates the migration tracker. In that case we
# auto-stamp migrations whose objects already exist rather than aborting.
fresh_tracker=$("${PSQL[@]}" -tAc \
    "SELECT CASE WHEN COUNT(*) = 0 THEN '1' ELSE '0' END FROM schema_migrations;")

# ── Collect and sort migration files ─────────────────────────────────────────
if [[ ! -d "$MIGRATIONS_DIR" ]]; then
    echo "ERROR: migrations dir not found: $MIGRATIONS_DIR" >&2
    exit 1
fi

shopt -s nullglob
migrations=("$MIGRATIONS_DIR"/*.sql)
shopt -u nullglob

if [[ ${#migrations[@]} -eq 0 ]]; then
    echo "WARN: no .sql files found in $MIGRATIONS_DIR"
    exit 0
fi

# Sort by filename so 001, 002, 003 ... always run in order.
IFS=$'\n' sorted=($(sort <<<"${migrations[*]}")); unset IFS

applied=0
stamped=0
skipped=0

for f in "${sorted[@]}"; do
    name="$(basename "$f")"

    already=$("${PSQL[@]}" -tAc \
        "SELECT 1 FROM schema_migrations WHERE filename = '$name';")

    if [[ "$already" == "1" ]]; then
        echo "    skip  $name (already applied)"
        (( skipped++ )) || true
        continue
    fi

    echo -n "    apply $name ... "

    err_file=$(mktemp)
    # Run the migration and record it atomically inside one transaction.
    # psql exits non-zero on the first SQL error (ON_ERROR_STOP=1), which
    # rolls the transaction back automatically when the connection closes.
    if "${PSQL[@]}" 2>"$err_file" <<SQL; then
BEGIN;
\i $f
INSERT INTO schema_migrations (filename) VALUES ('$name');
COMMIT;
SQL
        echo "ok"
        (( applied++ )) || true
    elif [[ "$fresh_tracker" == "1" ]] && \
         grep -qiE "already exists|duplicate_object|duplicate_table|duplicate_column|42P07|42710|42701" "$err_file"; then
        # Bootstrap: schema was built before the tracker existed. Stamp the
        # migration as applied so future runs skip it cleanly.
        echo "stamped (objects already exist — pre-tracker schema)"
        "${PSQL[@]}" -c \
            "INSERT INTO schema_migrations (filename) VALUES ('$name') ON CONFLICT DO NOTHING;" \
            >/dev/null
        (( stamped++ )) || true
    else
        echo "FAILED"
        echo "--- error output ---" >&2
        cat "$err_file" >&2
        rm -f "$err_file"
        exit 1
    fi
    rm -f "$err_file"
done

echo "==> done: $applied applied, $stamped stamped, $skipped skipped"

#!/usr/bin/env bash
# The memory on a Mac, no Docker: Homebrew Postgres 17 + pgvector holds both the
# structured data (duke_data) and Hindsight's long-term memory (hindsight), and
# Hindsight itself runs from pip. No sudo. Called by setup.sh; safe to re-run.
# Works with macOS's own bash 3.2.
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
PG_DIR="$REPO_ROOT/blimp-infra/postgres"
HOME_DIR="${BLIMP_HOME:-$HOME/blimp}"
DB_NAME="${DUKE_DB_NAME:-duke_data}"; HS_DB="${HINDSIGHT_DB_NAME:-hindsight}"
VENV="$HOME_DIR/duke-venv"; ENV_FILE="$HOME_DIR/.env"
BIN="$HOME/.local/bin"; mkdir -p "$BIN"
die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "  [memory] $*"; }
[ "$(uname -s)" = Darwin ] || die "this script is for macOS."
command -v brew >/dev/null || die "Homebrew is missing."

log "Postgres 17 + pgvector"
for f in postgresql@17 pgvector python@3.13; do brew list --versions "$f" >/dev/null 2>&1 || brew install -q "$f"; done
PGBIN="$(brew --prefix postgresql@17)/bin"; export PATH="$PGBIN:$PATH"
pg_isready -q -h 127.0.0.1 2>/dev/null || brew services start postgresql@17 >/dev/null
i=0; while [ $i -lt 30 ]; do pg_isready -q -h 127.0.0.1 && break; sleep 1; i=$((i + 1)); done
pg_isready -q -h 127.0.0.1 || die "Postgres did not start. Try: brew services restart postgresql@17"
PG_PORT="$(psql -h 127.0.0.1 -d postgres -tAc 'SHOW port;' | tr -d ' ')"
PG_SOCK="$(psql -h 127.0.0.1 -d postgres -tAc 'SHOW unix_socket_directories;' | cut -d, -f1 | tr -d ' ')"

log "databases $DB_NAME and $HS_DB"
for db in "$DB_NAME" "$HS_DB"; do
  psql -h 127.0.0.1 -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 || createdb -h 127.0.0.1 "$db"
  psql -h 127.0.0.1 -d "$db" -qc "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null
done
psql -h 127.0.0.1 -d "$DB_NAME" -q -f "$PG_DIR/schema.sql" >/dev/null
DB_URL="postgresql:///$DB_NAME?host=$PG_SOCK&port=$PG_PORT"
sed -i.bak '/^DUKE_DB_URL=/d' "$ENV_FILE" && rm -f "$ENV_FILE.bak"; printf "DUKE_DB_URL='%s'\n" "$DB_URL" >> "$ENV_FILE"

log "Python environment"
PY="$(brew --prefix python@3.13)/bin/python3.13"
[ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || { rm -rf "$VENV"; "$PY" -m venv "$VENV"; }
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$PG_DIR/requirements.txt"

log "Hindsight"
HINDSIGHT_DB_URL="postgresql://$(id -un)@127.0.0.1:$PG_PORT/$HS_DB" bash "$PG_DIR/bootstrap-hindsight-pip.sh"

log "commands: duke-data, duke-mem"
cat > "$BIN/duke-data" <<W
#!/usr/bin/env bash
export DUKE_DB_URL='$DB_URL'
exec "$VENV/bin/python" "$PG_DIR/duke_memory.py" "\$@"
W
cat > "$BIN/duke-mem" <<W
#!/usr/bin/env bash
export HINDSIGHT_URL="http://127.0.0.1:${HINDSIGHT_API_PORT:-8888}" HINDSIGHT_USER="duke"
exec "$VENV/bin/python" "$PG_DIR/hindsight_client.py" "\$@"
W
chmod +x "$BIN/duke-data" "$BIN/duke-mem"
"$BIN/duke-data" state-set duke_bootstrap_ts "\"$(date +%Y-%m-%dT%H:%M:%S)\"" >/dev/null || die "the database smoke test failed."
log "memory is online"

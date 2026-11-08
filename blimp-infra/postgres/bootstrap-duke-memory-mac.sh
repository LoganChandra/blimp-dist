#!/usr/bin/env bash
# Memory stack for macOS (Homebrew Postgres 17 + pgvector, Hindsight in Docker).
# Same result as bootstrap-duke-memory.sh (Ubuntu), no sudo. UNTESTED on a real Mac:
# if a step fails, run the doctor: python3 blimp-infra/doctor/doctor.py
set -euo pipefail
log() { printf '\n\033[1;36m[brain]\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mStopped: %s\033[0m\n' "$*"; exit 1; }

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PG_DIR="$REPO_ROOT/blimp-infra/postgres"
DB_NAME="${DUKE_DB_NAME:-duke_data}"
API_PORT="${HINDSIGHT_API_PORT:-8888}"; UI_PORT="${HINDSIGHT_UI_PORT:-9999}"
DATA="${HINDSIGHT_DATA:-$HOME/.hindsight-docker}"
IMAGE="${HINDSIGHT_IMAGE:-ghcr.io/vectorize-io/hindsight@sha256:19d0c08f3df79acac29384826419a1ca158129e81ec527cdcbd8c97ff4ef76da}"
VENV="$HOME/blimp/duke-venv"; ENV_FILE="$HOME/blimp/.env"
BIN="$HOME/.local/bin"; mkdir -p "$BIN" "$DATA"
[[ "$(uname -s)" == Darwin ]] || die "this script is for macOS."
set -a; source "$ENV_FILE" 2>/dev/null || true; set +a
HS_MODE="${HINDSIGHT_MODE:-docker}"   # pip = Hindsight from pip with a Codex login: no Docker, no OpenRouter key
KEY="${OPENROUTER_API_KEY:-}"; [[ "$HS_MODE" == pip || -n "$KEY" ]] || die "OPENROUTER_API_KEY is missing from $ENV_FILE."
LLM_URL="${HINDSIGHT_LLM_BASE_URL:-https://openrouter.ai/api/v1}"
LLM_MODEL="${HINDSIGHT_LLM_MODEL:-deepseek/deepseek-v4-flash}"

log "[1/5] Postgres + pgvector (Homebrew)"
command -v brew >/dev/null || die "Homebrew is missing: https://brew.sh"
brew list postgresql@17 >/dev/null 2>&1 || brew install postgresql@17
brew list pgvector >/dev/null 2>&1 || brew install pgvector
brew services start postgresql@17 >/dev/null
PGBIN="$(brew --prefix postgresql@17)/bin"; export PATH="$PGBIN:$PATH"
for _ in $(seq 1 20); do pg_isready -q && break; sleep 1; done
pg_isready -q || die "Postgres did not start. Try: brew services restart postgresql@17"
ok "postgres answering"

log "[2/5] Database $DB_NAME"
psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 || createdb "$DB_NAME"
psql -d "$DB_NAME" -qc "CREATE EXTENSION IF NOT EXISTS vector;" 
psql -d "$DB_NAME" -q -f "$PG_DIR/schema.sql" >/dev/null
PG_PORT="$(psql -d postgres -tAc 'SHOW port;' | tr -d ' ')"
PG_SOCK="$(psql -d postgres -tAc 'SHOW unix_socket_directories;' | cut -d, -f1 | tr -d ' ')"
DB_URL="postgresql:///$DB_NAME?host=$PG_SOCK&port=$PG_PORT"
sed -i.bak '/^DUKE_DB_URL=/d' "$ENV_FILE" && rm -f "$ENV_FILE.bak"; printf "DUKE_DB_URL='%s'\n" "$DB_URL" >> "$ENV_FILE"
ok "database ready ($DB_URL)"

log "[3/5] Hindsight ($HS_MODE)"
if [[ "$HS_MODE" == pip ]]; then
  bash "$PG_DIR/bootstrap-hindsight-pip.sh" || die "Hindsight (pip) failed."
else
command -v docker >/dev/null || die "Docker is missing. Install Docker Desktop (docker.com/products/docker-desktop), open it once, then run setup again."
docker info >/dev/null 2>&1 || die "Docker is installed but not running. Open Docker Desktop, wait until it says running, then run setup again."
if ! docker ps -a --format '{{.Names}}' | grep -q '^duke-hindsight$'; then
  docker pull "$IMAGE"
  KEYFILE="$(mktemp)"; chmod 600 "$KEYFILE"; printf 'HINDSIGHT_API_LLM_API_KEY=%s\n' "$KEY" > "$KEYFILE"   # not on the command line
  trap 'rm -f "$KEYFILE"' EXIT
  docker run -d --name duke-hindsight --restart unless-stopped \
    -p "127.0.0.1:${API_PORT}:8888" -p "127.0.0.1:${UI_PORT}:9999" \
    -e HINDSIGHT_API_LLM_PROVIDER=openai --env-file "$KEYFILE" \
    -e HINDSIGHT_API_LLM_BASE_URL="$LLM_URL" -e HINDSIGHT_API_LLM_MODEL="$LLM_MODEL" \
    --env-file "$PG_DIR/brain-1.2.env" --env-file "$PG_DIR/brain-model.env" \
    --shm-size 1g -v "$DATA:/home/hindsight/.pg0" "$IMAGE"
else
  docker ps --format '{{.Names}}' | grep -q '^duke-hindsight$' || docker start duke-hindsight >/dev/null
fi
for i in $(seq 1 45); do curl -sf "http://localhost:${API_PORT}/health" >/dev/null 2>&1 && break; sleep 2; done
curl -sf "http://localhost:${API_PORT}/health" >/dev/null 2>&1 || die "Hindsight did not answer in 90s. Check: docker logs duke-hindsight"
ok "Hindsight up"
fi

log "[4/5] Python environment"
[[ -d "$VENV" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$PG_DIR/requirements.txt"
[[ -f "$PG_DIR/../a2a/requirements.txt" ]] && "$VENV/bin/pip" install -q -r "$PG_DIR/../a2a/requirements.txt" || true
ok "venv ready"

log "[5/5] Commands: duke-data, duke-mem (in $BIN)"
cat > "$BIN/duke-data" <<W
#!/usr/bin/env bash
export DUKE_DB_URL="$DB_URL"
exec "$VENV/bin/python" "$PG_DIR/duke_memory.py" "\$@"
W
cat > "$BIN/duke-mem" <<W
#!/usr/bin/env bash
export HINDSIGHT_URL="http://localhost:${API_PORT}" HINDSIGHT_USER="duke"
exec "$VENV/bin/python" "$PG_DIR/hindsight_client.py" "\$@"
W
chmod +x "$BIN/duke-data" "$BIN/duke-mem"
export PATH="$BIN:$PATH"
duke-mem status >/dev/null 2>&1 && duke-mem ingest-repo --root "$REPO_ROOT" || true
duke-data state-set duke_bootstrap_ts "\"$(date +%Y-%m-%dT%H:%M:%S)\"" >/dev/null && ok "smoke test passed"
echo "Brain is online."

#!/usr/bin/env bash
# bootstrap-duke-memory.sh
# Sets up Duke's full memory stack:
#   - Postgres + pgvector (for structured business data: tasks, prospects, pricing_log, project_state)
#   - Hindsight (Docker container, owns long-term semantic memory, conversations, doc retrieval)
#   - Python venv + duke_memory.py (business data CLI: `duke-data`)
#   - hindsight_client.py wrapper (memory CLI: `duke-mem`)
#   - Initial ingest of repo docs into Hindsight
#
# Idempotent. Safe to re-run.

set -euo pipefail
log()  { printf '\n\033[1;36m[duke]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  warn\033[0m %s\n' "$*"; }

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"  # repo root, wherever it is checked out
PG_DIR="$REPO_ROOT/blimp-infra/postgres"
DB_NAME="${DUKE_DB_NAME:-duke_data}"
HINDSIGHT_API_PORT="${HINDSIGHT_API_PORT:-8888}"
HINDSIGHT_UI_PORT="${HINDSIGHT_UI_PORT:-9999}"
# Publish ports on loopback only (P0.2). Override with HINDSIGHT_BIND only for
# a Tailscale-interface bind — never 0.0.0.0.
HINDSIGHT_BIND="${HINDSIGHT_BIND:-127.0.0.1}"
HINDSIGHT_DATA="${HINDSIGHT_DATA:-$HOME/.hindsight-docker}"
# Pinned to the exact image the brain runs (API 1.2.0 since 2026-11-06). Upgrading is deliberate: run the
# memory exam (blimp-infra/brain-eval/exam) on a clone first, then upgrade-brain.sh. The reranker and
# embedder are set explicitly below so a new image's defaults can't silently swap them (or pick the
# paid hosted provider, "hindsight"). Old pin (API 0.9.2): sha256:84ab276b8f501546deb6ea9c64a57291718b4e16a59dd9e02a02fdd5adfe9028
HINDSIGHT_IMAGE="${HINDSIGHT_IMAGE:-ghcr.io/vectorize-io/hindsight@sha256:19d0c08f3df79acac29384826419a1ca158129e81ec527cdcbd8c97ff4ef76da}"
VENV="$HOME/blimp/duke-venv"
USER_NAME="$(whoami)"

# LLM provider for Hindsight's internal memory ops (fact extraction etc).
# We use OpenRouter via OpenAI-compatible interface.
# Required: $OPENROUTER_API_KEY in env (set in ~/blimp/.env or shell).
#
# Model choices (locked in):
#   - Hindsight (memory ops):  openai/gpt-oss-20b   — cheap, fast, 20B open-source
#   - Duke (when on Hermes):   qwen/qwen3-max       — qwen's flagship for agent reasoning
# Both go through OpenRouter, so prefixes follow OpenRouter's slug format.
# Override at runtime with env vars if you want to A/B another model.

# Try sourcing the key (and any saved model overrides) from ~/blimp/.env first
mkdir -p "$HOME/blimp"
[ -f "$HOME/blimp/.env" ] && source "$HOME/blimp/.env" 2>/dev/null || true

HINDSIGHT_LLM_PROVIDER="${HINDSIGHT_LLM_PROVIDER:-openai}"
HINDSIGHT_LLM_BASE_URL="${HINDSIGHT_LLM_BASE_URL:-https://openrouter.ai/api/v1}"
HINDSIGHT_LLM_MODEL="${HINDSIGHT_LLM_MODEL:-deepseek/deepseek-v4-flash}"

# Duke's model when running on Hermes Agent (or any non-Claude-Code runtime).
# Not consumed by this script directly — exported into ~/blimp/.env so the
# Hermes launcher can pick it up later. the Boss can edit ~/blimp/.env any time.
HERMES_DUKE_MODEL="${HERMES_DUKE_MODEL:-qwen/qwen3-max}"

# HINDSIGHT_MODE=pip (set by the public setup for Codex users): Hindsight runs from pip with a Codex login, no Docker, no OpenRouter key.
HS_MODE="${HINDSIGHT_MODE:-docker}"
HINDSIGHT_API_KEY="${OPENROUTER_API_KEY:-${HINDSIGHT_API_LLM_API_KEY:-}}"
if [ "$HS_MODE" != pip ] && [ -z "$HINDSIGHT_API_KEY" ]; then
  echo "ERROR: OPENROUTER_API_KEY is not set."
  echo "Set it in your shell:  export OPENROUTER_API_KEY=sk-or-v1-..."
  echo "Or add to ~/blimp/.env:  OPENROUTER_API_KEY=sk-or-v1-..."
  echo "Then re-run this script."
  exit 1
fi

# Persist model choices to ~/blimp/.env so future runs (and the Hermes launcher)
# don't need them re-exported.  Idempotent: replaces existing lines if present.
ENV_FILE="$HOME/blimp/.env"
touch "$ENV_FILE"
_set_env_kv() {
  local k="$1" v="$2"
  if grep -q "^${k}=" "$ENV_FILE"; then
    # macOS/BSD sed differs from GNU; use a tempfile to stay portable
    sed -i.bak "s|^${k}=.*|${k}=${v}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
  else
    echo "${k}=${v}" >> "$ENV_FILE"
  fi
}
_set_env_kv HINDSIGHT_LLM_PROVIDER  "$HINDSIGHT_LLM_PROVIDER"
_set_env_kv HINDSIGHT_LLM_BASE_URL  "$HINDSIGHT_LLM_BASE_URL"
_set_env_kv HINDSIGHT_LLM_MODEL     "$HINDSIGHT_LLM_MODEL"
_set_env_kv HERMES_DUKE_MODEL       "$HERMES_DUKE_MODEL"
ok "model env saved to $ENV_FILE  (Hindsight: $HINDSIGHT_LLM_MODEL | Duke→Hermes: $HERMES_DUKE_MODEL)"

# ------------------------------------------------------------------
# 1) apt deps
# ------------------------------------------------------------------
log "[1/7] apt deps (postgres + pgvector + python + docker)"
# Ubuntu's postgres major version moves with the release (24.04 -> 16,
# 26.04 -> 18), so hardcoding 16 fails with "Unable to locate package
# postgresql-16" on newer distros. Ask apt what it actually offers.
PG_VER="${DUKE_PG_VER:-$(apt-cache search --names-only '^postgresql-[0-9]+$' 2>/dev/null \
          | grep -oE 'postgresql-[0-9]+' | grep -oE '[0-9]+$' | sort -n | tail -1)}"
PG_VER="${PG_VER:-16}"
if ! dpkg -l "postgresql-$PG_VER" >/dev/null 2>&1 || { [ "$HS_MODE" != pip ] && ! command -v docker >/dev/null; }; then
  sudo apt-get update -y
  sudo apt-get install -y \
    "postgresql-$PG_VER" "postgresql-contrib-$PG_VER" \
    python3 python3-venv python3-pip build-essential libpq-dev \
    $([ "$HS_MODE" = pip ] || echo docker.io)
  # pgvector is packaged per server version and the name varies by release.
  sudo apt-get install -y "postgresql-$PG_VER-pgvector" 2>/dev/null \
    || sudo apt-get install -y postgresql-pgvector 2>/dev/null \
    || warn "no pgvector package for pg$PG_VER - CREATE EXTENSION vector may fail"
  sudo usermod -aG docker "$USER_NAME" || true
fi
ok "system packages present (postgres $PG_VER)"

# Ubuntu 26.04 has no sysvinit wrapper for postgresql, so `service postgresql
# start` reports "Unit postgresql.service not found" when systemd is absent.
# Fall back to pg_ctlcluster, which drives the cluster directly.
pg_up() { sudo -u postgres psql -tAc 'SELECT 1' >/dev/null 2>&1; }

if ! pg_up; then
  # A postgres package installed while systemd was absent often ends up with NO
  # cluster at all (the postinst could not create/start one). Create it, then start.
  if ! sudo pg_lsclusters 2>/dev/null | awk 'NR>1' | grep -q .; then
    log "  no postgres cluster exists - creating $PG_VER/main"
    sudo pg_createcluster "$PG_VER" main --start >/dev/null 2>&1 || true
  fi

  sudo systemctl start postgresql >/dev/null 2>&1 || true
  sudo service postgresql start   >/dev/null 2>&1 || true
  sudo pg_ctlcluster "$PG_VER" main start >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do pg_up && break; sleep 1; done

  if ! pg_up; then
    # try every cluster the box knows about, whatever its version
    sudo pg_lsclusters 2>/dev/null | awk 'NR>1{print $1" "$2}' | while read -r v c; do
      sudo pg_ctlcluster "$v" "$c" start >/dev/null 2>&1 || true
    done
    for _ in 1 2 3 4 5; do pg_up && break; sleep 1; done
  fi

  if ! pg_up; then
    warn "postgres will not start - diagnostics follow"
    echo "    --- pg_lsclusters ---"
    sudo pg_lsclusters 2>&1 | sed 's/^/    /'
    echo "    --- systemctl status ---"
    sudo systemctl status "postgresql@$PG_VER-main" --no-pager 2>&1 | head -12 | sed 's/^/    /'
    echo "    --- cluster log ---"
    sudo tail -20 "/var/log/postgresql/postgresql-$PG_VER-main.log" 2>&1 | sed 's/^/    /'
    echo "    --- pg_createcluster retry ---"
    sudo pg_createcluster "$PG_VER" main --start 2>&1 | head -10 | sed 's/^/    /'
    pg_up || { echo; echo "FATAL: no working postgres. Send the block above."; exit 1; }
  fi
fi
ok "postgres answering queries"

if [ "$HS_MODE" != pip ] && ! sudo docker info >/dev/null 2>&1; then
  sudo service docker start >/dev/null 2>&1 \
    || sudo systemctl start docker >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8; do sudo docker info >/dev/null 2>&1 && break; sleep 2; done
fi
ok "postgres + docker running"

# ------------------------------------------------------------------
# 2) Postgres database + role for business data
# ------------------------------------------------------------------
log "[2/7] Postgres database $DB_NAME"
if ! sudo -u postgres psql -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
  sudo -u postgres createdb "$DB_NAME"
  ok "created database $DB_NAME"
else
  ok "database $DB_NAME already exists"
fi

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$USER_NAME'" | grep -q 1; then
  sudo -u postgres psql -c "CREATE ROLE \"$USER_NAME\" LOGIN SUPERUSER;"
  ok "created postgres role $USER_NAME"
fi

sudo -u postgres psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null
sudo -u postgres psql -d "$DB_NAME" -f "$PG_DIR/schema.sql" >/dev/null
ok "schema applied (tasks, prospects, pricing_log, project_state)"

# Patch pg_hba.conf to use trust auth for local connections (single-user dev box).
# Hindsight's embedded postgres uses scram-sha-256; if we accidentally connect to
# it, we'd get "fe_sendauth: no password supplied". Trust + Unix socket avoids this.
PG_HBA=$(sudo -u postgres psql -tAc "SHOW hba_file;")
if ! sudo grep -qE '^host\s+all\s+all\s+127\.0\.0\.1/32\s+trust' "$PG_HBA"; then
  sudo sed -i.bak -E '
    s/^(host\s+all\s+all\s+127\.0\.0\.1\/32\s+)\S+/\1trust/;
    s/^(host\s+all\s+all\s+::1\/128\s+)\S+/\1trust/;
    s/^(local\s+all\s+all\s+)\S+/\1trust/
  ' "$PG_HBA"
  sudo service postgresql reload
  ok "pg_hba.conf patched: local + 127.0.0.1 set to trust auth"
else
  ok "pg_hba.conf already trust-auth for localhost"
fi

# ------------------------------------------------------------------
# 3) Hindsight (Docker) — image: ghcr.io/vectorize-io/hindsight:latest
#    Exposes API on 8888 and UI on 9999.
#    Embedded Postgres lives in /home/hindsight/.pg0 (mounted to $HINDSIGHT_DATA)
# ------------------------------------------------------------------
log "[3/7] Hindsight container"
mkdir -p "$HINDSIGHT_DATA"
if [ "$HS_MODE" = pip ]; then
  bash "$PG_DIR/bootstrap-hindsight-pip.sh" || exit 1
elif ! sudo docker ps -a --format '{{.Names}}' | grep -q '^duke-hindsight$'; then
  sudo docker pull "$HINDSIGHT_IMAGE"
  sudo docker run -d \
    --name duke-hindsight \
    --restart unless-stopped \
    -p "${HINDSIGHT_BIND}:${HINDSIGHT_API_PORT}:8888" \
    -p "${HINDSIGHT_BIND}:${HINDSIGHT_UI_PORT}:9999" \
    -e HINDSIGHT_API_LLM_PROVIDER="$HINDSIGHT_LLM_PROVIDER" \
    -e HINDSIGHT_API_LLM_API_KEY="$HINDSIGHT_API_KEY" \
    -e HINDSIGHT_API_LLM_BASE_URL="$HINDSIGHT_LLM_BASE_URL" \
    -e HINDSIGHT_API_LLM_MODEL="$HINDSIGHT_LLM_MODEL" \
    --env-file "$PG_DIR/brain-1.2.env" \
    --env-file "$PG_DIR/brain-model.env" \
    --shm-size 1g \
    -v "$HINDSIGHT_DATA:/home/hindsight/.pg0" \
    "$HINDSIGHT_IMAGE"
  ok "Hindsight container started (image: $HINDSIGHT_IMAGE)"
else
  if ! sudo docker ps --format '{{.Names}}' | grep -q '^duke-hindsight$'; then
    sudo docker start duke-hindsight
  fi
  ok "Hindsight container already running"
fi

# Wait for Hindsight to be healthy
log "    waiting for Hindsight API on port ${HINDSIGHT_API_PORT} (up to 90s)..."
for i in {1..45}; do
  # Try a few candidate health endpoints. Whichever responds 200 wins.
  for ep in "/health" "/api/health" "/api/v1/health" "/"; do
    if curl -sf "http://localhost:${HINDSIGHT_API_PORT}${ep}" >/dev/null 2>&1; then
      ok "Hindsight is up (responded at ${ep})"
      break 2
    fi
  done
  sleep 2
  [ "$i" -eq 45 ] && warn "Hindsight didn't respond — check 'sudo docker logs duke-hindsight'"
done

# ------------------------------------------------------------------
# 4) Python venv
# ------------------------------------------------------------------
log "[4/7] Python venv at $VENV"
if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$PG_DIR/requirements.txt"
# Also install A2A broker dependencies into the same venv (shares Postgres)
[ -f "$PG_DIR/../a2a/requirements.txt" ] && pip install --quiet -r "$PG_DIR/../a2a/requirements.txt"
# Orchestrator (LangGraph supervisor) deps
[ -f "$PG_DIR/../orchestrator/requirements.txt" ] && pip install --quiet -r "$PG_DIR/../orchestrator/requirements.txt"
ok "venv ready ($(python --version 2>&1))"

# ------------------------------------------------------------------
# 5) System-wide wrappers
# ------------------------------------------------------------------
log "[5/7] Installing /usr/local/bin/duke-data and duke-mem"

# Auto-detect WSL postgres port (Hindsight's embedded postgres often squats 5432,
# pushing Debian postgres to 5433). Use the Unix socket so we don't fight over TCP.
PG_PORT=$(sudo -u postgres psql -tAc "SHOW port;" 2>/dev/null | tr -d ' ')
PG_PORT="${PG_PORT:-5432}"
ok "WSL postgres detected on port $PG_PORT (using Unix socket)"

sudo tee /usr/local/bin/duke-data > /dev/null <<EOF
#!/usr/bin/env bash
# Unix socket connection — avoids port-5432 collision with Hindsight's embedded pg
export DUKE_DB_URL="postgresql:///$DB_NAME?host=/var/run/postgresql&port=$PG_PORT"
exec "$VENV/bin/python" "$PG_DIR/duke_memory.py" "\$@"
EOF
sudo chmod +x /usr/local/bin/duke-data

sudo tee /usr/local/bin/duke-mem > /dev/null <<EOF
#!/usr/bin/env bash
export HINDSIGHT_URL="http://localhost:${HINDSIGHT_API_PORT}"
export HINDSIGHT_USER="duke"
exec "$VENV/bin/python" "$PG_DIR/hindsight_client.py" "\$@"
EOF
sudo chmod +x /usr/local/bin/duke-mem

ok "commands available: duke-data, duke-mem"

# ------------------------------------------------------------------
# 6) Ingest repo into Hindsight
# ------------------------------------------------------------------
log "[6/7] Ingesting repo docs into Hindsight"
duke-mem status || warn "Hindsight not reachable — skipping ingest"
duke-mem status >/dev/null 2>&1 && duke-mem ingest-repo --root "$REPO_ROOT" || true

# ------------------------------------------------------------------
# 7) Smoke test
# ------------------------------------------------------------------
log "[7/7] Smoke test"
echo "--- duke-mem status ---"
duke-mem status || true
echo "--- duke-mem recall 'pricing framework' ---"
duke-mem recall "pricing framework" --limit 3 2>&1 || warn "recall failed"
echo "--- duke-data state-set test ---"
duke-data state-set duke_bootstrap_ts "\"$(date -Iseconds)\""
duke-data state-get duke_bootstrap_ts
echo "--- duke-data tasks ---"
duke-data tasks || true

cat <<'NEXT'

============================================================
 Duke's brain is online.
============================================================

 Long-term memory (semantic, conversations, doc retrieval):
   duke-mem recall "<query>"
   duke-mem remember --content "..." --tags fact,decision
   duke-mem log --user logan --channel other-agent --speaker duke --message "..."
   duke-mem recent --channel other-agent
   duke-mem ingest-repo                # re-ingest after doc updates
   duke-mem status

 Structured business data (tasks, prospects, pricing, state):
   duke-data task-add --title "Send pricing to X"
   duke-data tasks --status in_progress
   duke-data prospect-add --company "X Med Spa" --industry aesthetic --country SG
   duke-data prospects --stage discovery
   duke-data quote-log --prospect 3 --amount 8000 --currency SGD
   duke-data state-set current_focus '{"client":"X"}'
   duke-data activity

 Hindsight API:  http://localhost:${HINDSIGHT_API_PORT}
 Hindsight UI:   http://localhost:${HINDSIGHT_UI_PORT}
   (data persisted at $HINDSIGHT_DATA)

 Database: postgresql://$USER_NAME@localhost:5432/$DB_NAME
 Backup:   pg_dump $DB_NAME > backup.sql ; sudo docker exec duke-hindsight ... (see Hindsight docs)
============================================================
NEXT

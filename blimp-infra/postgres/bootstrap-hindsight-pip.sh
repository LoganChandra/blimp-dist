#!/usr/bin/env bash
# Hindsight (the long-term memory) without Docker: pip install into its own venv,
# run in tmux session 'hindsight' on 127.0.0.1 only.
#
# Settings (env):
#   HINDSIGHT_LLM_MODE   openrouter (default when OPENROUTER_API_KEY is set) | codex
#   HINDSIGHT_DB_URL     an external Postgres with pgvector (the Mac path uses Homebrew's);
#                        unset = Hindsight's embedded Postgres (pg0)
#   HINDSIGHT_CI=1       CI only: skip the LLM check at start so a dummy key works
# The memory model's key is read from ~/blimp/.env each time it starts, never
# written into the runner. Hindsight docs call the Codex route "personal
# development use only" (ChatGPT terms).
set -euo pipefail
HOME_DIR="${BLIMP_HOME:-$HOME/blimp}"
VENV="$HOME_DIR/hindsight-venv"
CODEX_DIR="$HOME_DIR/hindsight-codex"      # its OWN login: Codex refresh tokens are single-use, sharing breaks them
PORT="${HINDSIGHT_API_PORT:-8888}"
VERSION="${HINDSIGHT_VERSION:-1.2.0}"
PG_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HOME_DIR/.env"
die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "  [hindsight] $*"; }

set -a; . "$ENV_FILE" 2>/dev/null || true; set +a
MODE="${HINDSIGHT_LLM_MODE:-}"
[ -n "$MODE" ] || { [ -n "${OPENROUTER_API_KEY:-}" ] && MODE=openrouter || MODE=codex; }
command -v tmux >/dev/null || die "tmux is missing."
[ "$MODE" = codex ] && { command -v codex >/dev/null || die "Codex is not installed (npm install -g @openai/codex)."; }
[ "$MODE" = openrouter ] && [ -z "${OPENROUTER_API_KEY:-}" ] && die "OPENROUTER_API_KEY is missing from $ENV_FILE."

# Hindsight's local models want Python 3.11-3.13 (its docs: 3.14 supports hosted providers only).
PY=""
for c in python3.13 python3.12 python3.11 /opt/homebrew/bin/python3.13 /usr/local/bin/python3.13; do
  command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
done
[ -n "$PY" ] || die "Need Python 3.11-3.13. Mac: brew install python@3.13. Ubuntu: sudo apt install python3.12 python3.12-venv (22.04: python3.11 python3.11-venv)."

mkdir -p "$HOME_DIR"
if [ ! -x "$VENV/bin/hindsight-api" ]; then
  log "installing hindsight-api $VERSION (about 3 GB with its local models; takes a while)"
  [ -x "$VENV/bin/pip" ] || { rm -rf "$VENV"; "$PY" -m venv "$VENV" || { rm -rf "$VENV"; die "could not create a Python venv. Ubuntu: sudo apt install ${PY}-venv"; }; }
  "$VENV/bin/pip" install -q --upgrade pip
  # Linux: the default torch wheel drags in ~2 GB of CUDA libraries; the CPU build is all this needs.
  [ "$(uname)" = Linux ] && "$VENV/bin/pip" install -q torch --index-url https://download.pytorch.org/whl/cpu
  "$VENV/bin/pip" install -q "hindsight-api==$VERSION"
fi

if [ "$MODE" = codex ] && [ ! -s "$CODEX_DIR/auth.json" ]; then
  mkdir -p "$CODEX_DIR"
  log "one-time ChatGPT login for the memory (a separate login from the agent's; a browser opens)"
  if CODEX_HOME="$CODEX_DIR" codex login --help >/dev/null 2>&1; then CODEX_HOME="$CODEX_DIR" codex login
  else CODEX_HOME="$CODEX_DIR" codex auth login; fi
  [ -s "$CODEX_DIR/auth.json" ] || die "login did not save $CODEX_DIR/auth.json. Run: CODEX_HOME=$CODEX_DIR codex login"
fi

# The runner: same embedding/reranker settings as our own brain (brain-1.2.env).
RUNNER="$HOME_DIR/run-hindsight.sh"
envlines() {  # KEY=VALUE lines -> shell-safe assignments (values like JSON keep their quotes)
  grep -E '^[A-Z_]+=' "$1" 2>/dev/null | while IFS= read -r l; do printf '%s=%q\n' "${l%%=*}" "${l#*=}"; done
}
{
  echo '#!/usr/bin/env bash'
  echo 'set -a'
  envlines "$PG_DIR/brain-1.2.env"
  if [ "$MODE" = openrouter ]; then
    envlines "$PG_DIR/brain-model.env"
    echo 'HINDSIGHT_API_LLM_PROVIDER=openai'
    echo 'HINDSIGHT_API_LLM_BASE_URL=https://openrouter.ai/api/v1'
    printf '. %q\n' "$ENV_FILE"
    echo 'HINDSIGHT_API_LLM_API_KEY="$OPENROUTER_API_KEY"'
  else
    printf 'HINDSIGHT_API_LLM_PROVIDER=openai-codex\nHINDSIGHT_API_LLM_MODEL=%s\nCODEX_HOME=%q\n' "${HINDSIGHT_CODEX_MODEL:-gpt-5.6-luna}" "$CODEX_DIR"
  fi
  [ -n "${HINDSIGHT_DB_URL:-}" ] && printf 'HINDSIGHT_API_DATABASE_URL=%q\n' "$HINDSIGHT_DB_URL"
  [ "${HINDSIGHT_CI:-}" = 1 ] && echo 'HINDSIGHT_API_SKIP_LLM_VERIFICATION=true'
  echo 'set +a'
  printf 'exec %q --host 127.0.0.1 --port %q\n' "$VENV/bin/hindsight-api" "$PORT"
} > "$RUNNER"
chmod 700 "$RUNNER"

if command -v docker >/dev/null 2>&1 && docker ps --format "{{.Names}}" 2>/dev/null | grep -q hindsight; then
  die "a Docker Hindsight is already on port $PORT. Stop it (docker stop duke-hindsight) first."
fi
if ! curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  tmux kill-session -t hindsight 2>/dev/null || true
  LOG="$HOME_DIR/logs/hindsight.log"; mkdir -p "$HOME_DIR/logs"
  tmux new-session -d -s hindsight "while true; do bash $(printf %q "$RUNNER") 2>&1 | tee -a $(printf %q "$LOG"); sleep 5; done"
  log "starting (the first start downloads the embedding models, up to 5 minutes)"
  i=0; while [ $i -lt 200 ]; do curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 3; i=$((i + 1)); done
fi
curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || { tail -n 30 "$HOME_DIR/logs/hindsight.log" 2>/dev/null; die "Hindsight did not answer. Look at: tmux attach -t hindsight"; }
log "up on port $PORT (memory model: $MODE)"

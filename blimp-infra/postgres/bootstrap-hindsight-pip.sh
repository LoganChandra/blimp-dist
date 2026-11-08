#!/usr/bin/env bash
# Hindsight without Docker: pip install + your own Codex login as its memory model.
# Used when setup runs with HINDSIGHT_MODE=pip (Codex users, no OpenRouter key).
# Hindsight docs call the Codex route "personal development use only" (ChatGPT terms).
# UNTESTED against a real Codex login: see README "Status".
set -euo pipefail
HOME_DIR="${BLIMP_HOME:-$HOME/blimp}"
VENV="$HOME_DIR/hindsight-venv"
CODEX_DIR="$HOME_DIR/hindsight-codex"      # its OWN login: Codex refresh tokens are single-use, sharing breaks them
PORT="${HINDSIGHT_API_PORT:-8888}"
VERSION="${HINDSIGHT_VERSION:-1.2.0}"
PG_DIR="$(cd "$(dirname "$0")" && pwd)"
die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "  [hindsight] $*"; }

command -v codex >/dev/null || die "Codex is not installed (npm install -g @openai/codex)."
command -v tmux  >/dev/null || die "tmux is missing."

# Hindsight's local models want Python 3.11-3.13 (its docs: 3.14 supports hosted providers only).
PY=""
for c in python3.13 python3.12 python3.11; do command -v "$c" >/dev/null && { PY="$c"; break; }; done
[[ -n "$PY" ]] || die "Need Python 3.11-3.13. Mac: brew install python@3.12. Ubuntu: sudo apt install python3.12 python3.12-venv (22.04: python3.11 python3.11-venv)."

mkdir -p "$HOME_DIR" "$CODEX_DIR"
if [[ ! -x "$VENV/bin/hindsight-api" ]]; then
  log "installing hindsight-api $VERSION (about 3 GB: it includes local embedding models; takes a while)"
  [[ -x "$VENV/bin/pip" ]] || { rm -rf "$VENV"; "$PY" -m venv "$VENV" || { rm -rf "$VENV"; die "could not create a Python venv. Ubuntu: sudo apt install ${PY}-venv"; }; }
  "$VENV/bin/pip" install -q --upgrade pip
  # Linux: the default torch wheel drags in ~2 GB of CUDA libraries; the CPU build is all this needs.
  [[ "$(uname)" == Linux ]] && "$VENV/bin/pip" install -q torch --index-url https://download.pytorch.org/whl/cpu
  "$VENV/bin/pip" install -q "hindsight-api==$VERSION"
fi

if [[ ! -s "$CODEX_DIR/auth.json" ]]; then
  log "one-time ChatGPT login for the memory (a separate login from the agent's; a browser opens)"
  if CODEX_HOME="$CODEX_DIR" codex login --help >/dev/null 2>&1; then CODEX_HOME="$CODEX_DIR" codex login
  else CODEX_HOME="$CODEX_DIR" codex auth login; fi
  [[ -s "$CODEX_DIR/auth.json" ]] || die "login did not save $CODEX_DIR/auth.json. Run: CODEX_HOME=$CODEX_DIR codex login"
fi

# Same embedding/reranker settings as the Docker setup; NOT brain-model.env (that is the OpenRouter/DeepSeek model).
RUNNER="$HOME_DIR/run-hindsight.sh"
{
  echo '#!/usr/bin/env bash'
  echo 'set -a'
  grep -E '^[A-Z_]+=' "$PG_DIR/brain-1.2.env" 2>/dev/null || true
  printf 'HINDSIGHT_API_LLM_PROVIDER=openai-codex\nHINDSIGHT_API_LLM_MODEL=%s\nCODEX_HOME=%q\n' "${HINDSIGHT_CODEX_MODEL:-gpt-5.6-luna}" "$CODEX_DIR"
  echo 'set +a'
  printf 'exec %q --host 127.0.0.1 --port %q\n' "$VENV/bin/hindsight-api" "$PORT"
} > "$RUNNER"
chmod 700 "$RUNNER"

if command -v docker >/dev/null 2>&1 && docker ps --format "{{.Names}}" 2>/dev/null | grep -q hindsight; then die "a Docker Hindsight is already on port $PORT. Stop it (docker stop duke-hindsight) or pick the OpenRouter-key memory."; fi
if ! curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  tmux kill-session -t hindsight 2>/dev/null || true
  tmux new-session -d -s hindsight "while true; do bash $(printf %q "$RUNNER"); sleep 5; done"
  log "starting (first start downloads the embedding models, up to 5 minutes)"
  for _ in $(seq 1 150); do curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 2; done
fi
curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || die "Hindsight did not answer. Look at: tmux attach -t hindsight"
log "up on port $PORT (memory model: your Codex login)"

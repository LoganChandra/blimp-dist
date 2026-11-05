#!/usr/bin/env bash
# enable-hermes-build-tools.sh
#
# Grants Hermes file + terminal + code_execution tools on his A2A (webhook)
# surface, so he can BUILD (write files / run code), not just design.
#
# WHY THIS IS A SCRIPT YOU RUN (not something Duke runs):
#   Handing an autonomous agent shell access is a privilege escalation. Duke's
#   safety layer (correctly) refuses to do it off a Discord message. Running it
#   here, at your own terminal, IS the authorization. Same pattern as a blocked
#   git push: Duke prepares, the Boss executes.
#
# "Equals" note: Hermes keeps his framework guardrails (approvals.mode=manual,
#   command_allowlist for destructive ops, tirith). This grants equal TOOLS, not
#   an unrestricted root shell — the same way Duke operates under rails.
#
# Safe by design: backs up the config, validates the YAML, restarts the gateway,
#   and AUTO-ROLLS-BACK (restores backup + restarts) if the gateway fails to come
#   up. Idempotent — re-running is fine.

set -uo pipefail

CONFIG="$HOME/.hermes/config.yaml"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP="$CONFIG.bak.voicetools.$TS"

if [ ! -f "$CONFIG" ]; then
  echo "ERR: $CONFIG not found. Is Hermes installed?" >&2
  exit 1
fi

# Find a python that has PyYAML
PY=""
for c in "$HOME/blimp/duke-venv/bin/python" python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import yaml" >/dev/null 2>&1; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "ERR: no python with PyYAML found. Install with: pip install pyyaml — then re-run. (No change made.)" >&2
  exit 1
fi

echo "============================================================"
echo " Grant Hermes build tools (file + terminal + code_execution"
echo " on his A2A/webhook surface). He keeps his guardrails"
echo " (manual approvals, destructive-command allowlist, tirith)."
echo "============================================================"
read -r -p "Proceed, the Boss? [y/N] " ans
case "$ans" in
  y|Y|yes|YES) ;;
  *) echo "Aborted. No change made."; exit 1 ;;
esac

cp -a "$CONFIG" "$BACKUP" && echo "  backed up -> $BACKUP"

# Apply the change: platform_toolsets.webhook := platform_toolsets.cli (full tools)
"$PY" - "$CONFIG" <<'PYEOF'
import sys, yaml
path = sys.argv[1]
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
pt = cfg.setdefault("platform_toolsets", {})
cli = pt.get("cli") or [
    "browser","clarify","code_execution","cronjob","delegation","file",
    "image_gen","memory","messaging","moa","session_search","skills",
    "terminal","todo","tts","vision","web",
]
pt["webhook"] = list(cli)
with open(path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
print("  platform_toolsets.webhook =", pt["webhook"])
PYEOF
if [ $? -ne 0 ]; then
  echo "ERR: config edit failed — restoring backup." >&2
  cp -a "$BACKUP" "$CONFIG"
  exit 1
fi

# Validate YAML before restarting
if ! "$PY" -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" "$CONFIG" >/dev/null 2>&1; then
  echo "ERR: resulting YAML is invalid — restoring backup." >&2
  cp -a "$BACKUP" "$CONFIG"
  exit 1
fi

echo "  restarting hermes-gateway ..."
systemctl --user restart hermes-gateway
sleep 5

if systemctl --user is-active --quiet hermes-gateway; then
  echo
  echo "OK ✅  hermes-gateway is active. Hermes now has file/terminal/code_execution on A2A."
  echo "       Tell Duke it's done — he'll verify Hermes can write a file, then hand off the build."
else
  echo
  echo "FAIL ❌  gateway did not come up — ROLLING BACK." >&2
  cp -a "$BACKUP" "$CONFIG"
  systemctl --user restart hermes-gateway
  sleep 5
  systemctl --user is-active --quiet hermes-gateway \
    && echo "       rolled back; gateway restored to working state." \
    || echo "       rollback restart also failed — run blimp-infra/bootstrap-hermes.sh to recover."
  echo "       Tell Duke it failed (config restored from $BACKUP)."
  exit 1
fi

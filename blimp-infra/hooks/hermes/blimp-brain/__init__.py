"""Hermes plugin: the same two brain hooks Duke and Frank get from Claude Code.

  pre_llm_call  (first turn of a Discord session) -> sessionstart-team-context.py: what the rest of the
                team just did, injected into the turn (Claude Code's SessionStart hook equivalent).
  post_llm_call (every Discord turn)               -> hermes-brain-capture.py, detached: the conversation
                lands in the team brain (Claude Code's Stop hook equivalent).

Both fail open: any problem returns None / does nothing, so Hermes's reply is never touched.
Installed by blimp-infra/hooks/install-brain-capture.sh (symlinked into ~/.hermes/plugins/).
"""
import os
import subprocess
from pathlib import Path

REPO = Path(os.getenv("BLIMP_REPO", str(Path.home() / "blimp" / "repo")))
HOOKS = REPO / "blimp-infra" / "hooks"
PY = os.getenv("BLIMP_PYTHON", str(Path.home() / "blimp" / "duke-venv" / "bin" / "python"))


def _python() -> str:
    return PY if Path(PY).exists() else "python3"


def team_brief(session_id="", is_first_turn=False, platform="", **kwargs):
    if not is_first_turn or platform != "discord":
        return None
    try:
        r = subprocess.run([_python(), str(HOOKS / "sessionstart-team-context.py")], input="{}",
                           capture_output=True, text=True, timeout=8,
                           env={**os.environ, "BLIMP_AGENT": "hermes"})
        out = (r.stdout or "").strip()
        return {"context": out} if out else None
    except Exception:
        return None


def skills_check(session_id="", platform="", **kwargs):
    """Every Discord turn: remind to scan the skills first (same line the Claude Code hook gives Duke and Frank)."""
    if platform != "discord":
        return None
    try:
        r = subprocess.run(["bash", str(HOOKS / "promptsubmit-skills.sh")], input="",
                           capture_output=True, text=True, timeout=5,
                           env={**os.environ, "BLIMP_AGENT": "hermes", "CLAUDE_PROJECT_DIR": str(REPO)})
        out = (r.stdout or "").strip()
        return {"context": out} if out else None
    except Exception:
        return None


def capture(session_id="", platform="", **kwargs):
    if platform != "discord" or not session_id:
        return None
    try:
        # The turn's last rows may still be on their way to state.db; give them a moment.
        subprocess.Popen(["bash", "-c", 'sleep 5; exec "$0" "$1" --session "$2"', _python(),
                          str(HOOKS / "hermes-brain-capture.py"), str(session_id)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass
    return None


def register(ctx):
    ctx.register_hook("pre_llm_call", team_brief)
    ctx.register_hook("pre_llm_call", skills_check)
    ctx.register_hook("post_llm_call", capture)

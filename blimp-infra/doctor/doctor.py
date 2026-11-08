#!/usr/bin/env python3
"""Blimp doctor: a terminal chat that helps fix setup problems.

Runs on the AI the user picked in setup (BLIMP_AI in ~/blimp/config.env):
  claude -> the Claude Code CLI      codex -> the Codex CLI
  anything else -> an OpenAI-compatible chat API (OpenRouter) with one tool,
  run_command, asked-about unless the user granted sudo/auto mode in setup.
Standard library only.
"""
import json, os, shlex, subprocess, sys, urllib.request, urllib.error
from pathlib import Path

HOME = Path(os.environ.get("BLIMP_HOME", Path.home() / "blimp"))
REPO = Path(__file__).resolve().parents[2]


def load_env(p):
    out = {}
    try:
        for line in Path(p).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


CFG = load_env(HOME / "config.env")
KEYS = load_env(HOME / ".env")
AI = CFG.get("BLIMP_AI", "claude")
NAME = CFG.get("BLIMP_NAME", "Blimp")
OS = CFG.get("BLIMP_OS", "linux")
AUTO = CFG.get("BLIMP_AUTO", "no") == "yes"  # user granted "run commands yourself"

SECRETS = [v for v in list(KEYS.values()) + [os.environ.get("OPENROUTER_API_KEY", "")] if len(v) >= 8]
CLEAN_ENV = {k: v for k, v in os.environ.items()
             if k not in ("OPENROUTER_API_KEY", "DISCORD_BOT_TOKEN") and not k.endswith(("_API_KEY", "_TOKEN"))}


def redact(text):
    for v in SECRETS:
        text = text.replace(v, "***")
    return text


PROMPT = (Path(__file__).with_name("PROMPT.md").read_text()
          .replace("{NAME}", NAME).replace("{OS}", OS)
          .replace("{REPO}", str(REPO)).replace("{HOME_DIR}", str(HOME)))
FIRST = "Start the check now: look at the latest setup log and what is installed, then tell me what you find."


def need(tool):
    from shutil import which
    if not which(tool):
        sys.exit(f"The '{tool}' command is not installed yet, so the doctor cannot run on it.\n"
                 "Run setup.sh again, or switch the doctor to an OpenRouter model: "
                 f"set BLIMP_AI=deepseek in {HOME / 'config.env'}.")


def run_claude():
    need("claude")
    cmd = ["claude", FIRST, "--append-system-prompt", PROMPT]
    if AUTO:
        cmd += ["--allowedTools", "Bash,Read,Edit,Write,Grep,Glob"]
    os.execvpe("claude", cmd, CLEAN_ENV)


def run_codex():
    need("codex")
    flag = []
    if AUTO:  # flag names differ between Codex versions: use whichever this one lists
        h = subprocess.run(["codex", "--help"], capture_output=True, text=True).stdout
        flag = ["--approve-for-me"] if "--approve-for-me" in h else (["--full-auto"] if "--full-auto" in h else [])
    os.execvpe("codex", ["codex"] + flag + [PROMPT + "\n\n" + FIRST], CLEAN_ENV)


def api_chat(messages, model, key, tools):
    body = json.dumps({"model": model, "messages": messages, "tools": tools}).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", body,
        {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]


def run_api():
    key = KEYS.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        sys.exit("No OpenRouter key saved. Re-run setup.sh and paste one.")
    model = CFG.get("BLIMP_MODEL") or {"deepseek": "deepseek/deepseek-chat"}.get(AI, AI)
    tools = [{"type": "function", "function": {
        "name": "run_command", "description": "Run one shell command on the user's computer.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}}]
    msgs = [{"role": "system", "content": PROMPT}, {"role": "user", "content": FIRST}]
    print(f"{NAME} doctor ({model}). Type your question, or 'exit'.\n")
    while True:
        try:
            m = api_chat(msgs, model, key, tools)
        except (urllib.error.URLError, KeyError, ValueError) as e:
            sys.exit(f"Could not reach the AI: {e}")
        msgs.append(m)
        if (m.get("content") or "").strip():
            print(m["content"], "\n")
        calls = m.get("tool_calls") or []
        for c in calls:
            try:
                cmd = json.loads(c["function"]["arguments"])["command"]
            except (ValueError, KeyError):
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": "bad arguments"}); continue
            print("  $", cmd)
            if not AUTO and input("  Run this? [y/N] ").strip().lower() != "y":
                out = "The user declined to run this command."
            else:
                try:
                    p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=300, cwd=REPO, env=CLEAN_ENV)
                    out = redact(p.stdout + p.stderr)[-6000:] or f"(exit {p.returncode}, no output)"
                except subprocess.TimeoutExpired:
                    out = "timed out after 300s"
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
        if calls:
            continue
        try:
            u = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if u.lower() in ("exit", "quit"):
            return
        if u:
            msgs.append({"role": "user", "content": u})


if __name__ == "__main__":
    if AI == "claude":
        run_claude()
    elif AI == "codex":
        run_codex()
    else:
        run_api()

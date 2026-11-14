#!/usr/bin/env python3
"""Discord <-> Codex bridge: lets the agent run on a Codex account alone (no Claude).

Polls the owner's Discord DM over the REST API (standard library only, no
websocket), hands each message to `codex exec`, and posts the answer back.
The shared brain is reached through the blimp-brain MCP server registered in
Codex (setup does `codex mcp add`). Answers only DISCORD_OWNER_USER_ID.
UNTESTED against a real Codex account: see README "Status".
"""
import json, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

HOME = Path(os.environ.get("BLIMP_HOME", Path.home() / "blimp"))
REPO = Path(__file__).resolve().parents[2]
API = os.environ.get("BLIMP_DISCORD_API", "https://discord.com/api/v10")


def load_env(p):
    out = {}
    try:
        for line in Path(p).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1); out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


CFG = load_env(HOME / "config.env")
STATE = HOME / "discord-state"
TOKEN = load_env(STATE / ".env").get("DISCORD_BOT_TOKEN", "")
OWNER = load_env(HOME / ".env").get("DISCORD_OWNER_USER_ID", "")
NAME = CFG.get("BLIMP_NAME", "Blimp")
AUTO = CFG.get("BLIMP_AGENT_AUTO", "no") == "yes"
HIST = STATE / "codex-history.json"
SEEN = STATE / "codex-last-id"


def discord(method, path, body=None):
    req = urllib.request.Request(API + path, json.dumps(body).encode() if body is not None else None,
        {"Authorization": "Bot " + TOKEN, "Content-Type": "application/json",
         "User-Agent": "DiscordBot (blimp-codex-bridge, 1)"}, method=method)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r) if r.status != 204 else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(json.load(e).get("retry_after", 2)) + 0.5); continue
            raise
    return {}


def codex_flags():
    """Pick the unattended-approval flag this Codex version knows."""
    try:
        h = subprocess.run(["codex", "exec", "--help"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        h = ""
    if not AUTO:
        return []
    return ["--approve-for-me"] if "--approve-for-me" in h else (["--full-auto"] if "--full-auto" in h else [])


SYSTEM = (f"You are the agent of a system called {NAME}, talking to its owner on Discord. "
          "Be short and plain. You have a shared memory through the blimp-brain tools: "
          "recall() before answering anything about past context, remember() when the owner "
          "decides something. Never invent facts; say when you do not know. "
          "Your final message is delivered to the owner for you: do not call send_discord or any other reply tool. Skills are folders in .claude/skills/<name>/SKILL.md and helper-agent prompts are in .claude/agents/*.md: before any non-trivial task, list .claude/skills, read the one that matches and follow it. Follow AGENTS.md for the working rules. Follow .claude/skills/codex-guards/SKILL.md; end any message that reports work with a line starting 'Check:'. To use a helper agent (critic, verifier, ...) read its codex-agent-<name> skill and run the command it gives. Never print keys or tokens. The repo with your rules is at " + str(REPO) + " (read AGENTS.md if asked).")


def ask_codex(history, msg):
    convo = "\n".join(f"{r}: {t[:1500]}" for r, t in history[-10:])[-9000:]
    prompt = f"{SYSTEM}\n\nConversation so far:\n{convo}\n\nOwner: {msg}\n\nReply to the owner now."
    fd, name = tempfile.mkstemp(suffix=".txt"); os.close(fd); out = Path(name)
    cmd = ["codex", "exec", "--skip-git-repo-check", "-C", str(REPO), "-o", str(out)] + codex_flags() + [prompt]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        return "Codex is not installed on this computer. Run setup.sh again."
    except subprocess.TimeoutExpired:
        return "That took over 15 minutes, so I stopped. Try a smaller ask."
    text = out.read_text().strip() if out.exists() else ""
    out.unlink(missing_ok=True)
    if not text:
        tail = (p.stderr or p.stdout or "").strip()[-400:]
        return "Codex gave no answer." + (f" Last output: {tail}" if tail else " Run the doctor: python3 blimp-infra/doctor/doctor.py")
    return text


DONE_WORDS = re.compile(r"\b(done|fixed|shipped|deployed|completed|all set|finished|verified)\b", re.I)


def brain(*args):
    """Call the memory CLI (duke-mem / duke-data); never let a failure stop the bridge."""
    try:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=20,
                              env={**os.environ, "PATH": f"{Path.home()}/.local/bin:/usr/local/bin:" + os.environ.get("PATH", "")}).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def context_line():
    """What the Claude prompt-submit hook adds: open-task count + the recall-first reminder."""
    n = brain("duke-data", "tasks", "--status", "in_progress")
    open_tasks = len([l for l in n.splitlines() if l.strip()]) if n else "?"
    return f"[context: open tasks: {open_tasks}. Recall before asserting anything about the past.]"


def with_check(reply, msg):
    """Verify-before-done guard: a completion claim needs a 'Check:' line, else ask once, else label it."""
    if not DONE_WORDS.search(reply) or re.search(r"^Check:", reply, re.M | re.I):
        return reply
    again = ask_codex([], f"{msg}\n\nYour draft reply was:\n{reply}\n\nIt claims completion but shows no check. "
                          "Run a real check now (or say it is unverified) and give the final reply ending with a line "
                          "'Check: <what you ran and what it showed>' or 'Check: none (unverified)'.")
    if re.search(r"^Check:", again, re.M | re.I):
        return again
    return reply + "\n\nCheck: none shown (unverified)"


def send(chan, text):
    text = text or "(empty)"
    while text:
        discord("POST", f"/channels/{chan}/messages", {"content": text[:1900]})
        text = text[1900:]


def main():
    if not (TOKEN and OWNER):
        sys.exit("Missing Discord token or owner id. Run setup.sh.")
    chan = discord("POST", "/users/@me/channels", {"recipient_id": OWNER})["id"]
    try: hist = json.loads(HIST.read_text())
    except (OSError, ValueError): hist = []
    last = SEEN.read_text().strip() if SEEN.exists() else None
    if last is None:  # first start: ignore old messages
        msgs = discord("GET", f"/channels/{chan}/messages?limit=1")
        last = msgs[0]["id"] if msgs else "0"
    print(f"{NAME} (Codex) listening on DM with owner {OWNER[:4]}...", flush=True)
    while True:
        try:
            msgs = discord("GET", f"/channels/{chan}/messages?after={last}&limit=20")
            for m in sorted(msgs, key=lambda x: int(x["id"])):
                if m["author"]["id"] != OWNER or not m.get("content", "").strip():
                    last = m["id"]; SEEN.write_text(last); continue
                discord("POST", f"/channels/{chan}/typing")
                reply = with_check(ask_codex(hist, context_line() + "\n" + m["content"]), m["content"])
                brain("duke-mem", "log", "--user", "owner", "--channel", "discord", "--speaker", "owner", "--message", m["content"][:4000])
                brain("duke-mem", "log", "--user", "owner", "--channel", "discord", "--speaker", "agent", "--message", reply[:4000])
                hist += [["Owner", m["content"]], ["You", reply]]; hist[:] = hist[-40:]
                HIST.write_text(json.dumps(hist)); send(chan, reply)
                last = m["id"]; SEEN.write_text(last)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            print("poll error:", e, flush=True); time.sleep(10)
        time.sleep(3)


if __name__ == "__main__":
    main()

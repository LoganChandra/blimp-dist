#!/usr/bin/env python3
"""stop-brain-capture.py: every Discord conversation lands in the team brain automatically.

Why: logging depended on each agent *remembering* to call log_message. It decayed from about 400 turns a
month (July) to about 20 (Sept), and stopped for Duke on 2026-09-02. On 2026-11-05 the brain knew nothing
of a full day of ProfAI work Duke did with Han. The fix is the pattern the field uses (claude-mem, Hermes
memory providers, Letta sleep-time agents): capture at the runtime layer, not by model discipline.

What it does, as a Claude Code Stop hook (or by hand with --backfill):
  - reads the session transcript;
  - finds every inbound Discord message and every outbound reply tool call;
  - groups them into exchanges (the message(s) someone sent + the agent's replies), so a reply like
    "done, it's live" is stored next to the question it answers;
  - names each speaker from brain-capture.json (the Boss, Han, ...), never assumes it was the Boss;
  - strips secrets (API keys, tokens, passcodes) before anything leaves the machine;
  - writes each exchange to Hindsight as one conversation document, "Name (time UTC): text", with the
    real Discord timestamp and document_id = discord:<agent>:<first message id>. A re-run only re-sends
    an exchange whose text changed (same id, so Hindsight replaces it): idempotent and cheap.

Never blocks and never wedges the agent: the Stop hook forks, returns 0 at once, and every failure fails
open with a line in ~/.cache/blimp-brain-capture/capture.log. Stdlib only, so any python3 runs it.

Agent = $BLIMP_AGENT, else inferred from the session cwd (nanook -> "nanook", blimp or duke -> "duke").
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

STATE_DIR = Path(os.getenv("BLIMP_CAPTURE_STATE") or Path.home() / ".cache" / "blimp-brain-capture")
INBOUND = re.compile(r'<channel source="plugin:discord:discord"([^>]*)>\n?(.*?)\n?</channel>', re.S)
ATTR = re.compile(r'(\w+)="([^"]*)"')
REPLY_TOOL = "mcp__plugin_discord_discord__reply"
MAX_MSG_CHARS = 4000     # one very long message is cut, not dropped
MAX_DOC_CHARS = 24000    # one exchange; Hindsight chunks inside it
BATCH = 10

# Secrets never reach the brain (or the extraction model). Specific shapes first, then labelled values.
_SECRET_SHAPES = [
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    r"\bsk-(?:or-v1-|ant-|proj-)?[A-Za-z0-9_-]{20,}",
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}",
    r"\bglpat-[A-Za-z0-9_-]{20,}|\bnpm_[A-Za-z0-9]{36}|\bhf_[A-Za-z0-9]{30,}|\bgsk_[A-Za-z0-9]{40,}",
    r"\bxox[abpr]-[A-Za-z0-9-]{10,}",
    r"\bAIza[0-9A-Za-z_-]{35}|\bGOCSPX-[A-Za-z0-9_-]{20,}",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    r"\b[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,40}\b",
    r"\b(?:rk|pk|sk)_(?:live|test)_[A-Za-z0-9]{16,}|\bwhsec_[A-Za-z0-9]{20,}",
    r"\bsb_secret_[A-Za-z0-9_-]{16,}|\bsbp_[A-Za-z0-9]{30,}|\bpolar_(?:oat|at|pat)_[A-Za-z0-9_-]{20,}",
    r"\bre_[A-Za-z0-9]{8,}_[A-Za-z0-9]{8,}",
    r"(?<=\b[Bb]earer )[A-Za-z0-9._~+/-]{16,}=*|(?<=\bBEARER )[A-Za-z0-9._~+/-]{16,}=*",
]
SECRET = re.compile("|".join(_SECRET_SHAPES), re.S)
# postgresql://user:PASSWORD@host -> keep the URL, drop the password.
URL_CRED = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)([^@\s]+)(@)")
# OPENAI_API_KEY=..., CRON_SECRET: ..., DB_PASSWORD=...
ENV_SECRET = re.compile(r"(\b[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|PASS)[A-Z0-9_]*\s*[=:]\s*)(['\"]?)([^\s'\"]+)")
# Passwords: after "password/passcode/passphrase is|:|=" the whole clause goes ("the wifi password is sunflower",
# "Passphrase: correct horse battery staple", "Wifi password: Blimp Guest 2026"), unless it starts with an
# ordinary word ("is working", "is in Bitwarden", "Password: min 8 chars") or is a number with a unit ("is 8
# characters long"). A clause ends at , ; ! ? a newline, or a full stop followed by a space, so a second password
# later on the same line is matched on its own. Over-redacting a sentence is the cheap failure; a leak is not.
PASSWORD = re.compile(r"(?i)(?<![A-Za-z0-9_])(pass ?code|password|passwd|passphrase|app password)(\s+is\s+|\s*[:=]\s*)"
                      r"(```[^\n]*\n[\s\S]*?```|`[^`\n]+`|\"[^\"\n]+\"|(?:[^\n,;!?.]|[,;!?.](?!\s|$))+)")
_NOT_SECRET = set("""a an the not no now still also only just never always too very being been set reset changed
working wrong required optional incorrect invalid valid missing empty expired weak strong stored saved hashed sent
needed fine ok okay broken locked same different in on at for from to with by of my your our their his her its
what which where how when who usual encrypted hidden shown visible min max minimum maximum least protected managed
handled saved""".split())
_UNITS = r"(?:lines?|tokens?|rows?|chars?|characters?|digits?|letters?|words?|ms|px|%|seconds?|secs?|minutes?|mins?|hours?|days?)\b"


def _password(m: re.Match) -> str:
    label, sep, clause = m[1], m[2], m[3]
    if clause.startswith("```"):  # a copyable code block after the label: all of it
        return f"{label}{sep}```\n[secret removed]\n```"
    first = re.match(r"\s*([^\s]+)", clause)
    word = first[1].strip("`\"'").lower() if first else ""
    if label.lower() != "passphrase" and (not word or word in _NOT_SECRET or ("'" in first[1] and not first[1].startswith("'"))
            or re.match(r"\s*\d+\s+" + _UNITS, clause)):
        return m[0]
    lead = clause[: len(clause) - len(clause.lstrip())]
    return f"{label}{sep}{lead}[secret removed]"


# "API key: abc123def456ghi7", "token = ..." only when the value looks random (letters + digits, 12+ chars).
LABELLED_KEY = re.compile(r"(?i)(?<![A-Za-z0-9_])(secret|token|api[ _-]?key|access[ _-]?key|client[ _-]?secret)(\s+is\s+|\s*[:=]\s*)(`?)"
                          r"((?=[^\s`]*[A-Za-z])(?=[^\s`]*\d)(?![^\s`]*://)[^\s`]{12,})(`?)")
# "PIN: 4821", "verification code is 482913", "Code: `pf-de308d4bfcca`". "status code: 500" stays (3 digits).
LABELLED_CODE = re.compile(r"(?i)(?<![A-Za-z0-9_])(pin|otp|code|passcode)(\s+is\s+|\s*[:=]\s*|\s+(?=\d{4,8}\b))"
                           r"(?:`((?=[^`]*[A-Za-z])(?=[^`]*\d)[^`\s]{6,}|\d{4,8})`|(\d{4,8})\b(?!\s*" + _UNITS + r"|-\d))")


def redact(text: str) -> str:
    text = SECRET.sub("[secret removed]", text)
    text = URL_CRED.sub(r"\1[secret removed]\3", text)
    text = ENV_SECRET.sub(r"\1\2[secret removed]", text)
    text = PASSWORD.sub(_password, text)
    text = LABELLED_KEY.sub(lambda m: f"{m[1]}{m[2]}{m[3]}[secret removed]{m[5]}", text)
    return LABELLED_CODE.sub(lambda m: f"{m[1]}{m[2]}" + ("`[secret removed]`" if m[3] else "[secret removed]"), text)


def names() -> dict:
    try:
        return json.loads((HERE / "brain-capture.json").read_text())
    except Exception:
        return {"people": {}, "channels": {}, "agents": {}}


def agent_for(cwd: str) -> str:
    if os.getenv("BLIMP_AGENT"):
        return os.environ["BLIMP_AGENT"]
    c = cwd.lower()
    if "nanook" in c:
        return "nanook"
    if "blimp" in c or "duke" in c:
        return "duke"
    return ""


_MID = re.compile(r"^\d{1,25}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _inbound_texts(d: dict):
    """Where a Discord message really arrives in a transcript. Tool output is never one of them, so a tool
    that prints a <channel> tag (a test fixture, Frank reading Duke's log) can't fake a message."""
    if d.get("isSidechain"):
        return
    if d.get("type") == "queue-operation" and d.get("operation") == "enqueue":
        yield d.get("content") or ""
    elif d.get("type") == "attachment":
        a = d.get("attachment") or {}
        if a.get("type") == "queued_command":
            yield a.get("prompt") or ""
    elif d.get("type") == "user" and "toolUseResult" not in d:
        c = (d.get("message") or {}).get("content")
        if isinstance(c, str):
            yield c
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    yield b.get("text") or ""


_ENTRY = re.compile(rb'"entrypoint"\s*:\s*"([^"]*)"')


def extract(transcript: Path, since: str = "") -> list[dict]:
    """Discord messages in the transcript, in the order the agent saw them.

    The whole file is read every time: a tail window can start inside an exchange and either split it
    into a second document or lose its question. Only lines that can hold a Discord message are parsed,
    so even a 100 MB transcript takes a fraction of a second, in the background.
    Order is transcript position, not timestamps: inbound times come from Discord's clock and outbound
    from this PC's, and a skewed WSL clock must not attach a reply to the wrong question.
    Each message: {id, ts, out (bool), who_id, who, chat_id, text}.
    """
    lines = []
    with transcript.open("rb") as f:
        for l in f:
            # Headless runs (evals, `claude -p`, even one appended to an interactive session with --resume)
            # are not "cli", and some feed in made-up Discord messages ("TEST HARNESS: ..."). Only a purely
            # interactive session talks to real people.
            if b'"entrypoint"' in l and any(e != b"cli" for e in _ENTRY.findall(l)):
                return []
            if b"plugin:discord:discord" in l or REPLY_TOOL.encode() in l:
                lines.append(l.decode("utf-8", "replace"))
    return [m for m in _parse(lines) if m["ts"] >= since]


def _parse(lines: list[str]) -> list[dict]:
    seen: dict[str, dict] = {}
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        stamp = d.get("timestamp") or ""
        if d.get("type") == "assistant" and not d.get("isSidechain"):
            for c in (d.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == REPLY_TOOL:
                    inp = c.get("input") or {}
                    text = (inp.get("text") or "").strip()
                    if text and stamp and c.get("id") not in seen:
                        seen[c["id"]] = {"id": c["id"], "ts": stamp, "out": True, "who_id": "",
                                         "chat_id": str(inp.get("chat_id") or ""), "text": text}
            continue
        for s in _inbound_texts(d):
            if "plugin:discord:discord" not in s:
                continue
            for attrs, text in INBOUND.findall(s):
                a = dict(ATTR.findall(attrs))
                mid, ts = a.get("message_id", ""), a.get("ts", "")
                if _MID.match(mid) and _TS.match(ts) and mid not in seen and text.strip():
                    seen[mid] = {"id": mid, "ts": ts, "out": False, "who_id": a.get("user_id", ""),
                                 "who": a.get("user", ""), "chat_id": a.get("chat_id", ""), "text": text.strip()}
    return list(seen.values())  # dicts keep insertion order = transcript order


def exchanges(msgs: list[dict]) -> list[list[dict]]:
    """Group messages into exchanges, per channel: what someone sent, then the agent's replies.

    A reply joins the open exchange in its own channel, even if the agent posted elsewhere in between.
    A new exchange starts in a channel when someone writes there after the agent has replied there.
    """
    groups: list[list[dict]] = []
    open_by_chat: dict[str, list[dict]] = {}
    for m in msgs:
        g = open_by_chat.get(m["chat_id"])
        if g is None or (not m["out"] and any(x["out"] for x in g)):
            g = [m]
            groups.append(g)
            open_by_chat[m["chat_id"]] = g
        else:
            g.append(m)
    return groups


def render(agent: str, group: list[dict], cfg: dict) -> dict:
    people, chans = cfg.get("people", {}), cfg.get("channels", {})
    me = cfg.get("agents", {}).get(agent, agent.capitalize())
    chan = chans.get(group[0]["chat_id"]) or (f"dm-{group[0]['chat_id']}" if group[0]["chat_id"] else "discord")
    lines, who = [], []
    for m in group:
        name = me if m["out"] else (people.get(m["who_id"]) or m.get("who") or "someone")
        if m.get("unverified") and not m["out"]:
            name += " (display name)"
        who.append(name)
        lines.append(f"{name} ({m['ts'][:16].replace('T', ' ')} UTC): {redact(m['text'][:MAX_MSG_CHARS])}")
    body = "\n\n".join(lines)[:MAX_DOC_CHARS]
    speakers = sorted(set(who))
    return {
        "doc_id": f"discord:{agent}:{group[0]['id']}",
        "hash": hashlib.sha1(body.encode()).hexdigest(),
        "item": {
            "content": body,
            # Hindsight files a fact as "experience" (the agent's own) or "world" based on this line, so it
            # must say which speaker is the agent. The bank is shared by three agents; naming none let
            # Frank's and Hermes's actions be read as the bank's default agent's (Duke's).
            "context": (f"Discord #{chan}. {me} is the Blimp AI agent in this conversation: lines starting "
                        f"'{me} (' are {me}'s own words and actions. "
                        + (f"Also speaking: {', '.join(s for s in speakers if s != me)}." if len(speakers) > 1 else "")),
            "timestamp": group[-1]["ts"],
            "document_id": f"discord:{agent}:{group[0]['id']}",
            "tags": ["conversation", "capture:auto", f"channel:{agent}", f"discord:{chan}", f"author:{agent}"]
                    + [f"speaker:{s.lower().replace(' (display name)', '')}" for s in speakers],
            "metadata": {"kind": "chat", "channel": agent, "discord_channel": chan, "author": agent,
                         "speakers": ",".join(speakers), "verified": str(not any(m.get("unverified") for m in group)), "source": "stop-brain-capture",
                         "first_message_id": group[0]["id"], "messages": str(len(group))},
        },
    }


def load_state(agent: str) -> dict:
    try:
        v = json.loads((STATE_DIR / f"{agent}.v2.json").read_text())
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def save_state(agent: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    keep = dict(sorted(state.items(), key=lambda kv: kv[1][1])[-20000:])  # newest 20k exchanges
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=f".{agent}.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(keep, f)
    os.replace(tmp, STATE_DIR / f"{agent}.v2.json")


@contextlib.contextmanager
def locked(agent: str):
    """One writer per agent at a time (two quick Stops, or a backfill next to the live hook)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / f"{agent}.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _post(items: list[dict]) -> None:
    url = (f"{os.getenv('HINDSIGHT_URL', 'http://localhost:8888')}/v1/default/banks/"
           f"{os.getenv('DUKE_BANK', 'duke')}/memories")
    req = urllib.request.Request(url, data=json.dumps({"items": items, "async": True}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=float(os.getenv("BLIMP_CAPTURE_TIMEOUT", "20"))) as r:
        r.read()


def write(agent: str, msgs: list[dict], dry: bool = False, post=_post) -> int:
    groups = exchanges(msgs)
    with locked(agent):
        return _write(agent, groups, dry, post)


def _write(agent: str, groups: list[list[dict]], dry: bool, post) -> int:
    cfg, state = names(), load_state(agent)
    todo = [r for r in (render(agent, g, cfg) for g in groups) if state.get(r["doc_id"], ["", ""])[0] != r["hash"]]
    if dry:
        return len(todo)
    for i in range(0, len(todo), BATCH):
        batch = todo[i : i + BATCH]
        post([r["item"] for r in batch])
        for r in batch:
            state[r["doc_id"]] = [r["hash"], r["item"]["timestamp"]]
        save_state(agent, state)
    return len(todo)


def _log(line: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "capture.log").open("a") as f:
        f.write(f"{time.strftime('%F %T')} {line}\n")


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--backfill":
        # --backfill <agent> <since-ISO> [--dry] <transcript>...
        agent, since = args[1], args[2]
        dry = "--dry" in args
        # One transcript at a time, exactly as the live hook groups it, so both produce the same documents.
        files = sorted((Path(a) for a in args[3:] if a != "--dry" and Path(a).is_file()), key=lambda p: p.stat().st_mtime)
        n = found = chars = 0
        for f in files:
            msgs = extract(f, since=since)
            found += len(msgs)
            chars += sum(len(m["text"]) for m in msgs)
            n += write(agent, msgs, dry=dry)
        print(json.dumps({"agent": agent, "messages": found, "exchanges_to_send": n, "chars": chars, "dry": dry}))
        if not dry:
            _log(f"{agent} backfill +{n}")
        return 0

    # Stop hook: read the payload, fork, return at once.
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    transcript = Path(payload.get("transcript_path") or "")
    agent = agent_for(payload.get("cwd") or os.getcwd())
    if not agent or not transcript.is_file():
        return 0
    if os.fork() != 0:
        return 0
    try:
        os.setsid()
        # Let go of the hook's pipes. Without this Claude Code waits for EOF, i.e. for the whole write.
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        try:
            n = write(agent, extract(transcript))
            if n:
                _log(f"{agent} +{n}")
        except Exception as e:  # fail open, but leave a trace the SessionStart hook reports
            _log(f"{agent} ERROR {e!r}"[:500])
    finally:
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())

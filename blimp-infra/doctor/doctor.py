#!/usr/bin/env python3
"""Blimp doctor: checks every part of an install, explains what is wrong in
plain English, and fixes it.

Two layers, in this order:
  1. Checks: fixed code, no AI, no network beyond the services being checked.
     Free, fast, and they work even when the AI login is the broken part.
  2. Chat: only if a problem is left that the checks cannot fix. A terminal
     chat on the AI login the user picked in setup (Claude Code or Codex), or an
     OpenRouter model. Never on anyone else's account or key.

Usage:
  doctor.py              check, offer to fix, then offer the chat
  doctor.py --fix        check and fix (asks before each fix)
  doctor.py --fix --yes  check and fix without asking
  doctor.py --lint       check only, change nothing; exit 1 if anything failed
  doctor.py --json       results as JSON (implies --lint)
  doctor.py --chat       go straight to the chat
  --skip-human           skip checks that need a person (logins, live Discord token)

Exit codes: 0 healthy, 1 problems left, 2 the doctor itself failed.
Standard library only; Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("BLIMP_HOME", Path.home() / "blimp"))
REPO = Path(__file__).resolve().parents[2]
IS_MAC = sys.platform == "darwin"
STATE = HOME / "discord-state"
HS_PORT = os.environ.get("HINDSIGHT_API_PORT", "8888")


def load_env(p: Path) -> dict:
    out = {}
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


CFG = load_env(HOME / "config.env")
KEYS = load_env(HOME / ".env")
AI = CFG.get("BLIMP_AI", "claude")
RUNTIME = "codex" if AI == "codex" else "claude"
MEMORY = CFG.get("BLIMP_MEMORY", "")
NAME = CFG.get("BLIMP_NAME", "Blimp")
SECRETS = [v for v in KEYS.values() if len(v) >= 8]
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN"))}


def redact(text: str) -> str:
    for v in SECRETS:
        text = text.replace(v, "***")
    return text


# ---- small helpers ---------------------------------------------------------

EXTRA_PATH = [str(Path.home() / p) for p in (".local/bin", ".bun/bin", ".npm-global/bin")]
if IS_MAC:
    EXTRA_PATH += ["/opt/homebrew/bin", "/usr/local/bin", "/opt/homebrew/opt/postgresql@17/bin",
                   "/usr/local/opt/postgresql@17/bin"]
os.environ["PATH"] = os.pathsep.join(EXTRA_PATH + [os.environ.get("PATH", "")])


def which(cmd: str) -> str | None:
    return shutil.which(cmd)


def sh(cmd: list[str] | str, timeout: int = 20) -> tuple[int, str]:
    """Run a command, return (exit code, combined output). Never raises."""
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
                           timeout=timeout, env=CLEAN_ENV, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "not installed"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as e:
        return 126, str(e)


def version_tuple(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x) for x in m.groups() if x is not None) if m else ()


def http_ok(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def brew_install(pkg: str) -> str:
    return f"brew install {pkg}"


def pkg_fix(mac: str, apt: str) -> list[str]:
    return [brew_install(mac)] if IS_MAC else [f"sudo apt-get install -y {apt}"]


# ---- results ---------------------------------------------------------------

class R:
    """One check's result. status: ok | warn | fail | skip."""

    def __init__(self, status, detail, fix=None, hint=None):
        self.status, self.detail, self.fix, self.hint = status, detail, fix or [], hint


def ok(d): return R("ok", d)
def warn(d, fix=None, hint=None): return R("warn", d, fix, hint)
def fail(d, fix=None, hint=None): return R("fail", d, fix, hint)
def skip(d): return R("skip", d)


CHECKS: list[tuple[str, str, str, object]] = []   # (id, group, title, fn)


def check(cid, group, title):
    def deco(fn):
        CHECKS.append((cid, group, title, fn))
        return fn
    return deco


START_FIX = [f"bash {shlex.quote(str(REPO / 'setup.sh'))} --start"]


# ---- System ----------------------------------------------------------------

@check("os", "System", "Operating system")
def c_os(ctx):
    if IS_MAC:
        v = platform.mac_ver()[0] or "?"
        arch = platform.machine()
        if version_tuple(v) and version_tuple(v) < (13,):
            return warn(f"macOS {v} ({arch}); Blimp is built for macOS 13 or newer",
                        hint="Update macOS in System Settings, General, Software Update.")
        return ok(f"macOS {v} ({arch})")
    if sys.platform.startswith("linux"):
        name = "Linux"
        try:
            rel = load_env(Path("/etc/os-release"))
            name = rel.get("PRETTY_NAME", name)
        except Exception:
            pass
        wsl = " on Windows (WSL)" if "microsoft" in platform.release().lower() else ""
        return ok(f"{name}{wsl}")
    return fail(f"{sys.platform} is not supported", hint="Blimp runs on macOS, Ubuntu, or Windows through WSL.")


@check("disk", "System", "Free disk space")
def c_disk(ctx):
    free = shutil.disk_usage(Path.home()).free / 1e9
    if free < 3:
        return fail(f"{free:.1f} GB free; setup needs about 8 GB",
                    hint="Empty the Trash or remove large files, then run the doctor again.")
    if free < 8:
        return warn(f"{free:.1f} GB free; the memory models need about 8 GB")
    return ok(f"{free:.0f} GB free")


@check("network", "System", "Internet")
def c_network(ctx):
    for host in ("github.com", "pypi.org"):
        try:
            socket.create_connection((host, 443), timeout=4).close()
        except OSError:
            return warn(f"cannot reach {host}", hint="Check the Wi-Fi or a VPN or firewall that blocks it.")
    return ok("reaches GitHub and PyPI")


@check("config", "System", "Setup answers")
def c_config(ctx):
    if not (HOME / "config.env").exists():
        return fail("setup has not run yet", fix=[f"bash {shlex.quote(str(REPO / 'setup.sh'))}"])
    missing = [k for k in ("BLIMP_OS", "BLIMP_AI", "BLIMP_NAME") if not CFG.get(k)]
    if missing:
        return fail("setup stopped before the first questions were answered", fix=[f"bash {shlex.quote(str(REPO / 'setup.sh'))}"])
    env = HOME / ".env"
    if env.exists() and env.stat().st_mode & 0o077:
        return warn("your keys file can be read by other users of this computer", fix=[f"chmod 600 {shlex.quote(str(env))}"])
    return ok(f"{NAME}, AI: {AI}, memory: {MEMORY or 'on'}")


# ---- Tools -----------------------------------------------------------------

@check("brew", "Tools", "Homebrew")
def c_brew(ctx):
    if not IS_MAC:
        return skip("only used on a Mac")
    if not which("brew"):
        return fail("Homebrew is not installed", hint="Run setup again: it installs Homebrew for you (it asks for your Mac password).")
    return ok(sh(["brew", "--version"])[1].splitlines()[0])


@check("basics", "Tools", "git, curl, tmux, jq")
def c_basics(ctx):
    need = [t for t in ("git", "curl", "tmux", "jq") if not which(t)]
    if need:
        return fail("missing: " + ", ".join(need), fix=pkg_fix(" ".join(need), " ".join(need)))
    return ok("all installed")


@check("python", "Tools", "Python")
def c_python(ctx):
    v = version_tuple(platform.python_version())
    if v < (3, 9):
        return fail(f"Python {platform.python_version()} is too old (need 3.9+)", fix=pkg_fix("python@3.13", "python3"))
    return ok(f"Python {platform.python_version()}")


@check("bun", "Tools", "Bun (runs the Discord plugin)")
def c_bun(ctx):
    if RUNTIME != "claude":
        return skip("not used with Codex")
    if not which("bun"):
        return fail("Bun is not installed", fix=["curl -fsSL https://bun.sh/install | bash"])
    return ok("Bun " + sh(["bun", "--version"])[1])


@check("node", "Tools", "Node.js")
def c_node(ctx):
    if RUNTIME != "codex":
        return skip("only needed for Codex")
    if not which("node"):
        return fail("Node.js is not installed", fix=pkg_fix("node", "nodejs"))
    v = version_tuple(sh(["node", "-v"])[1])
    if v and v[0] < 22:
        return warn(f"Node {'.'.join(map(str, v))} is past end of life; 22 or newer is supported",
                    fix=["brew upgrade node"] if IS_MAC else None)
    return ok("Node " + sh(["node", "-v"])[1])


@check("ai_cli", "Tools", "AI program")
def c_ai_cli(ctx):
    cli = RUNTIME
    if not which(cli):
        fix = ["curl -fsSL https://claude.ai/install.sh | bash"] if cli == "claude" else ["npm install -g @openai/codex"]
        return fail(f"{'Claude Code' if cli == 'claude' else 'Codex'} is not installed", fix=fix)
    return ok(f"{cli} {sh([cli, '--version'])[1].splitlines()[0] if sh([cli, '--version'])[0] == 0 else ''}".strip())


@check("ai_login", "Tools", "AI login")
def c_ai_login(ctx):
    if ctx.skip_human:
        return skip("needs a person (skipped)")
    if not which(RUNTIME):
        return skip("AI program not installed yet")
    if RUNTIME == "claude":
        rc, out = sh(["claude", "auth", "status"])
        if rc != 0 or '"loggedIn": false' in out:
            return fail("Claude Code is not logged in",
                        hint="Run: claude   then sign in in the browser window, then type /exit.")
        return ok("Claude Code is logged in")
    rc, out = sh(["codex", "login", "status"])
    if rc != 0:
        return fail("Codex is not logged in", hint="Run: codex login")
    return ok("Codex is logged in")


@check("plugin", "Tools", "Discord plugin")
def c_plugin(ctx):
    if RUNTIME != "claude":
        return skip("Codex uses the built-in bridge")
    if not which("claude"):
        return skip("Claude Code not installed yet")
    rc, out = sh(["claude", "plugin", "list"], timeout=30)
    if "discord@claude-plugins-official" not in out:
        return fail("the Discord plugin is not installed",
                    fix=["claude plugin marketplace add anthropics/claude-plugins-official",
                         "claude plugin install discord@claude-plugins-official"])
    return ok("installed")


# ---- Memory ----------------------------------------------------------------

def memory_off():
    return MEMORY == "none"


def venv_py() -> Path:
    return HOME / "duke-venv" / "bin" / "python"


def db_url() -> str:
    return KEYS.get("DUKE_DB_URL", "")


def db_query(sql: str) -> tuple[int, str]:
    code = ("import sys, psycopg\n"
            "with psycopg.connect(sys.argv[1], connect_timeout=5) as c:\n"
            "    r = c.execute(sys.argv[2]).fetchall()\n"
            "    print(repr(r))\n")
    return sh([str(venv_py()), "-c", code, db_url(), sql])


@check("venv", "Memory", "Python environment")
def c_venv(ctx):
    if memory_off():
        return skip("memory is off")
    if not venv_py().exists():
        return fail("the memory's Python environment is missing", fix=[f"bash {shlex.quote(str(REPO / 'setup.sh'))}"])
    rc, out = sh([str(venv_py()), "-c", "import psycopg, httpx, mcp"])
    if rc != 0:
        return fail("the memory's Python packages are broken",
                    fix=[f"{shlex.quote(str(venv_py().parent / 'pip'))} install -r {shlex.quote(str(REPO / 'blimp-infra/postgres/requirements.txt'))}"])
    return ok("ready")


@check("postgres", "Memory", "Database (Postgres)")
def c_postgres(ctx):
    if memory_off():
        return skip("memory is off")
    if not db_url():
        return fail("no database address saved", fix=[f"bash {shlex.quote(str(REPO / 'setup.sh'))}"])
    if not venv_py().exists():
        return skip("needs the Python environment first")
    rc, out = db_query("select version()")
    if rc != 0:
        fix = ["brew services restart postgresql@17"] if IS_MAC else ["sudo service postgresql start"]
        return fail("the database is not answering", fix=fix, hint=redact(out.splitlines()[-1] if out else ""))
    m = re.search(r"PostgreSQL (\d+(\.\d+)?)", out)
    return ok(f"PostgreSQL {m.group(1) if m else ''} answering".replace("  ", " "))


@check("pgvector", "Memory", "Vector search (pgvector)")
def c_pgvector(ctx):
    if memory_off():
        return skip("memory is off")
    if ctx.results.get("postgres") and ctx.results["postgres"].status != "ok":
        return skip("needs the database first")
    rc, out = db_query("select extversion from pg_extension where extname='vector'")
    if rc != 0 or "[]" in out:
        fix = (["brew install pgvector", "psql -d duke_data -c 'CREATE EXTENSION IF NOT EXISTS vector'"]
               if IS_MAC else [f"bash {shlex.quote(str(REPO / 'setup.sh'))}"])
        return fail("pgvector is not enabled in the database", fix=fix)
    return ok("pgvector " + re.sub(r"[^\d.]", "", out))


@check("schema", "Memory", "Database tables")
def c_schema(ctx):
    if memory_off():
        return skip("memory is off")
    if ctx.results.get("postgres") and ctx.results["postgres"].status != "ok":
        return skip("needs the database first")
    rc, out = db_query("select count(*) from information_schema.tables where table_name in "
                       "('tasks','prospects','project_state','a2a_messages')")
    n = re.search(r"\((\d+),?\)", out)
    if rc != 0 or not n or int(n.group(1)) < 4:
        return fail("the database tables are missing",
                    fix=[f"psql \"$DUKE_DB_URL\" -f {shlex.quote(str(REPO / 'blimp-infra/postgres/schema.sql'))}"])
    return ok("in place")


@check("hindsight", "Memory", "Long-term memory (Hindsight)")
def c_hindsight(ctx):
    if memory_off():
        return skip("memory is off")
    if http_ok(f"http://127.0.0.1:{HS_PORT}/health"):
        return ok(f"answering on port {HS_PORT}")
    if (HOME / "run-hindsight.sh").exists():
        return fail("Hindsight is installed but not running", fix=START_FIX,
                    hint="Its own log: tmux attach -t hindsight")
    if which("docker") and sh(["docker", "inspect", "duke-hindsight"])[0] == 0:
        return fail("the Hindsight container is stopped", fix=["docker start duke-hindsight"])
    return fail("Hindsight is not installed", fix=[f"bash {shlex.quote(str(REPO / 'setup.sh'))}"])


@check("mcp", "Memory", "Memory connected to the agent")
def c_mcp(ctx):
    if memory_off():
        return skip("memory is off")
    if not which(RUNTIME):
        return skip("AI program not installed yet")
    rc, out = sh([RUNTIME, "mcp", "get", "blimp-brain"], timeout=30)
    if rc != 0:
        return fail("the agent is not connected to the memory", fix=[f"bash {shlex.quote(str(REPO / 'setup.sh'))}"])
    return ok("blimp-brain is registered")


# ---- Discord ---------------------------------------------------------------

def bot_token() -> str:
    return load_env(STATE / ".env").get("DISCORD_BOT_TOKEN", "") or KEYS.get("DISCORD_BOT_TOKEN", "")


@check("token", "Discord", "Bot token")
def c_token(ctx):
    t = bot_token()
    if not t:
        return fail("no Discord bot token saved", fix=[f"bash {shlex.quote(str(REPO / 'setup.sh'))}"])
    if t.count(".") != 2 or len(t) < 50:
        return fail("the saved bot token does not look like a Discord token",
                    hint=f"Copy it again from Discord's developer portal (Bot, Reset Token), "
                         f"put it in {HOME / '.env'} as DISCORD_BOT_TOKEN=..., and run setup again.")
    if not (STATE / ".env").exists():
        return fail("the Discord plugin has no copy of the token", fix=START_FIX)
    if ctx.skip_human:
        return ok("saved (live check skipped)")
    return discord_live(t)


def discord_api(path: str, token: str):
    req = urllib.request.Request("https://discord.com/api/v10" + path,
                                 headers={"Authorization": "Bot " + token, "User-Agent": "BlimpDoctor (local, 1.0)"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def discord_live(t: str) -> R:
    try:
        me = discord_api("/users/@me", t)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return fail("Discord rejects the bot token (it was reset or mistyped)",
                        hint="Discord developer portal, your app, Bot, Reset Token. Paste the new one when setup asks "
                             f"(delete the DISCORD_BOT_TOKEN line in {HOME / '.env'} first).")
        return warn(f"Discord answered {e.code}; try again in a minute")
    except (urllib.error.URLError, OSError, ValueError):
        return warn("could not reach Discord to test the token")
    name = me.get("username", "?")
    try:
        app = discord_api("/applications/@me", t)
        flags = int(app.get("flags", 0))
        if not flags & ((1 << 18) | (1 << 19)):
            return fail(f"bot '{name}' cannot read messages: Message Content Intent is off",
                        hint="Discord developer portal, your app, Bot: turn on Message Content Intent and save.")
    except Exception:
        pass
    return ok(f"works: bot '{name}'")


@check("access", "Discord", "Who the bot answers")
def c_access(ctx):
    p = STATE / "access.json"
    owner = KEYS.get("DISCORD_OWNER_USER_ID", "")
    if not p.exists():
        return fail("no access list yet", fix=START_FIX)
    try:
        a = json.loads(p.read_text())
    except ValueError:
        return fail(f"{p} is not valid JSON", hint=f"Delete it and run setup again: rm {p}")
    allow = a.get("allowFrom") or []
    if owner and owner not in allow:
        return fail("your Discord user ID is not on the bot's allow list",
                    hint=f"Check DISCORD_OWNER_USER_ID in {HOME / '.env'}, delete {p}, run setup again.")
    if a.get("dmPolicy") not in ("allowlist", "disabled"):
        return warn(f"the bot accepts DMs from strangers (dmPolicy={a.get('dmPolicy')})",
                    hint=f"Set \"dmPolicy\": \"allowlist\" in {p}.")
    return ok(f"only you ({len(allow)} allowed)")


# ---- Agent -----------------------------------------------------------------

@check("agent", "Agent", "Agent running")
def c_agent(ctx):
    if not which("tmux"):
        return skip("tmux not installed yet")
    if sh(["tmux", "has-session", "-t", "duke"])[0] != 0:
        if ctx.skip_human:
            return skip("not started (needs the logins first)")
        return fail("the agent is not running", fix=START_FIX)
    rc, pane = sh(["tmux", "capture-pane", "-p", "-t", "duke", "-S", "-40"])
    low = pane.lower()
    for needle, msg in (("invalid api key", "the AI login has expired"),
                        ("please run /login", "the AI login has expired"),
                        ("rate limit", "the AI plan hit its usage limit"),
                        ("usage limit", "the AI plan hit its usage limit")):
        if needle in low:
            return warn(f"running, but {msg}", hint="Watch it: tmux attach -t duke")
    return ok("running in the background (tmux session 'duke')")


@check("autostart", "Agent", "Starts after a restart")
def c_autostart(ctx):
    if IS_MAC:
        plist = Path.home() / "Library/LaunchAgents/com.blimp.start.plist"
        if plist.exists():
            log = HOME / "logs/autostart.log"
            tail = log.read_text(errors="replace")[-2000:] if log.exists() else ""
            if "Operation not permitted" in tail:
                return fail("the login item is blocked by macOS privacy settings",
                            hint="Move the Blimp folder out of Downloads, Desktop or Documents, then run setup again from there.")
            return ok("yes (login item)")
        return warn("no; after a restart run: blimp start", hint="Run setup again and say yes to starting at login.")
    return skip("on Windows, double-click START-BLIMP.bat after a restart")


# ---- runner ----------------------------------------------------------------

class Ctx:
    def __init__(self, skip_human=False):
        self.skip_human = skip_human
        self.results: dict[str, R] = {}


def run_checks(ctx: Ctx, only=None):
    for cid, group, title, fn in CHECKS:
        if only and cid not in only:
            continue
        try:
            r = fn(ctx)
        except Exception as e:  # a broken check must not hide the others
            r = warn(f"check could not run: {e.__class__.__name__}: {redact(str(e))[:120]}")
        ctx.results[cid] = r
    return ctx.results


TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM", "dumb") != "dumb"
UTF = "utf" in (os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE") or os.environ.get("LANG") or "").lower()


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if TTY else s


MARK = {"ok": c("38;5;78", "✓" if UTF else "ok"), "warn": c("38;5;214", "!"),
        "fail": c("38;5;203", "✗" if UTF else "x"), "skip": c("2", "·" if UTF else "-")}


def print_report(results):
    group = None
    for cid, g, title, _ in CHECKS:
        if cid not in results:
            continue
        r = results[cid]
        if g != group:
            group = g
            print(f"\n  {c('1', g)}")
        line = f"     {MARK[r.status]} {title}: {r.detail}" if r.status != "skip" else c("2", f"     {MARK['skip']} {title}: {r.detail}")
        print(line)
        if r.status in ("fail", "warn"):
            for f in r.fix:
                print(c("2", f"         fix: {f}"))
            if r.hint:
                print(c("2", f"         {r.hint}"))
    n = {s: sum(1 for r in results.values() if r.status == s) for s in ("ok", "warn", "fail", "skip")}
    print(f"\n  {n['ok']} ok {c('2', '·')} {n['warn']} worth a look {c('2', '·')} {n['fail']} problems"
          + (c("2", f" {'·'} {n['skip']} skipped") if n["skip"] else ""))
    return n


def apply_fixes(ctx: Ctx, assume_yes: bool) -> int:
    fixed = 0
    for cid, g, title, _ in CHECKS:
        r = ctx.results.get(cid)
        if not r or r.status not in ("fail", "warn") or not r.fix:
            continue
        print(f"\n  {c('1', title)}: {r.detail}")
        for f in r.fix:
            print(c("2", f"     $ {f}"))
        if not assume_yes:
            try:
                a = input("     Run this fix? [Y/n] ").strip().lower()
            except EOFError:
                a = "n"
            if a not in ("", "y", "yes"):
                continue
        env = dict(CLEAN_ENV, DUKE_DB_URL=db_url())
        good = True
        for f in r.fix:
            if subprocess.run(["bash", "-c", f], cwd=REPO, env=env).returncode != 0:
                good = False
                print(c("38;5;203", "     that command failed"))
                break
        if good:
            fixed += 1
    return fixed


def chat(results=None):
    """Hand over to the AI on the user's own login, with the check report as context."""
    prompt = (Path(__file__).with_name("PROMPT.md").read_text()
              .replace("{NAME}", NAME).replace("{OS}", CFG.get("BLIMP_OS", "?"))
              .replace("{REPO}", str(REPO)).replace("{HOME_DIR}", str(HOME)))
    report = ""
    if results:
        report = "\n".join(f"- {t}: {results[i].status.upper()}: {results[i].detail}"
                           + (f" (suggested fix: {'; '.join(results[i].fix)})" if results[i].fix else "")
                           for i, _, t, _ in CHECKS if i in results and results[i].status in ("fail", "warn"))
    first = ("The doctor's automatic checks found these problems:\n" + report +
             "\n\nStart with the first one: confirm the cause with one safe command, explain it to me in plain "
             "English, then fix it.") if report else "Start the check now: run `python3 blimp-infra/doctor/doctor.py --lint` and tell me what you find."
    auto = CFG.get("BLIMP_AUTO", "no") == "yes"
    if AI == "claude" and which("claude"):
        cmd = ["claude", first, "--append-system-prompt", prompt]
        if auto:
            cmd += ["--allowedTools", "Bash,Read,Edit,Write,Grep,Glob"]
        os.chdir(REPO)
        os.execvpe("claude", cmd, CLEAN_ENV)
    if AI == "codex" and which("codex"):
        flag = []
        if auto:
            h = sh(["codex", "--help"])[1]
            flag = ["--full-auto"] if "--full-auto" in h else []
        os.chdir(REPO)
        os.execvpe("codex", ["codex"] + flag + [prompt + "\n\n" + first], CLEAN_ENV)
    key = KEYS.get("OPENROUTER_API_KEY", "")
    if not key:
        print("\n  The chat needs your AI program (Claude Code or Codex) or an OpenRouter key, and neither is ready.")
        print("  Send the report above to whoever gave you Blimp.")
        return 1
    return api_chat_loop(key, prompt, first, auto)


def api_chat_loop(key, prompt, first, auto):
    model = CFG.get("BLIMP_MODEL") or {"deepseek": "deepseek/deepseek-chat"}.get(AI, "deepseek/deepseek-chat")
    tools = [{"type": "function", "function": {
        "name": "run_command", "description": "Run one shell command on the user's computer.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]
    msgs = [{"role": "system", "content": prompt}, {"role": "user", "content": first}]
    print(f"\n  {NAME} doctor chat ({model}). Type a question, or 'exit'.\n")
    while True:
        body = json.dumps({"model": model, "messages": msgs, "tools": tools}).encode()
        req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", body,
                                     {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                m = json.load(r)["choices"][0]["message"]
        except (urllib.error.URLError, KeyError, ValueError, OSError) as e:
            print(f"  Could not reach the AI: {e}")
            return 1
        msgs.append(m)
        if (m.get("content") or "").strip():
            print(m["content"], "\n")
        calls = m.get("tool_calls") or []
        for call in calls:
            try:
                cmd = json.loads(call["function"]["arguments"])["command"]
            except (ValueError, KeyError):
                msgs.append({"role": "tool", "tool_call_id": call["id"], "content": "bad arguments"})
                continue
            print("  $", cmd)
            if not auto and input("  Run this? [y/N] ").strip().lower() != "y":
                out = "The user declined to run this command."
            else:
                try:
                    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=300,
                                       cwd=REPO, env=CLEAN_ENV)
                    out = redact(p.stdout + p.stderr)[-6000:] or f"(exit {p.returncode}, no output)"
                except subprocess.TimeoutExpired:
                    out = "timed out after 300s"
            msgs.append({"role": "tool", "tool_call_id": call["id"], "content": out})
        if calls:
            continue
        try:
            u = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if u.lower() in ("exit", "quit"):
            return 0
        if u:
            msgs.append({"role": "user", "content": u})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check and fix a Blimp install.")
    ap.add_argument("--fix", action="store_true", help="apply fixes (asks first unless --yes)")
    ap.add_argument("--yes", "-y", action="store_true", help="do not ask before fixes")
    ap.add_argument("--lint", action="store_true", help="check only, change nothing")
    ap.add_argument("--json", action="store_true", help="print results as JSON (implies --lint)")
    ap.add_argument("--chat", action="store_true", help="go straight to the AI chat")
    ap.add_argument("--skip-human", action="store_true", help="skip checks that need a person")
    ap.add_argument("--only", help="comma-separated check ids")
    ap.add_argument("--quiet", action="store_true", help="print only the summary line")
    a = ap.parse_args(argv)
    if a.chat:
        return chat() or 0
    ctx = Ctx(skip_human=a.skip_human or os.environ.get("BLIMP_CI") == "1")
    only = set(a.only.split(",")) if a.only else None
    t0 = time.time()
    results = run_checks(ctx, only)
    bad = [r for r in results.values() if r.status == "fail"]
    if a.json:
        print(json.dumps({"ok": not bad, "checks": [
            {"id": i, "group": g, "title": t, "status": results[i].status, "detail": results[i].detail,
             "fix": results[i].fix, "hint": results[i].hint} for i, g, t, _ in CHECKS if i in results]}, indent=2))
        return 1 if bad else 0
    if not a.quiet:
        print(f"\n  {c('1;38;5;75', NAME + ' doctor')}  {c('2', f'checked {len(results)} things in {time.time() - t0:.1f}s')}")
    n = print_report(results) if not a.quiet else None
    if a.quiet:
        print(f"{len(results) - len(bad)}/{len(results)} checks passed")
    if a.lint or not bad:
        return 1 if bad else 0
    interactive = sys.stdin.isatty()
    fixable = any(r.fix for r in bad)
    if fixable and (a.fix or interactive):
        if a.fix and a.yes:
            go = True
        else:
            try:
                go = input("\n  Fix these now? [Y/n] ").strip().lower() in ("", "y", "yes")
            except EOFError:
                go = False
        if go:
            apply_fixes(ctx, assume_yes=a.yes)
            print(c("2", "\n  Checking again..."))
            ctx.results = {}
            results = run_checks(ctx, only)
            print_report(results)
            bad = [r for r in results.values() if r.status == "fail"]
    if bad and interactive and not a.fix:
        try:
            go = input("\n  Some problems are left. Talk them through with the AI doctor? [Y/n] ").strip().lower()
        except EOFError:
            go = "n"
        if go in ("", "y", "yes"):
            return chat(results) or (1 if bad else 0)
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(1)

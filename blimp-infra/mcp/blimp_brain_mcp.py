"""
blimp_brain_mcp.py — MCP server exposing the Blimp team brain.

The single tool contract both Duke (Claude Code) and Hermes (Hermes Agent)
use to read and write the shared memory + business data. Backed by:
- Hindsight bank `duke` (long-term semantic memory)
- WSL Postgres `duke_data` (structured business records)

Transport: stdio MCP (so any MCP-aware agent runtime can register it).

Run standalone for testing:
    /home/user/blimp/duke-venv/bin/python blimp_brain_mcp.py

Register with Hermes:
    hermes mcp add blimp-brain \
      --command /home/user/blimp/duke-venv/bin/python \
      --args /mnt/c/.../blimp-infra/mcp/blimp_brain_mcp.py

Register with Claude Code (in .mcp.json):
    {
      "mcpServers": {
        "blimp-brain": {
          "command": "/home/user/blimp/duke-venv/bin/python",
          "args": ["/mnt/c/.../blimp-infra/mcp/blimp_brain_mcp.py"]
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

# Make sibling postgres/ modules importable. The MCP server lives at
# blimp-infra/mcp/, the brain modules at blimp-infra/postgres/.
HERE = Path(__file__).resolve().parent
POSTGRES_DIR = HERE.parent / "postgres"
sys.path.insert(0, str(POSTGRES_DIR))

# Ensure duke_memory.py can find the database. The bootstrap script wired
# /usr/local/bin/duke-data with DUKE_DB_URL, but when we import duke_memory
# directly here, that env var won't be set unless the caller exports it.
# Set sane defaults that match the bootstrap.
os.environ.setdefault(
    "DUKE_DB_URL",
    "postgresql:///duke_data?host=/var/run/postgresql&port=5433",
)
os.environ.setdefault("HINDSIGHT_URL", "http://localhost:8888")
os.environ.setdefault("DUKE_BANK", "duke")

import hindsight_client as hc       # noqa: E402
import duke_memory as dm            # noqa: E402

# Brain upgrades (2026-07-13): bitemporal fact store + procedural memory live in
# this dir; contextual-enrich/trust-weighting helpers in postgres/. All additive.
sys.path.insert(0, str(HERE))
import facts as factstore          # noqa: E402
import procedural as procmem       # noqa: E402
import blackboard as boardstore    # noqa: E402
import idempotency                  # noqa: E402
import contract as task_contract    # noqa: E402
import messagelog                   # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402


mcp = FastMCP("blimp-brain")


# ---------------------------------------------------------------------
# Long-term memory (Hindsight) — duke-mem equivalent
# ---------------------------------------------------------------------

_RECENCY = re.compile(r"\b(latest|most recent(ly)?|recent(ly)?|last (thing|message|time|said|asked)|right now|currently|today|tonight|yesterday|this (week|morning|afternoon|evening)|working on|up to|lately|just now)\b", re.I)
_AGENT_CHANNELS = {"frank": "nanook", "nanook": "nanook", "duke": "duke", "hermes": "hermes"}


@mcp.tool()
def recall(query: str, limit: int = 5, tags: Optional[str] = None,
           budget: str = "mid", scope: Optional[str] = None,
           min_relevance: Optional[float] = None) -> str:
    """
    Search the team's long-term memory (Hindsight) for facts, decisions,
    conversations, doc chunks matching the query. Use this BEFORE answering
    any question where past context might exist — client names, pricing,
    "what did we decide about X", architecture choices.

    Args:
        query: Natural-language query.
        limit: Max number of results (default 5).
        tags: Optional comma-separated tag filter (e.g. "decision,pricing").
        budget: Token budget for retrieval — "low", "mid", or "high".

    Returns:
        JSON list of matching memories, each with: text, relevance (0..1
        cross-encoder score — higher = better match), rank, author + verified
        (provenance — ground-truth-check an unverified claim before acting on
        it), tags, id, metadata.
    """
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    results = hc.recall(query, limit=limit, tags=tag_list, budget=budget,
                        scope=scope, min_relevance=min_relevance)
    # "Latest / right now / last thing" questions: semantic recall has almost no sense of time (Hindsight's
    # recency boost is at most +/-10%), so on 2026-09-27 "what did the Boss last say to Frank" returned June.
    # Put the newest logged turns of the agents named (or all agents) first, newest first.
    if _RECENCY.search(query) and not tag_list:
        chans = [c for name, c in _AGENT_CHANNELS.items() if re.search(rf"\b{name}\b", query, re.I)]
        latest = []
        for c in dict.fromkeys(chans) or [None]:
            latest += hc.recent(channel=c, limit=6)
        latest = [r for r in latest if isinstance(r, dict)]
        if re.search(r"\blogan\b.{0,20}\b(said|say|asked|ask|told|tell|wrote|sent|want)", query, re.I):
            said = [r for r in latest if "speaker:logan" in (r.get("tags") or [])]
            latest = said or latest
        latest.sort(key=hc._when, reverse=True)
        uniq: list = []
        for r in latest:  # Hindsight stores a turn as several near-identical facts; show each once
            t = r.get("text") or ""
            if not any(hc._text_ratio(t, u.get("text") or "") >= 0.8 for u in uniq):
                uniq.append(r)
        latest = uniq
        seen = {r.get("id") for r in latest}
        results = latest[:8] + [r for r in results if not (isinstance(r, dict) and r.get("id") in seen)]
    # Surface provenance + relevance prominently so the agent can weight a hit,
    # ground-truth-check an unverified claim, or abstain when the top score is low.
    surfaced = []
    for r in results:
        if not isinstance(r, dict):
            surfaced.append(r); continue
        prov = hc.provenance_of(r)
        surfaced.append({
            "text": r.get("text") or r.get("content") or r.get("fact"),
            "relevance": r.get("relevance"),
            "rank": r.get("rank"),
            "author": prov["author"],
            "verified": prov["verified"],
            "scope": prov["scope"],
            "importance": prov["importance"],
            "tags": r.get("tags"),
            "id": r.get("id"),
            # When it happened: without it an agent can't tell a June fact from today's.
            "date": hc._when(r)[:16] or None,
        })
    return json.dumps(surfaced, indent=2, default=str)


@mcp.tool()
def remember(content: str, tags: Optional[str] = None,
             source: Optional[str] = None,
             context: Optional[str] = None,
             author: Optional[str] = None,
             verified: Optional[bool] = None,
             scope: Optional[str] = None) -> str:
    """
    Save a durable fact, decision, or piece of knowledge to the team's
    long-term memory. Call this *in the same turn* whenever:
    - the Boss says "remember X" or "don't forget X"
    - A real decision lands (architecture, pricing, client)
    - You discover a non-obvious fact about the codebase

    Writes are stamped with provenance (author + verified) and de-duplicated
    on write: a near/verbatim duplicate of an existing memory is reinforced
    or amended ("As of <date>: ...") instead of blindly appended.

    Args:
        content: The fact / decision / observation to save.
        tags: Comma-separated tags (e.g. "decision,architecture,gotcha").
        source: Where this came from — file path, conversation, doc.
        context: Brief framing context (e.g. "discovery call", "build review").
        author: Who is writing — defaults to this agent (env A2A_AGENT_NAME).
        verified: Whether the claim is trusted. Defaults true for the core
                  agents (duke/logan/frank), false for less-trusted sources.
    """
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    result = hc.remember(content, source=source, tags=tag_list, context=context,
                         author=author, verified=verified, scope=scope)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def log_message(channel: str, speaker: str, message: str,
                user: str = "logan") -> str:
    """
    Log a conversation turn to the shared team brain. Both Duke and Hermes
    should call this on every chat turn so the other agent can recall
    what was discussed.

    Args:
        channel: Where the conversation happened (e.g. "duke", "hermes",
                 "general", "builds").
        speaker: Who spoke — "duke", "hermes", or "logan".
        message: The message content.
        user: The owning user (default "logan").
    """
    result = hc.log_message(user, channel, speaker, message)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def who_believes(query: str, limit: int = 12) -> str:
    """
    Theory-of-mind: who on the team believes what about a topic. Returns the
    recalled claims grouped BY AUTHOR (agent), each with relevance + verified.
    Use to see every agent's position on a question before you decide.
    """
    return json.dumps(hc.who_believes(query, limit=limit), indent=2, default=str)


@mcp.tool()
def disagreements(query: str, limit: int = 16) -> str:
    """
    Theory-of-mind: surface where different agents hold CONFLICTING claims on a
    topic. Returns distinct_authors, a 'contested' flag, and the claims to
    adjudicate. Use to detect inter-agent drift/conflict (essential at scale).
    """
    return json.dumps(hc.disagreements(query, limit=limit), indent=2, default=str)


@mcp.tool()
def agent_beliefs(agent: str, query: Optional[str] = None, limit: int = 20) -> str:
    """
    Theory-of-mind: what a specific agent (duke/hermes/frank/…) has asserted —
    their 'view'. Optionally narrow with a query. Use to route ("who knows X?")
    or review one agent's positions.
    """
    return json.dumps(hc.agent_beliefs(agent, query=query, limit=limit), indent=2, default=str)


@mcp.tool()
def recent(channel: Optional[str] = None, limit: int = 20) -> str:
    """
    Get recent conversation log entries from the shared team brain. Useful
    on session start to catch up on what your teammate has been discussing
    with the Boss without him.

    Args:
        channel: Filter to a specific channel (e.g. "duke", "hermes").
                 Omit for all channels combined.
        limit: Max number of entries (default 20).
    """
    rows = hc.recent(channel=channel, limit=limit)
    return json.dumps(rows, indent=2, default=str)


@mcp.tool()
def memory_stats() -> str:
    """
    Get health + size stats for the team brain — node count, observation
    count, document count, pending consolidation. Useful for sanity checks.
    """
    try:
        stats = hc.bank_stats()
        return json.dumps(stats, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------
# Structured business data (Postgres) — duke-data equivalent
# ---------------------------------------------------------------------

@mcp.tool()
def tasks(status: Optional[str] = None,
          assigned_to: Optional[str] = None,
          limit: int = 50) -> str:
    """
    List tasks from the team's structured task table.

    Args:
        status: Filter by status — "pending", "in_progress", "completed",
                "blocked", "cancelled". Omit for all.
        assigned_to: Filter to a specific assignee (e.g. "logan", "duke",
                     "hermes").
        limit: Max rows.
    """
    rows = dm.tasks_list(status=status, assigned_to=assigned_to, limit=limit)
    return json.dumps(rows, indent=2, default=str)


@mcp.tool()
def task_add(title: str, priority: str = "P2",
             assigned_to: str = "duke",
             description: str = "") -> str:
    """
    Create a new task in the team's task table.

    Args:
        title: Task title (required).
        priority: P0 / P1 / P2 / P3. Default P2.
        assigned_to: Who owns it ("logan", "duke", "hermes"). Default "duke".
        description: Optional longer description.

    Returns:
        JSON with the new task id.
    """
    new_id = dm.task_add(title=title, priority=priority,
                         assigned_to=assigned_to, description=description)
    return json.dumps({"ok": True, "task_id": new_id})


@mcp.tool()
def task_update(task_id: int,
                status: Optional[str] = None,
                priority: Optional[str] = None,
                assigned_to: Optional[str] = None,
                description: Optional[str] = None) -> str:
    """
    Update an existing task. Only the fields you provide change.

    Args:
        task_id: The task ID to update.
        status: New status (pending / in_progress / completed / blocked / cancelled).
        priority: New priority (P0..P3).
        assigned_to: New owner.
        description: New description (replaces existing).
    """
    fields = {k: v for k, v in {
        "status": status,
        "priority": priority,
        "assigned_to": assigned_to,
        "description": description,
    }.items() if v is not None}
    dm.task_update(task_id, **fields)
    return json.dumps({"ok": True, "task_id": task_id, "updated": list(fields.keys())})


@mcp.tool()
def prospects(stage: Optional[str] = None, limit: int = 50) -> str:
    """
    List prospects (potential Blimp clients) from the team's CRM table.

    Args:
        stage: Filter — "outreach", "discovery", "quoted", "won", "lost".
        limit: Max rows.
    """
    rows = dm.prospects_list(stage=stage, limit=limit)
    return json.dumps(rows, indent=2, default=str)


@mcp.tool()
def prospect_add(company_name: str, industry: str = "", country: str = "",
                 decision_maker: Optional[str] = None,
                 contact_channel: Optional[str] = None,
                 notes: Optional[str] = None) -> str:
    """
    Add a new prospect to the CRM.

    Args:
        company_name: Company name (required).
        industry: e.g. "med-spa", "logistics", "saas".
        country: e.g. "SG", "MY", "ID", "TH", "AU", "US".
        decision_maker: Primary contact's name.
        contact_channel: How we reach them — "IG", "WhatsApp", "LinkedIn",
                         "Email", "Referral".
        notes: Free-text.

    Returns:
        JSON with the new prospect id.
    """
    extras = {k: v for k, v in {
        "decision_maker": decision_maker,
        "contact_channel": contact_channel,
        "notes": notes,
    }.items() if v is not None}
    new_id = dm.prospect_add(company_name=company_name, industry=industry,
                              country=country, **extras)
    return json.dumps({"ok": True, "prospect_id": new_id})


@mcp.tool()
def prospect_update(prospect_id: int,
                    stage: Optional[str] = None,
                    quoted_amount: Optional[float] = None,
                    quoted_currency: Optional[str] = None,
                    qualification_score: Optional[int] = None,
                    notes: Optional[str] = None) -> str:
    """
    Update a prospect's stage, quote, qualification score, or notes.

    Args:
        prospect_id: Prospect ID.
        stage: new, qualified, discovery, quoted, won, lost, parked.
        quoted_amount: Numeric quote.
        quoted_currency: SGD / MYR / USD / etc.
        qualification_score: 0-100.
        notes: Free-text (replaces existing).
    """
    fields = {k: v for k, v in {
        "stage": stage,
        "quoted_amount": quoted_amount,
        "quoted_currency": quoted_currency,
        "qualification_score": qualification_score,
        "notes": notes,
    }.items() if v is not None}
    dm.prospect_update(prospect_id, **fields)
    return json.dumps({"ok": True, "prospect_id": prospect_id, "updated": list(fields.keys())})


@mcp.tool()
def quote_log(prospect_id: int, amount: float, currency: str,
              inputs_json: str = "{}",
              reasoning: str = "",
              outcome: str = "pending",
              framework_version: str = "v1") -> str:
    """
    Log a pricing quote against a prospect.

    Args:
        prospect_id: The prospect ID.
        amount: Quoted price.
        currency: 3-letter (SGD, MYR, USD, etc.).
        inputs_json: JSON string describing what drove the quote — complexity,
                     integrations, languages, urgency. Example:
                     '{"integrations":3,"languages":["en","ms"],"urgency":"P2"}'.
        reasoning: Free-text reasoning.
        outcome: "pending", "sent", "accepted", "rejected".
        framework_version: Which pricing framework version (e.g. "v1").
    """
    try:
        inputs = json.loads(inputs_json) if inputs_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid inputs_json: {e}"})
    new_id = dm.quote_log(prospect_id=prospect_id, amount=amount,
                          currency=currency, inputs=inputs,
                          reasoning=reasoning, outcome=outcome,
                          framework_version=framework_version)
    return json.dumps({"ok": True, "quote_id": new_id})


@mcp.tool()
def quotes(prospect_id: Optional[int] = None, limit: int = 20) -> str:
    """
    List quote history. Filter by prospect or get the most recent across all.

    Args:
        prospect_id: Filter to one prospect (optional).
        limit: Max rows.
    """
    rows = dm.quotes_list(prospect_id=prospect_id, limit=limit)
    return json.dumps(rows, indent=2, default=str)


@mcp.tool()
def state_get(key: str) -> str:
    """
    Read a JSON value from the team's project_state key-value store.
    Use for ad-hoc cross-session state (current focus, in-progress builds, etc.).
    """
    val = dm.state_get(key)
    return json.dumps(val, indent=2, default=str)


@mcp.tool()
def state_set(key: str, value_json: str) -> str:
    """
    Write a JSON value to the team's project_state key-value store.

    Args:
        key: The state key.
        value_json: The value, AS A JSON STRING (will be parsed and stored).
                    Examples: '"hello"', '{"client":"X","stage":"deploy"}',
                    '[1,2,3]', 'true', '42'.
    """
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}. Wrap strings in quotes, e.g. '\"hello\"'."})
    dm.state_set(key, value)
    return json.dumps({"ok": True, "key": key})


@mcp.tool()
def activity(limit: int = 20) -> str:
    """
    Cross-table recent activity feed (tasks + prospects + quotes + state).
    Useful for "what's been happening in the business" overview.
    """
    rows = dm.recent_activity(limit=limit)
    return json.dumps(rows, indent=2, default=str)


# ---------------------------------------------------------------------
# Inter-agent comms (A2A protocol — JSON-RPC shape, Postgres-backed)
# ---------------------------------------------------------------------
import uuid
import psycopg
from psycopg.rows import dict_row
import subprocess
import httpx

SELF_AGENT = os.environ.get("A2A_AGENT_NAME", "duke")  # set by each agent's runtime
# Push delivery lives in blimp-infra/mesh/ (2026-09-26): a Postgres trigger on
# a2a_messages NOTIFYs each recipient's mesh listener, which pushes into the live
# Claude Code session (channels) or Hermes's webhook. The old tmux send-keys /
# A2A_PUSH poke is gone — every INSERT below is delivered by the mesh, once.

DUKE_DB_URL = os.environ["DUKE_DB_URL"]


def _a2a_conn():
    return psycopg.connect(DUKE_DB_URL, row_factory=dict_row)


@mcp.tool()
def a2a_send(to: str, message: str,
             skill: Optional[str] = None,
             parent_task_id: Optional[str] = None,
             idempotency_key: Optional[str] = None) -> str:
    """
    Send an A2A task to the other agent. Returns the task_id.

    Args:
        to: recipient agent name — "duke", "hermes", or "nanook" (Frank's
            wiring name; persona is Frank, agent key stays nanook).
        message: the task description / body.
        skill: optional skill name (e.g. "design-architecture", "ship-pr").
        parent_task_id: if this is a reply to an existing task, its ID.
        idempotency_key: any unique string for this send (e.g. "review-pr-18"). Calling again with
            the same key within 24 h returns the first result instead of sending twice. Use it
            whenever a retry or a resumed session might repeat the send.
    """
    return idempotency.once(SELF_AGENT, "a2a_send", idempotency_key,
                            lambda: _a2a_send(to, message, skill, parent_task_id),
                            args=[to, message, skill, parent_task_id])


def _a2a_send(to: str, message: str, skill: Optional[str], parent_task_id: Optional[str]) -> str:
    task_id = parent_task_id or str(uuid.uuid4())
    body = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message}],
                "messageId": str(uuid.uuid4()),
            },
        },
    }
    with _a2a_conn() as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO a2a_messages (task_id, parent_task_id, from_agent, to_agent,
                                       state, skill, body)
            VALUES (%s, %s, %s, %s, 'submitted', %s, %s)
            RETURNING id
        """, [task_id, parent_task_id, SELF_AGENT, to, skill, json.dumps(body)])
        row = cur.fetchone()
        c.commit()
    # Delivery: the mesh trigger pushes this row to the recipient if they are
    # online (blimp-infra/mesh/); otherwise it waits here for a2a_inbox / brief().
    return json.dumps({"ok": True, "task_id": task_id, "row_id": row["id"]})


@mcp.tool()
def messages(channel: str = "duke", speaker: str = "", limit: int = 20, newest_first: bool = True) -> str:
    """
    EXACT Discord messages, in order, from the brain's saved conversations. Use this, never recall(),
    for "the Nth/last message", "how many times did X say/ask Y", "what exactly did X write" questions:
    recall() returns summaries that merge messages and cannot count or order them. No model calls.

    Args:
        channel: Discord channel as captured: "duke", "frank", "hermes", "general", or "dm-<id>".
        speaker: only this person's/agent's messages (e.g. "Han", "the Boss", "Duke"); empty = everyone.
        limit: how many to return.
        newest_first: True = newest first (position_from_newest 1 is the latest).

    Each message carries position_from_newest / position_from_oldest. Say the limits it returns if the
    question depends on them (minute-resolution times, cut long messages).
    """
    try:
        return json.dumps(messagelog.messages(hc._get, hc._bank_path, channel, speaker, limit, newest_first),
                          default=str)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"messages failed: {e}"})


@mcp.tool()
def a2a_contract(to: str, objective: str, output: str, boundaries: str,
                 stop_condition: str, check_command: str,
                 skill: Optional[str] = None,
                 idempotency_key: Optional[str] = None) -> str:
    """
    Hand a task to a peer with the 5-field contract: what to do, what to return, what's off-limits,
    when to stop, and the command that proves it's done. Use this instead of a bare a2a_send for any
    real work hand-off; a missing field is refused, because that is how hand-offs usually fail.

    Args:
        to: "duke", "hermes" or "nanook" (Frank).
        objective: the one outcome wanted, in a sentence.
        output: the exact shape to return (e.g. "PR link + test count", "a table of X by Y").
        boundaries: what not to touch / tools not to use / budget.
        stop_condition: when to stop, including when to stop and ask instead.
        check_command: the runnable check that proves it's done (e.g. "pytest blimp-infra/hooks -q").
        skill / idempotency_key: as in a2a_send.
    """
    try:
        text = task_contract.render(objective=objective, output=output, boundaries=boundaries,
                                    stop_condition=stop_condition, check_command=check_command)
    except ValueError as e:
        return json.dumps({"ok": False, "error": str(e)})
    return a2a_send(to, text, skill=skill, idempotency_key=idempotency_key)


@mcp.tool()
def reflect(query: str, budget: str = "low") -> str:
    """
    Ask the brain to THINK over what it knows and write a reasoned answer with its sources (Hindsight
    reflect), not just return matching memories like recall(). Use for synthesis: "why did we drop
    Cohere?", "what's the full history of X?", "what do we know about Y across projects?".
    Slow (30-60 s) and it costs model tokens (one cheap brain-model call, about 100k input tokens),
    so use recall() for quick facts and never call this in a loop or on a timer.

    Args:
        query: the question.
        budget: "low" (default), "mid" or "high": how hard it searches.
    """
    try:
        resp = httpx.post(hc.HINDSIGHT_URL + hc._bank_path("/reflect"), timeout=240,  # reflect runs 30-60 s
                          json={"query": query, "budget": budget, "max_tokens": 1200})
        resp.raise_for_status()
        r = resp.json()
    except Exception as e:
        return json.dumps({"ok": False, "error": f"reflect failed: {e}"})
    return json.dumps({"ok": True, "answer": r.get("text") or r.get("answer") or "",
                       "usage": r.get("usage")}, default=str)


@mcp.tool()
def a2a_inbox(state: str = "submitted", limit: int = 10) -> str:
    """
    List incoming A2A messages addressed to this agent.

    Args:
        state: filter — "submitted" (new), "working", "input-required", "completed", or "any".
        limit: max rows.
    """
    sql = """SELECT id, task_id, parent_task_id, from_agent, state, skill,
                    body, created_at, acked_at
             FROM a2a_messages WHERE to_agent = %s"""
    params = [SELF_AGENT]
    if state and state != "any":
        sql += " AND state = %s"; params.append(state)
    sql += " ORDER BY created_at DESC LIMIT %s"; params.append(limit)
    with _a2a_conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        # Mark them as acked
        new_ids = [r["id"] for r in rows if r["acked_at"] is None]
        if new_ids:
            cur.execute(
                "UPDATE a2a_messages SET acked_at = NOW() WHERE id = ANY(%s)",
                [new_ids],
            )
            c.commit()
    return json.dumps(rows, indent=2, default=str)


@mcp.tool()
def a2a_reply(task_id: str, message: str, state: str = "working") -> str:
    """
    Reply to an A2A task. Updates the original task and creates a reply record.

    Args:
        task_id: the task you're replying to.
        message: your reply body.
        state: new state — "working" (progress), "input-required" (need more info),
               "completed" (done), "failed", "canceled".
    """
    valid = {"submitted", "working", "input-required", "completed", "failed", "canceled"}
    if state not in valid:
        return json.dumps({"error": f"invalid state {state}; pick from {sorted(valid)}"})
    body = {
        "jsonrpc": "2.0",
        "method": "task/update",
        "params": {
            "task_id": task_id,
            "state": state,
            "message": {"role": "agent", "parts": [{"kind": "text", "text": message}]},
        },
    }
    with _a2a_conn() as c, c.cursor() as cur:
        # Find the original task to know who to reply to
        cur.execute("SELECT from_agent, to_agent FROM a2a_messages "
                    "WHERE task_id = %s AND parent_task_id IS NULL LIMIT 1",
                    [task_id])
        orig = cur.fetchone()
        if not orig:
            return json.dumps({"error": f"task {task_id} not found"})
        # Reply goes from this agent back to the original sender
        cur.execute("""
            INSERT INTO a2a_messages (task_id, parent_task_id, from_agent, to_agent,
                                       state, body)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, [str(uuid.uuid4()), task_id, SELF_AGENT, orig["from_agent"], state, json.dumps(body)])
        # Update original task's state too
        cur.execute("UPDATE a2a_messages SET state = %s, updated_at = NOW() "
                    "WHERE task_id = %s AND parent_task_id IS NULL",
                    [state, task_id])
        c.commit()
    result = {"ok": True, "task_id": task_id, "state": state}
    # Non-blocking nudge toward the verification policy (blimp-infra/duke/
    # verification-policy.md): a completion with no evidence-shaped language
    # is exactly the "done" claim the policy was written against (Hermes's
    # webhook has fabricated tool calls before). This WARNS, never rejects —
    # a hard block here risks bricking a legitimate completion on a false
    # negative, which is worse than a missed nudge. The hard gate stays where
    # it belongs: the verifier subagent + the evidence artifact in the brain.
    if state == "completed" and not re.search(
        r"\bverif|\bevidence\b|\bPASS\b|exit code|test(s)? pass|ran\b.*check",
        message, re.IGNORECASE,
    ):
        result["reminder"] = (
            "No evidence-shaped language detected in this completion. Per "
            "verification-policy.md: no completion should reach the Boss "
            "without a verifier PASS stored as evidence "
            "(remember(tags='evidence,task:" + task_id + "')). If you have "
            "not run the verifier subagent yet, do that before reporting "
            "done."
        )
    return json.dumps(result)


@mcp.tool()
def a2a_complete(task_id: str, result: str) -> str:
    """
    Mark an A2A task completed with a final result.

    Args:
        task_id: the task to complete.
        result: free-text final result / artifact / answer.
    """
    return a2a_reply(task_id, result, state="completed")


@mcp.tool()
def a2a_pushback(task_id: str, reason: str, alternative: Optional[str] = None) -> str:
    """
    Push back on a task — mark it input-required with an explanation.
    Use when the brief is wrong, the approach won't work, or you need clarification.

    Args:
        task_id: task to push back on.
        reason: why this won't work as-is.
        alternative: optional — what you'd do instead.
    """
    msg = reason
    if alternative:
        msg += f"\n\nAlternative I'd propose: {alternative}"
    return a2a_reply(task_id, msg, state="input-required")


# ---------------------------------------------------------------------
# Proactive Discord send — usable from webhook/A2A sessions (MCP-only,
# no shell). Shells out to the `hermes send` CLI, which reuses the
# gateway's platform credentials and needs no running agent loop.
# ---------------------------------------------------------------------
HERMES_BIN = os.environ.get("HERMES_BIN", os.path.expanduser("~/.local/bin/hermes"))

# Friendly aliases → Discord channel IDs (Blimp guild).
_DISCORD_CHANNELS = {
    "duke":      "<ID>",
    "hermes":    "<ID>",
    "general":   "<ID>",
    "alerts":    "<ID>",   # SYSTEM > #alerts (2026-11-06): anything broken goes here
    "ops":       "<ID>",   # old name for alerts; #ops was cut 2026-10-30
}


@mcp.tool()
def send_discord(channel: str, message: str, idempotency_key: Optional[str] = None) -> str:
    """
    Post a message to a Discord channel. Works even in restricted (webhook/A2A)
    sessions that have no terminal — this is your proactive Discord-send tool.

    Args:
        channel: target channel. One of the aliases
                 (duke, hermes, general, alerts), OR a raw
                 channel ID (e.g. "<ID>"), OR "#channel-name".
        message: the text to post.

    Returns JSON with ok/true on success. On failure, ok/false plus the
    real error — do NOT claim success if ok is false.
    idempotency_key: optional unique string; a repeat call with it within 24 h returns the first
        result (idempotent_replay: true) instead of posting the message again.
    """
    return idempotency.once(SELF_AGENT, "send_discord", idempotency_key,
                            lambda: _send_discord(channel, message), args=[channel, message])


def _send_discord(channel: str, message: str) -> str:
    key = channel.lower().lstrip("#")
    target = _DISCORD_CHANNELS.get(key, channel)
    to = f"discord:{target}"
    try:
        r = subprocess.run(
            [HERMES_BIN, "send", "--to", to, "--quiet", message],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return json.dumps({"ok": True, "channel": target, "sent": message})
        return json.dumps({
            "ok": False, "exit_code": r.returncode,
            "stderr": (r.stderr or "")[-600:], "stdout": (r.stdout or "")[-600:],
        })
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------------
# Bitemporal fact store — contradiction-resolving structured memory
# (LLM extracts, CODE decides which fact is current). See facts.py.
# ---------------------------------------------------------------------

# Ensure the new tables exist at startup (idempotent; never crash the server).
for _mod in (factstore, procmem, boardstore):
    try:
        _mod.ensure_schema()
    except Exception as _e:
        print(f"[brain] {_mod.__name__} schema not ready: {_e}", file=sys.stderr)


@mcp.tool()
def fact_set(entity: str, attribute: str, value: str,
             valid_from: Optional[str] = None, verified: Optional[bool] = None,
             source: Optional[str] = None) -> str:
    """
    Assert a structured fact: entity.attribute = value (e.g. entity="client:acme",
    attribute="monthly_price", value="1500"). Use this for facts that CHANGE over
    time — prices, plans, statuses, decisions — where a stale value recalled with
    false confidence would cost money.

    The store resolves contradictions deterministically: a new value for the same
    entity+attribute SUPERSEDES the old one (old kept with history, not deleted),
    so recall never returns both. An UNVERIFIED writer cannot overwrite a VERIFIED
    fact — it's stored as 'contested' for adjudication.

    Args:
        entity: stable id, e.g. "client:acme", "prospect:42", "logan".
        attribute: the property, e.g. "monthly_price", "status", "plan".
        value: the value as text.
        valid_from: ISO datetime when this became true in the world (default now).
        verified: override trust (defaults by author).
        source: where this came from.
    """
    r = factstore.fact_set(entity, attribute, value, valid_from=valid_from,
                           author=SELF_AGENT, verified=verified, source=source)
    return json.dumps(r, default=str)


@mcp.tool()
def fact_get(entity: str, attribute: str, as_of: Optional[str] = None) -> str:
    """
    Get the CURRENT value of entity.attribute, or the value in effect AT a past
    date (as_of, ISO datetime — point-in-time "what was our quote in May?").
    Returns the resolved fact, or null if unknown. Prefer this over recall() for
    anything that changes over time.
    """
    r = factstore.fact_get(entity, attribute, as_of=as_of)
    return json.dumps(r, indent=2, default=str)


@mcp.tool()
def fact_history(entity: str, attribute: Optional[str] = None) -> str:
    """Full change history for an entity (optionally one attribute) — every value
    with its valid-time window and what superseded it."""
    return json.dumps(factstore.fact_history(entity, attribute),
                      indent=2, default=str)


@mcp.tool()
def entity_state(entity: str) -> str:
    """All CURRENT facts for an entity as a live profile (attribute -> value)."""
    return json.dumps(factstore.entity_state(entity), indent=2, default=str)


@mcp.tool()
def contested_facts(limit: int = 50) -> str:
    """Open contested facts (an unverified writer disagreed with a verified one)
    awaiting adjudication — surface these to the Boss."""
    return json.dumps(factstore.contested_facts(limit=limit),
                      indent=2, default=str)


# ---------------------------------------------------------------------
# Procedural memory — retrievable "how-to" distilled from what worked.
# See procedural.py. New procedures start as unapproved candidates.
# ---------------------------------------------------------------------

@mcp.tool()
def procedure_recall(query: str, limit: int = 3,
                     only_approved: bool = True) -> str:
    """
    At the START of a task, retrieve how we've successfully done this before —
    the reusable steps, ranked by relevance and proven success. Returns approved
    procedures by default. Check this before re-deriving a recurring workflow.
    """
    return json.dumps(procmem.procedure_recall(query, limit=limit,
                                               only_approved=only_approved),
                      indent=2, default=str)


@mcp.tool()
def procedure_add(name: str, steps: str, trigger: str = "", tags: str = "",
                  source: Optional[str] = None, approved: bool = False) -> str:
    """
    Save a how-to procedure distilled from a run that WORKED. Starts as an
    unapproved candidate (the Boss/a peer approves before it's served by default) —
    we don't let the brain silently graduate its own playbooks.

    Args:
        name: short unique name, e.g. "deploy_blimp_site".
        steps: the ordered steps that worked.
        trigger: when to use this ("deploying the portfolio site").
        tags: comma-separated topics.
    """
    return json.dumps(procmem.procedure_add(name, steps, trigger=trigger,
                                            tags=tags, source=source,
                                            author=SELF_AGENT, approved=approved),
                      default=str)


@mcp.tool()
def procedure_record_outcome(name_or_id: str, success: bool,
                             note: Optional[str] = None) -> str:
    """Record whether following a procedure worked (feeds its success/fail
    signal, ideally from the V1-V5 verification ladder)."""
    return json.dumps(procmem.procedure_record_outcome(name_or_id, success, note),
                      default=str)


@mcp.tool()
def procedure_approve(name_or_id: str, approved: bool = True) -> str:
    """Graduate a candidate procedure so it's served by default (or un-approve)."""
    return json.dumps(procmem.procedure_approve(name_or_id, approved), default=str)


@mcp.tool()
def procedure_list(approved: Optional[bool] = None, limit: int = 100) -> str:
    """List procedures (approved=True for the live playbook, False for pending
    candidates awaiting review, None for all)."""
    return json.dumps(procmem.procedure_list(approved=approved, limit=limit),
                      indent=2, default=str)


# ---------------------------------------------------------------------
# Blackboard — shared coordination board (broadcast + self-selection),
# complementing point-to-point A2A. See blackboard.py.
# ---------------------------------------------------------------------

@mcp.tool()
def board_post(title: str, body: str = "", kind: str = "task",
               tags: str = "", idempotency_key: Optional[str] = None) -> str:
    """
    Post work to the shared team board so ANY agent can self-select it (vs A2A,
    which addresses one named agent). Use for "who can verify/help with X?" —
    kind is task | question | fyi. Check board_list at session start.
    idempotency_key: optional unique string; a repeat call with it within 24 h returns the first
    post instead of creating a duplicate.
    """
    return idempotency.once(SELF_AGENT, "board_post", idempotency_key, lambda: json.dumps(
        boardstore.board_post(title, body, kind=kind, tags=tags, posted_by=SELF_AGENT), default=str),
        args=[title, body, kind, tags])


@mcp.tool()
def board_list(status: str = "open", limit: int = 50) -> str:
    """Read the shared board. status = open (unclaimed work to pick up) |
    claimed | resolved | all. Check the open board at session start."""
    return json.dumps(boardstore.board_list(
        None if status == "all" else status, limit=limit), indent=2, default=str)


@mcp.tool()
def board_claim(post_id: int) -> str:
    """Claim an open board item so others know you've got it. Race-safe — if
    someone already claimed it, you get claim_failed (don't double-work it)."""
    return json.dumps(boardstore.board_claim(post_id, claimed_by=SELF_AGENT),
                      default=str)


@mcp.tool()
def board_respond(post_id: int, resolution: str, resolve: bool = True) -> str:
    """Respond to / resolve a board item with your answer or result."""
    return json.dumps(boardstore.board_respond(post_id, resolution,
                                              resolve=resolve, by=SELF_AGENT),
                      default=str)


# ---------------------------------------------------------------------
# Crew v2 board (spec blimp-v2-spec.md §23 phase 1): the mesh board is the
# source of truth for tasks, contracts, ledgers and typed messages. These
# tools replace a2a_* and the blackboard for new work; both stay readable
# until phase 3 retires them. Identity comes from A2A_AGENT_NAME only.
# ---------------------------------------------------------------------

sys.path.insert(0, str(HERE.parent.parent))  # repo root, for blimp.mesh
try:
    from blimp.mesh.tools import board_tools as _mesh_board  # noqa: E402
    for _fn in _mesh_board.ALL:
        mcp.tool()(_fn)
except ImportError as _e:  # the board must never take the brain down with it
    print(f"[blimp-brain] mesh board tools unavailable: {_e}", file=sys.stderr)


# ---------------------------------------------------------------------
# Session briefing (2026-09-26) — one call replaces the 4-call cold start
# ---------------------------------------------------------------------

@mcp.tool()
def brief(turns: int = 8) -> str:
    """
    Everything you need at session start, in one call: who on the team is
    online, messages waiting for you, open board items, open P0/P1 tasks, and
    the team's latest conversation turns. Structured-DB reads plus one cheap
    Hindsight list — no LLM, no recall budget. Call this FIRST in a session.
    """
    out: dict[str, Any] = {"you": SELF_AGENT}
    with _a2a_conn() as c, c.cursor() as cur:
        try:
            cur.execute("""SELECT agent, transport,
                                  CASE WHEN status = 'online' AND last_seen > NOW() - INTERVAL '3 minutes'
                                       THEN 'online' ELSE 'offline' END AS status, last_seen
                           FROM agent_presence ORDER BY agent""")
            out["team"] = cur.fetchall()
        except psycopg.Error:
            c.rollback()
            out["team"] = "mesh not installed (blimp-infra/mesh/mesh.sql)"
        cur.execute("""SELECT id, task_id, from_agent, state, created_at,
                              left(body->'params'->'message'->'parts'->0->>'text', 240) AS text
                       FROM a2a_messages
                       WHERE to_agent = %s AND state IN ('submitted', 'input-required')
                         AND created_at > NOW() - INTERVAL '14 days'
                       ORDER BY created_at DESC LIMIT 10""", [SELF_AGENT])
        out["waiting_for_you"] = cur.fetchall()
        cur.execute("SELECT value FROM project_state WHERE key = 'mesh:paused'")
        row = cur.fetchone()
        out["mesh_paused"] = bool(row and row["value"] is True)
    try:
        out["board_open"] = [{k: b.get(k) for k in ("id", "title", "kind", "posted_by")}
                             for b in boardstore.board_list("open", limit=8)]
    except Exception as e:  # board table optional
        out["board_open"] = f"unavailable: {e}"
    out["tasks_open"] = [t for t in dm.tasks_list(status="pending", limit=20)
                         if str(t.get("priority", "")).upper() in ("P0", "P1")][:8]
    try:
        out["recent_turns"] = [{"text": (m.get("text") or m.get("content") or "")[:220],
                                "tags": [t for t in m.get("tags", []) if t.startswith("channel:")]}
                               for m in hc.recent(limit=turns)]
    except Exception as e:
        out["recent_turns"] = f"hindsight unavailable: {e}"
    return json.dumps(out, indent=1, default=str)


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()

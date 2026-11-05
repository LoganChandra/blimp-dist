"""Deterministic Discord renders (spec §18.1 card templates, §8.7 mirroring, `/status`).

No LLM, no randomness: the same board state always renders the same text. the Boss reads
these on his phone, so every card leads with the point and stays short.
"""
from __future__ import annotations

from datetime import timedelta, timezone
import json
from typing import Any

MYT = timezone(timedelta(hours=8), "MYT")

MAX_PREVIEW = 400  # spec 8.7: collapse bodies longer than 400 characters
STATE_LABEL = {"IN_REVIEW": "IN REVIEW", "INPUT_REQUIRED": "NEEDS INPUT", "AUTH_REQUIRED": "NEEDS APPROVAL"}
NAMES = {"duke": "Duke", "hermes": "Hermes", "frank": "Frank", "logan": "the Boss", "svc-verifier": "Verifier"}


def _name(p: str | None) -> str:
    return NAMES.get(p or "", p or "unassigned")


def _steps(ledger: dict | None) -> list[dict]:
    """Plan steps that are well-formed; anything else is ignored rather than crashing a render."""
    plan = (ledger or {}).get("plan") if isinstance(ledger, dict) else None
    return [st for st in plan if isinstance(st, dict)] if isinstance(plan, list) else []


def _checks_line(ledger: dict | None, contract: dict) -> str | None:
    total = len(contract.get("acceptance_checks") or [])
    if not total:
        return None
    green = [r for s in _steps(ledger) for r in (s.get("receipts") or []) if isinstance(r, str)]
    return f"checks {min(len(green), total)}/{total} with receipts" + (f" [{', '.join(green[:3])}]" if green else "")


def task_card(task: dict[str, Any], ledger: dict[str, Any] | None = None, budget_used_pct: int | None = None) -> str:
    """The #board card (spec 18.1). Example:
    [T-0151] Telegram intake for Become bot  ·  WORKING  ·  owner Duke  ·  R3 step pending
    Progress: 3/5 steps · checks 2/3 with receipts [RC-9001, RC-9004]
    Budget: 41% · Next: worker retry backoff · ETA: not estimated
    """
    contract = task.get("contract") or {}
    head = [f"[{task['id']}] {task['title']}", STATE_LABEL.get(task["state"], task["state"]), f"owner {_name(task.get('owner'))}"]
    if contract.get("r3_steps") and task["state"] not in ("DONE", "CANCELED", "REJECTED", "FAILED"):
        head.append("R3 step pending")
    if contract.get("legacy"):
        head.append("imported from v1, needs a contract")
    lines = ["  ·  ".join(head)]
    plan = _steps(ledger)
    prog = []
    if plan:
        done = sum(1 for s in plan if s.get("status") == "done")
        prog.append(f"Progress: {done}/{len(plan)} steps")
    cl = _checks_line(ledger, contract)
    if cl:
        prog.append(cl)
    if prog:
        lines.append(" · ".join(prog))
    tail = []
    if budget_used_pct is not None:
        tail.append(f"Budget: {budget_used_pct}%")
    nxt = (ledger or {}).get("next_step") if isinstance(ledger, dict) else None
    nxt = nxt if isinstance(nxt, str) else None
    if nxt:
        tail.append(f"Next: {nxt}")
    tail.append("ETA: " + (task["due_at"].astimezone(MYT).strftime("%d %b %H:%M MYT") if task.get("due_at") else "not estimated"))
    lines.append(" · ".join(tail))
    return "\n".join(lines)


def message_card(msg: dict[str, Any]) -> str:
    """Thread mirror of one agent message (spec 8.7): `[act] from -> to · purpose` + body, collapsed past 400 chars."""
    to = ", ".join(_name(t) for t in msg.get("recipients") or msg.get("to") or [])
    body = " ".join(p.get("text", "") for p in msg.get("parts") or [] if isinstance(p, dict)).strip()
    if len(body) > MAX_PREVIEW:
        body = body[:MAX_PREVIEW].rstrip() + f"… (+{len(body) - MAX_PREVIEW} chars, full text on the board)"
    head = f"[{msg['act']}] {_name(msg.get('sender') or msg.get('from'))} → {to} · {msg.get('purpose', '')}".rstrip(" ·")
    return f"{head}\n{body}" if body else head


def status_card(tasks: list[dict[str, Any]], limit: int = 12) -> str:
    """`/status` with no task: what is in flight, deterministically (spec 18.1). Open work first."""
    order = ["WORKING", "IN_REVIEW", "AUTH_REQUIRED", "INPUT_REQUIRED", "BLOCKED", "READY", "PROPOSED", "PARKED", "DRAFT"]
    live = [t for t in tasks if t["state"] in order]
    live.sort(key=lambda t: (not t.get("pinned"), order.index(t["state"]), -(float(t.get("priority") or 0)), t["id"]))
    if not live:
        return "Board: nothing open."
    counts: dict[str, int] = {}
    for t in live:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    head = "Board: " + ", ".join(f"{n} {STATE_LABEL.get(s, s).lower()}" for s, n in sorted(counts.items(), key=lambda kv: order.index(kv[0])))
    rows = [f"- [{t['id']}] {t['title']} · {STATE_LABEL.get(t['state'], t['state'])} · {_name(t.get('owner'))}" + (" · pinned" if t.get("pinned") else "")
            for t in live[:limit]]
    if len(live) > limit:
        rows.append(f"…and {len(live) - limit} more")
    return "\n".join([head, *rows])


def approval_card(ap: dict[str, Any]) -> str:
    """spec 18.1 approval card. Example:
    APPROVAL AP-0102 · prod_deploy <your-product>@abc123 · requested by Frank for T-0151
    Why: ships Telegram intake (client: Become, SG)
    Risk: R3 · Rollback: redeploy @9f2e1d (runbook RB-03)
    Evidence: RC-9010, RC-9012
    Reply: /approve AP-0102 · /reject AP-0102 <why>
    """
    args = ap.get("args") or {}
    action = ap["tool"].split(".", 1)[-1]
    target = args.get("app") or args.get("repo") or args.get("to") or ""
    ref = args.get("ref") or args.get("pr") or ""
    what = f"{action} {target}{'@' + str(ref) if ref else ''}".strip()
    task = f" for {ap['task_id']}" if ap.get("task_id") else ""
    lines = [f"APPROVAL {ap['id']} · {what} · requested by {_name(ap.get('requested_by'))}{task}",
             f"Why: {ap.get('why', '').strip()}",
             f"Risk: R3 · Executor: {_name(ap.get('executor'))} · Rollback: {ap.get('rollback') or 'none given'}",
             f"Exact action: {ap['tool']} {json.dumps(args, sort_keys=True, ensure_ascii=False)[:300]}"]
    ev = ap.get("evidence") or []
    lines.append("Evidence: " + (", ".join(ev) if ev else "none cited"))
    if ap.get("cost_estimate_usd") is not None:
        lines.append(f"Cost (deterministic): USD {float(ap['cost_estimate_usd']):.2f}")
    lines.append(f"Reply: /approve {ap['id']} · /reject {ap['id']} <why>")
    return "\n".join(lines)

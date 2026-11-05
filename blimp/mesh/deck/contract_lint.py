"""Blimp Crew v2 — Contract lint (spec §9.3): a task cannot be READY until it passes.

Deterministic, zero LLM. Seven checks, verbatim from the spec:
1. Acceptance checks exist, are syntactically runnable, and have concrete `expected` values.
2. risk_tier >= max tier of all listed capabilities; R3 steps enumerated.
3. Budget <= owner's remaining monthly allowance and <= per-task cap for effort class.
4. Dependencies acyclic; dependency tasks exist.
5. Task traces to an objective or mandate (or is maintenance/incident).
6. No forbidden path overlaps an allowed path.
7. client_delivery has consent scope + market, and currency matches market (SGD/MYR/USD).
"""
from __future__ import annotations

import json
import re
import shlex
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "task-contract.v2.json"

# http checks may only target hosts on this allowlist (spec 9.3 check 1). Exact host or a
# "*.domain" suffix. The canonical list is policies/egress.yaml (R4); this default mirrors it.
DEFAULT_HTTP_ALLOWLIST = ("localhost", "127.0.0.1", "yourproduct.example", "*.yourproduct.example", "blimp.my",
                          "*.blimp.my", "*.vercel.app", "github.com", "api.github.com")

_SQL_WRITE = re.compile(r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|"
                        r"comment|copy|call|do|vacuum|reindex|cluster|lock|refresh|set|reset|prepare|execute|"
                        r"listen|notify|security|pg_sleep|lo_import|lo_export|dblink)\b", re.I)

TIER_ORDER = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}

# Capability -> minimum risk tier required (from the tool catalog, spec §13.3)
CAPABILITY_MIN_TIER: dict[str, str] = {
    # board
    "board.read": "R0", "board.write": "R2", "board.ledger": "R1",
    # comms
    "comms.send": "R2", "comms.external": "R3",
    # brain
    "brain.recall": "R0", "brain.private": "R1", "brain.propose": "R2",
    # artifacts / skills
    "artifacts.read": "R0", "artifacts.write": "R1", "artifacts.share": "R2",
    "skills.read": "R0", "skills.propose": "R2",
    # repo
    "repo.read": "R0", "repo.worktree": "R1", "repo.branch": "R2", "repo.merge_main": "R3",
    # deploy
    "deploy.staging": "R2", "deploy.prod": "R3",
    # ops
    "ops.read": "R0", "ops.restart": "R2", "ops.backup": "R1",
    "ops.restore": "R3", "ops.secrets": "R3", "ops.dns": "R3",
    # web (through reader; taints)
    "web.read": "R0",
    # crm
    "crm.read": "R0", "crm.write": "R2",
    # money
    "money.calc": "R0", "money.draft": "R1", "money.send": "R3",
    # sys
    "sys.read": "R0", "sys.approval_request": "R2",
}

CHECK_KINDS = {"test", "cmd", "http", "sql_readonly", "manual_by_logan", "review"}
TASK_TYPES_REQUIRING_CHECKS_EXEMPT = {"design", "sales_support"}
TRACEABLE_TYPES_EXEMPT = {"maintenance", "incident"}
MARKET_CURRENCY = {"SG": "SGD", "MY": "MYR", "OTHER": "USD"}


@lru_cache(maxsize=1)
def _schema_validator():
    from jsonschema import Draft202012Validator

    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


def schema_errors(contract: dict[str, Any]) -> list[str]:
    """JSON Schema 2020-12 validation of the contract (spec 16.2). Each error names the field."""
    out = []
    for e in sorted(_schema_validator().iter_errors(contract), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in e.path) or "(root)"
        out.append(f"0: schema {where}: {e.message}")
    return out


def _cmd_parses(cmd: str) -> str | None:
    """None if the command is syntactically runnable by a POSIX shell, else the reason."""
    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        return f"does not parse ({e})"
    if not parts:
        return "empty"
    return None


def _host_allowed(url: str, allowlist) -> str | None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return "must be an absolute http(s) URL"
    host = u.hostname.lower()
    for pat in allowlist:
        pat = pat.lower()
        if host == pat or (pat.startswith("*.") and (host.endswith(pat[1:]) or host == pat[2:])):
            return None
    return f"host '{host}' is not on the http allowlist"


def _sql_readonly(sql: str) -> str | None:
    body = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip().rstrip(";").strip()
    if ";" in body:
        return "must be a single statement"
    if not re.match(r"^(select|with|explain|values|table|show)\b", body, re.I):
        return "must start with SELECT / WITH / EXPLAIN / VALUES / TABLE / SHOW"
    m = _SQL_WRITE.search(body)
    if m:
        return f"contains non-read-only keyword '{m.group(1)}'"
    return None


def _find_cycle(start: str, deps: list[str], edges: dict[str, list[str]]) -> list[str] | None:
    """Depth-first search for a path from any of `deps` back to `start` through `edges`
    (task_id -> the task_ids it depends on). Returns the cycle path if one exists."""
    stack = [(d, [start, d]) for d in deps]
    seen: set[str] = set()
    while stack:
        node, path = stack.pop()
        if node == start:
            return path
        if node in seen:
            continue
        seen.add(node)
        for nxt in edges.get(node, []):
            stack.append((nxt, path + [nxt]))
    return None


def _tier(caps: list[str]) -> str:
    highest = "R0"
    for c in caps:
        t = CAPABILITY_MIN_TIER.get(c, "R3")  # unknown capability -> assume highest needed tier
        if TIER_ORDER[t] > TIER_ORDER[highest]:
            highest = t
    return highest


def lint_contract(
    contract: dict[str, Any],
    *,
    owner_remaining_monthly_tokens: int | None = None,
    existing_task_ids: set[str] | None = None,
    dependency_edges: dict[str, list[str]] | None = None,
    http_allowlist=DEFAULT_HTTP_ALLOWLIST,
    validate_schema: bool = True,
) -> tuple[bool, list[str]]:
    """Return (ok, errors[]). Schema validation (check 0) plus all seven spec checks;
    empty errors = READY-eligible.

    dependency_edges: task_id -> depends_on[] for tasks already on the board, used to
    reject cycles that pass through other tasks (not just self-dependency)."""
    errors: list[str] = schema_errors(contract) if validate_schema else []
    existing_task_ids = existing_task_ids or set()
    dependency_edges = dependency_edges or {}
    effort_caps = {"S": (150_000, 6), "M": (600_000, 20), "L": (0, 0)}

    # -- 1. acceptance checks --------------------------------------------------
    checks = contract.get("acceptance_checks") or []
    ttype = contract.get("type", "")
    if not checks and ttype not in TASK_TYPES_REQUIRING_CHECKS_EXEMPT:
        errors.append("1: acceptance_checks empty and type is not design/sales_support")
    for i, c in enumerate(checks):
        kind = c.get("kind")
        if kind not in CHECK_KINDS:
            errors.append(f"1: check[{i}].kind '{kind}' not in {sorted(CHECK_KINDS)}")
            continue
        if kind == "test" or kind == "cmd":
            if not (c.get("cmd") or "").strip():
                errors.append(f"1: check[{i}] kind={kind} missing cmd")
            else:
                why = _cmd_parses(c["cmd"])
                if why:
                    errors.append(f"1: check[{i}] cmd {why}")
        elif kind == "http":
            if not (c.get("url") or "").strip():
                errors.append(f"1: check[{i}] kind=http missing url")
            else:
                why = _host_allowed(c["url"], http_allowlist)
                if why:
                    errors.append(f"1: check[{i}] url {why}")
        elif kind == "sql_readonly":
            if not (c.get("cmd") or "").strip():
                errors.append(f"1: check[{i}] kind=sql_readonly missing cmd")
            else:
                why = _sql_readonly(c["cmd"])
                if why:
                    errors.append(f"1: check[{i}] sql {why}")
        elif kind == "manual_by_logan" and not (c.get("procedure") or "").strip():
            errors.append(f"1: check[{i}] kind=manual_by_logan missing procedure")
        if not (c.get("expected") or "").strip():
            errors.append(f"1: check[{i}] missing concrete expected value")
    if any(c.get("kind") in ("test", "cmd") for c in checks):
        wt = contract.get("worktree") or ""
        if not (isinstance(wt, str) and wt.startswith("/")):
            errors.append("1: test/cmd checks need contract.worktree (absolute path of the task checkout they run in)")
    if contract.get("worktree"):
        import os as _os

        roots = [_os.path.realpath(_os.path.expanduser(r)) for r in
                 _os.environ.get("BLIMP_WORKTREE_ROOTS", _os.path.expanduser("~/blimp/worktrees")).split(":") if r.strip()]
        wt = _os.path.normpath(str(contract["worktree"]))
        tid = contract.get("id", "")
        ok_root = any(wt.startswith(r + _os.sep) and (wt[len(r) + 1:].split(_os.sep, 1)[0] == tid
                                                        or wt[len(r) + 1:].split(_os.sep, 1)[0].startswith(tid + "-")) for r in roots)
        if not ok_root:
            errors.append(f"1: worktree must be under <root>/{tid} (roots: {roots}); one checkout per task")
    if checks and ttype not in TASK_TYPES_REQUIRING_CHECKS_EXEMPT:
        non_manual = [c for c in checks if c.get("kind") != "manual_by_logan"]
        if not non_manual:
            errors.append("1: at least one non-manual acceptance check required")

    # -- 2. risk tier vs capabilities -----------------------------------------
    caps = contract.get("capabilities") or []
    needed = _tier(caps)
    declared = contract.get("risk_tier")
    declared = declared if isinstance(declared, str) else ""
    if declared not in TIER_ORDER:
        errors.append(f"2: risk_tier '{declared}' invalid")
    elif TIER_ORDER[declared] < TIER_ORDER[needed]:
        errors.append(f"2: risk_tier {declared} < capabilities need {needed}")
    if TIER_ORDER.get(declared, -1) >= 3:
        if not contract.get("r3_steps"):
            errors.append("2: risk_tier R3 requires r3_steps[] enumerated")

    # -- 3. budget fits effort class and owner allowance -----------------------
    effort = contract.get("effort_class")
    if effort not in effort_caps:
        errors.append(f"3: effort_class '{effort}' invalid")
    else:
        budget = contract.get("budget") or {}
        cap_tokens, cap_turns = effort_caps[effort]
        if cap_tokens and int(budget.get("tokens", 0)) > cap_tokens:
            errors.append(f"3: budget.tokens {budget.get('tokens')} > effort {effort} cap {cap_tokens}")
        if cap_turns and int(budget.get("turns", 0)) > cap_turns:
            errors.append(f"3: budget.turns {budget.get('turns')} > effort {effort} cap {cap_turns}")
        if owner_remaining_monthly_tokens is not None:
            if int(budget.get("tokens", 0)) > owner_remaining_monthly_tokens:
                errors.append("3: budget.tokens exceed owner remaining monthly allowance")

    # -- 4. dependencies exist and are acyclic ---------------------------------
    deps = contract.get("depends_on") or []
    for d in deps:
        if d not in existing_task_ids and d != contract.get("id"):
            errors.append(f"4: dependency {d} does not exist")
    if contract.get("id") in deps:
        errors.append(f"4: task {contract.get('id')} depends on itself")
    elif contract.get("id") and deps:
        cycle = _find_cycle(contract["id"], deps, dependency_edges)
        if cycle:
            errors.append("4: dependency cycle " + " -> ".join(cycle))

    # -- 5. traceability ---------------------------------------------------------
    why = contract.get("why") or {}
    if ttype not in TRACEABLE_TYPES_EXEMPT:
        if not (why.get("objective_id") or why.get("mandate_id")):
            errors.append("5: task must trace to an objective or mandate (or be maintenance/incident)")

    # -- 6. forbidden vs allowed paths ------------------------------------------
    bounds = contract.get("boundaries") or {}
    forbidden = [f.strip("/") for f in bounds.get("forbidden", [])]
    allowed = [a.strip("/") for a in bounds.get("allowed_paths", [])]
    for f in forbidden:
        for a in allowed:
            if f and a and (a == f or a.startswith(f + "/") or f.startswith(a + "/")):
                errors.append(f"6: forbidden path '{f}' overlaps allowed path '{a}'")

    # -- 7. client delivery ------------------------------------------------------
    if ttype == "client_delivery":
        client = contract.get("client") or {}
        if not client.get("client_id"):
            errors.append("7: client_delivery missing client_id")
        if not client.get("consent_scope"):
            errors.append("7: client_delivery missing consent_scope")
        market = client.get("market")
        if market not in MARKET_CURRENCY:
            errors.append(f"7: client market '{market}' invalid (SG/MY/OTHER)")
        cur = client.get("currency")
        if market in MARKET_CURRENCY and cur is not None and cur != MARKET_CURRENCY[market]:
            errors.append(f"7: currency {cur} does not match market {market} (must be {MARKET_CURRENCY[market]})")

    return (len(errors) == 0), errors

"""
duke_memory.py — Duke's business-data CLI.

NOTE: Long-term memory (facts, conversations, semantic recall) is handled by
Hindsight (Docker container at http://localhost:8765). Call those via
`duke-mem <cmd>` (see hindsight_client.py).

This CLI handles STRUCTURED business data only: tasks, prospects, pricing
quotes, project state. Hindsight isn't built for these — Postgres is.

Usage:
    duke-data task-add --title "Send pricing to X"
    duke-data tasks --status in_progress
    duke-data task-update 5 --status completed
    duke-data prospect-add --company "X Med Spa" --industry "aesthetic" --country SG
    duke-data prospects --stage discovery
    duke-data quote-log --prospect 3 --amount 8000 --currency SGD --reasoning "..."
    duke-data state-set current_focus '{"client":"X","action":"sending quote"}'
    duke-data state-get current_focus
    duke-data activity                # recent activity view
"""

import argparse
import json
import os
import sys
from typing import Optional

import psycopg
from psycopg.rows import dict_row


def db():
    url = os.getenv("DUKE_DB_URL", "postgresql://postgres@localhost:5432/duke_data")
    return psycopg.connect(url, row_factory=dict_row)


# -------------------------- tasks --------------------------

def task_add(title: str, description: str = "", priority: str = "P2",
             assigned_to: str = "duke", parent_id: Optional[int] = None) -> int:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO tasks (title, description, priority, assigned_to, parent_id)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, [title, description, priority, assigned_to, parent_id])
        row = cur.fetchone()
        conn.commit()
        return row["id"]


def task_update(task_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = NOW()"
    if "status" in fields and fields["status"] == "completed":
        sets += ", completed_at = NOW()"
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE tasks SET {sets} WHERE id = %s",
                    list(fields.values()) + [task_id])
        conn.commit()


def tasks_list(status: Optional[str] = None, assigned_to: Optional[str] = None,
               limit: int = 50) -> list[dict]:
    sql = "SELECT id, title, status, priority, assigned_to, created_at FROM tasks WHERE 1=1"
    params = []
    if status:
        sql += " AND status = %s"; params.append(status)
    if assigned_to:
        sql += " AND assigned_to = %s"; params.append(assigned_to)
    sql += " ORDER BY priority, created_at DESC LIMIT %s"; params.append(limit)
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# -------------------------- prospects --------------------------

def prospect_add(company_name: str, industry: str = "", country: str = "",
                 stage: str = "new", **kw) -> int:
    fields = {"company_name": company_name, "industry": industry, "country": country, "stage": stage}
    fields.update({k: v for k, v in kw.items() if v is not None})
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["%s"] * len(fields))
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"INSERT INTO prospects ({cols}) VALUES ({placeholders}) RETURNING id",
                    list(fields.values()))
        row = cur.fetchone()
        conn.commit()
        return row["id"]


def prospect_update(pid: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = NOW()"
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE prospects SET {sets} WHERE id = %s",
                    list(fields.values()) + [pid])
        conn.commit()


def prospects_list(stage: Optional[str] = None, industry: Optional[str] = None,
                   limit: int = 50) -> list[dict]:
    sql = "SELECT id, company_name, industry, country, stage, qualification_score, quoted_amount, quoted_currency FROM prospects WHERE 1=1"
    params = []
    if stage:    sql += " AND stage = %s"; params.append(stage)
    if industry: sql += " AND industry = %s"; params.append(industry)
    sql += " ORDER BY updated_at DESC LIMIT %s"; params.append(limit)
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# -------------------------- pricing log --------------------------

def quote_log(prospect_id: Optional[int], amount: float, currency: str,
              inputs: dict, reasoning: str = "", outcome: str = "pending",
              framework_version: str = "v1") -> int:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pricing_log
              (prospect_id, quote_amount, quote_currency, inputs, reasoning, outcome, framework_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, [prospect_id, amount, currency, json.dumps(inputs), reasoning, outcome, framework_version])
        row = cur.fetchone()
        conn.commit()
        return row["id"]


def quotes_list(prospect_id: Optional[int] = None, limit: int = 20) -> list[dict]:
    sql = "SELECT id, prospect_id, quote_amount, quote_currency, outcome, created_at FROM pricing_log"
    params = []
    if prospect_id:
        sql += " WHERE prospect_id = %s"; params.append(prospect_id)
    sql += " ORDER BY created_at DESC LIMIT %s"; params.append(limit)
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# -------------------------- project_state --------------------------

def state_get(key: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM project_state WHERE key = %s", [key])
        row = cur.fetchone()
        return row["value"] if row else None


def state_set(key: str, value):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO project_state (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, [key, json.dumps(value)])
        conn.commit()


# -------------------------- activity view --------------------------

def recent_activity(limit: int = 50) -> list[dict]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM recent_activity LIMIT %s", [limit])
        return cur.fetchall()


# -------------------------- CLI --------------------------

def main():
    parser = argparse.ArgumentParser(prog="duke-data", description="Duke's structured business data")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # tasks
    p = sub.add_parser("task-add"); p.add_argument("--title", required=True); p.add_argument("--description", default=""); p.add_argument("--priority", default="P2"); p.add_argument("--assigned-to", default="duke")
    p = sub.add_parser("task-update"); p.add_argument("id", type=int); p.add_argument("--status"); p.add_argument("--priority"); p.add_argument("--assigned-to"); p.add_argument("--description")
    p = sub.add_parser("tasks"); p.add_argument("--status"); p.add_argument("--assigned-to"); p.add_argument("--limit", type=int, default=50)

    # prospects
    p = sub.add_parser("prospect-add")
    p.add_argument("--company", required=True); p.add_argument("--industry", default=""); p.add_argument("--country", default="")
    p.add_argument("--decision-maker"); p.add_argument("--contact-channel"); p.add_argument("--team-size", type=int)
    p.add_argument("--revenue-est", type=float); p.add_argument("--revenue-currency"); p.add_argument("--stage", default="new")
    p.add_argument("--notes")
    p = sub.add_parser("prospect-update"); p.add_argument("id", type=int); p.add_argument("--stage"); p.add_argument("--quoted-amount", type=float); p.add_argument("--quoted-currency"); p.add_argument("--qualification-score", type=int); p.add_argument("--notes")
    p = sub.add_parser("prospects"); p.add_argument("--stage"); p.add_argument("--industry"); p.add_argument("--limit", type=int, default=50)

    # pricing
    p = sub.add_parser("quote-log")
    p.add_argument("--prospect", type=int); p.add_argument("--amount", type=float, required=True); p.add_argument("--currency", required=True)
    p.add_argument("--inputs", default="{}"); p.add_argument("--reasoning", default=""); p.add_argument("--outcome", default="pending"); p.add_argument("--framework-version", default="v1")
    p = sub.add_parser("quotes"); p.add_argument("--prospect", type=int); p.add_argument("--limit", type=int, default=20)

    # state
    p = sub.add_parser("state-get"); p.add_argument("key")
    p = sub.add_parser("state-set"); p.add_argument("key"); p.add_argument("value")

    # activity
    p = sub.add_parser("activity"); p.add_argument("--limit", type=int, default=50)

    args = parser.parse_args()

    if args.cmd == "task-add":
        print(f"Task #{task_add(args.title, args.description, args.priority, args.assigned_to)}")
    elif args.cmd == "task-update":
        fields = {k: v for k, v in vars(args).items() if v is not None and k not in ("cmd", "id")}
        task_update(args.id, **fields); print(f"Task #{args.id} updated")
    elif args.cmd == "tasks":
        for r in tasks_list(args.status, args.assigned_to, args.limit):
            print(f"#{r['id']} [{r['priority']}] {r['status']:12} {r['title']}")

    elif args.cmd == "prospect-add":
        # Convert --revenue-est etc to dict keys matching schema (kebab → snake)
        kw = {
            "decision_maker": args.decision_maker,
            "contact_channel": args.contact_channel,
            "team_size": args.team_size,
            "revenue_est": args.revenue_est,
            "revenue_currency": args.revenue_currency,
            "notes": args.notes,
        }
        kw = {k: v for k, v in kw.items() if v is not None}
        print(f"Prospect #{prospect_add(args.company, args.industry, args.country, args.stage, **kw)}")
    elif args.cmd == "prospect-update":
        fields = {k.replace("_", "_"): v for k, v in vars(args).items() if v is not None and k not in ("cmd", "id")}
        # rename a couple kebab→snake
        if "quoted_amount" in fields: pass
        prospect_update(args.id, **fields); print(f"Prospect #{args.id} updated")
    elif args.cmd == "prospects":
        for r in prospects_list(args.stage, args.industry, args.limit):
            qa = f"{r['quoted_currency']} {r['quoted_amount']}" if r['quoted_amount'] else "-"
            print(f"#{r['id']} [{r['stage']:10}] {r['country']:5} {r['industry']:20} {r['company_name']:30} q:{qa}")

    elif args.cmd == "quote-log":
        try:
            inputs = json.loads(args.inputs)
        except json.JSONDecodeError:
            inputs = {"raw": args.inputs}
        print(f"Quote #{quote_log(args.prospect, args.amount, args.currency, inputs, args.reasoning, args.outcome, args.framework_version)}")
    elif args.cmd == "quotes":
        for r in quotes_list(args.prospect, args.limit):
            print(f"#{r['id']} prospect:{r['prospect_id']} {r['quote_currency']} {r['quote_amount']} [{r['outcome']}]")

    elif args.cmd == "state-get":
        val = state_get(args.key)
        print(json.dumps(val, indent=2) if val is not None else "(not set)")
    elif args.cmd == "state-set":
        state_set(args.key, json.loads(args.value)); print(f"State '{args.key}' updated")

    elif args.cmd == "activity":
        for r in recent_activity(args.limit):
            print(f"[{r['at']:%Y-%m-%d %H:%M}] {r['kind']:8} {r['context']:12} {r['summary']}")


if __name__ == "__main__":
    main()

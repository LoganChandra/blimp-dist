#!/usr/bin/env python3
"""
forgetting_gc.py — find FORGETTABLE memories (old + low-importance + low-link).

REPORT-ONLY by design. The 2026-06-23 brain-wipe incident is exactly why this
tool NEVER deletes: it produces a review list of decay candidates (oldest,
least-connected, not flagged important) for a human/agent to act on. The
forgetting policy is "decay, then review, then maybe archive" — never an
unattended DELETE against the live bank.

Run: /home/user/blimp/duke-venv/bin/python blimp-infra/brain-maint/forgetting_gc.py
     [--container duke-hindsight] [--days 120] [--limit 30]
"""
import subprocess, sys, argparse


def psql(container, sql):
    pgb = "/home/hindsight/.pg0/installation/18.1.0/bin"
    base = ["exec", "-e", "PGPASSWORD=hindsight", container,
            f"{pgb}/psql", "-h", "127.0.0.1", "-U", "hindsight", "-d", "hindsight",
            "-tAF", "\t", "-c", sql]
    for prefix in (["docker"], ["sudo", "docker"]):
        try:
            r = subprocess.run(prefix + base, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return r.stdout
        except Exception:
            continue
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="duke-hindsight")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--limit", type=int, default=30)
    a = ap.parse_args()

    # READ-ONLY: oldest + least-connected memory_units. Connectivity (link count)
    # is the salience proxy — a fact nothing else points to is the safest to forget.
    sql = f"""
    select mu.id,
           left(regexp_replace(mu.text, '\\s+', ' ', 'g'), 60),
           mu.mentioned_at::date,
           (select count(*) from memory_links ml
              where ml.from_id = mu.id or ml.to_id = mu.id) as links
    from memory_units mu
    where mu.mentioned_at < now() - interval '{a.days} days'
    order by links asc, mu.mentioned_at asc
    limit {a.limit};
    """
    out = psql(a.container, sql).strip()
    rows = [r for r in out.split("\n") if r]
    print(f"FORGETTABLE candidates in '{a.container}' (>{a.days}d old, fewest links) — REPORT ONLY:\n")
    print(f"  {'links':>5}  {'last-seen':<11}  text")
    for r in rows:
        parts = r.split("\t")
        if len(parts) >= 4:
            _id, text, seen, links = parts[0], parts[1], parts[2], parts[3]
            print(f"  {links:>5}  {seen:<11}  {text}")
    print(f"\n{len(rows)} review candidates. NOTHING deleted — this is a decay/review list by design.")
    print("Forgetting policy: review these, archive the truly-dead by hand (with a snapshot first).")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""write_check.py: is the brain still turning saves into facts? Plain SQL, no model calls, zero tokens.

The research behind it (2026-11-06): a save can "succeed" with zero facts and no error (Hindsight
#6295), and no benchmark or our read-side exam would notice. This checks the last 24 h of the live
bank and prints one line; with --alert it posts to #alerts only when something looks wrong.

  write_check.py            print the numbers
  write_check.py --alert    also post to #alerts if anything is off (silent when healthy)

What counts as wrong:
  - a long save (>= 800 chars) that produced no facts (short ones are greetings, rightly skipped);
  - any failed operation or failed consolidation in the window;
  - consolidation backlog that isn't draining (pending > 2000).
"""
import json
import subprocess
import sys

C = "duke-hindsight"
BANK = "duke"
ALERTS = "discord:<ID>"   # SYSTEM > #alerts


def q(sql: str) -> list[list[str]]:
    b = subprocess.run(["docker", "exec", C, "sh", "-c", "ls -d /home/hindsight/.pg0/installation/*/bin | head -1"],
                       capture_output=True, text=True, timeout=30).stdout.strip()
    out = subprocess.run(["docker", "exec", "-e", "PGPASSWORD=hindsight", C, f"{b}/psql", "-h", "/tmp", "-p", "5432",
                          "-U", "hindsight", "-d", "hindsight", "-tAF", "\t", "-c", sql],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def main() -> int:
    w = "now() - interval '24 hours'"
    docs, = q(f"select count(*) from documents where bank_id='{BANK}' and created_at > {w}")[0]
    empty = q(f"""select d.id, length(d.original_text) from documents d where d.bank_id='{BANK}' and d.created_at > {w}
                  and length(d.original_text) >= 800 and not exists
                  (select 1 from memory_units m where m.bank_id=d.bank_id and m.document_id=d.id)""")
    facts, = q(f"select count(*) from memory_units where bank_id='{BANK}' and created_at > {w} and fact_type<>'observation'")[0]
    failed, = q(f"select count(*) from async_operations where bank_id='{BANK}' and status='failed' and updated_at > {w}")[0]
    cfail, = q(f"select count(*) from memory_units where bank_id='{BANK}' and consolidation_failed_at > {w}")[0]
    pending, = q(f"select count(*) from memory_units where bank_id='{BANK}' and fact_type<>'observation' and consolidated_at is null and consolidation_failed_at is null")[0]
    res = {"saves_24h": int(docs), "facts_24h": int(facts), "long_saves_no_facts": len(empty),
           "failed_ops_24h": int(failed), "failed_consolidation_24h": int(cfail), "pending_consolidation": int(pending)}
    problems = []
    if empty:
        problems.append(f"{len(empty)} long save(s) produced no facts (e.g. {empty[0][0]})")
    if int(failed):
        problems.append(f"{failed} failed operation(s)")
    if int(cfail):
        problems.append(f"{cfail} failed consolidation(s)")
    if int(pending) > 2000:
        problems.append(f"consolidation backlog {pending}")
    print(json.dumps(res), "OK" if not problems else "PROBLEMS: " + "; ".join(problems))
    if problems and "--alert" in sys.argv:
        msg = ("Brain write check: " + "; ".join(problems) +
               f". Last 24 h: {docs} saves, {facts} facts. Details: python3 blimp-infra/brain-maint/write_check.py")
        subprocess.run(["hermes", "send", "--to", ALERTS, "--quiet", msg], timeout=60)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

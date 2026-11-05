---
topic: duke-memory-stack
audience: Duke + the Boss + any future agent runtime
last_updated: 2026-05-27
---

# Duke's Brain — Hindsight + Postgres

Two-layer agent-agnostic substrate. Any runtime (Claude Code, Hermes Agent,
Aider) reads/writes the same brain via two CLIs:

- **`duke-mem`** — long-term semantic memory, conversations, doc retrieval.
  Backed by **Hindsight** (MIT-licensed, 91.4% LongMemEval, 4-strategy
  retrieval: semantic + BM25 + graph + temporal).
- **`duke-data`** — structured business data: tasks, prospects, pricing_log,
  project_state. Backed by our own **Postgres + pgvector** database.

Two layers because:
- Hindsight is built for *unstructured* semantic memory (facts, decisions,
  doc chunks, conversational history). It's excellent at it.
- Business data (a task with status field, a prospect with stage, a quote
  with amount + currency) is structured records — Hindsight isn't the right
  shape. Postgres tables are.

Both live on the Boss's local machine. Same backup story (pg_dump + Hindsight's
data volume).

## CLI

### `duke-mem` (long-term memory via Hindsight)
```bash
duke-mem recall "<query>" [--limit 5] [--strategies semantic,bm25,graph,temporal]
duke-mem remember --content "<text>" [--source "<path>"] [--tags tag1,tag2]
duke-mem log --user logan --channel other-agent --speaker duke --message "..."
duke-mem recent --channel other-agent --limit 20
duke-mem ingest <file.md>                # ingest one file
duke-mem ingest-repo                     # walk whole repo, ingest all docs
duke-mem status                          # is Hindsight up?
```

### `duke-data` (structured business records via Postgres)
```bash
duke-data task-add --title "..." [--priority P1] [--assigned-to duke]
duke-data task-update <id> --status completed
duke-data tasks [--status in_progress]

duke-data prospect-add --company "..." --industry "..." --country SG
duke-data prospect-update <id> --stage quoted --quoted-amount 8000 --quoted-currency SGD
duke-data prospects [--stage discovery]

duke-data quote-log --prospect <id> --amount 8000 --currency SGD --reasoning "..."
duke-data quotes [--prospect <id>]

duke-data state-set <key> '<json>'
duke-data state-get <key>

duke-data activity        # cross-table recent activity view
```

## How agents use it

### Claude Code (current Duke)
Calls `duke-mem` / `duke-data` via the Bash tool. Output is plain text.

### Hermes Agent (future)
Uses Hindsight's native plugin for memory. Calls `duke-data` via shell.

### Aider / others
HTTP calls to Hindsight API directly, or shell to `duke-mem`. Same shared
substrate either way.

## Bootstrap (one-time)

Double-click `BOOTSTRAP-DUKE-MEMORY.bat` from Windows, or in WSL:
```bash
bash blimp-infra/postgres/bootstrap-duke-memory.sh
```

The script:
1. Installs Postgres 16 + pgvector + Python venv + Docker
2. Creates `duke_data` database, applies business-data schema
3. Pulls + starts Hindsight container on port 8765 (data in `~/blimp/hindsight-data/`)
4. Installs `duke-mem` and `duke-data` as system-wide commands
5. Ingests all repo docs into Hindsight

Idempotent — re-running is safe.

## Re-ingesting docs after updates

```bash
duke-mem ingest-repo
```

Hindsight handles dedup on content hash; only new/changed chunks get
re-embedded.

## Backup

### Postgres business data
```bash
pg_dump duke_data > ~/blimp/backups/duke_data-$(date +%F).sql
```

### Hindsight memory
Hindsight stores everything in its data volume at `~/blimp/hindsight-data/`.
Back it up with rsync or tar:
```bash
tar czf ~/blimp/backups/hindsight-$(date +%F).tar.gz -C ~/blimp hindsight-data
```

Recommended cron (3am daily, both):
```cron
0 3 * * * pg_dump duke_data | gzip > ~/blimp/backups/duke_data-$(date +\%F).sql.gz && tar czf ~/blimp/backups/hindsight-$(date +\%F).tar.gz -C ~/blimp hindsight-data
```

## Cloud migration path

**Postgres**: Supabase free tier (Postgres + pgvector built-in). Swap
`DUKE_DB_URL` in `/usr/local/bin/duke-data` and re-run `pg_dump | psql <cloud>`.

**Hindsight**: deploy the same Docker image on Hetzner / DigitalOcean / Fly.
Swap `HINDSIGHT_URL` in `/usr/local/bin/duke-mem`. Restore data volume from
your tarball.

## Files in this directory

- `schema.sql` — Postgres DDL for business data tables
- `duke_memory.py` — `duke-data` CLI implementation
- `hindsight_client.py` — `duke-mem` HTTP wrapper around Hindsight
- `requirements.txt` — Python deps (psycopg, httpx, pgvector)
- `bootstrap-duke-memory.sh` — one-shot installer (WSL)
- `README.md` — this file

## Why Hindsight, not Mem0 or rolling our own

- **Quality**: 91.4% on LongMemEval vs Mem0's 49% and DIY's untested
- **Runtime-portable**: HTTP API → callable from any language/agent
- **Self-hosted**: MIT-licensed Docker container, no external dependencies
- **4-strategy retrieval**: semantic + BM25 + graph + temporal in parallel
- **Native Hermes Agent plugin**: when we migrate runtimes, zero memory loss

See chat history (May 27, 2026) for the architecture decision discussion.

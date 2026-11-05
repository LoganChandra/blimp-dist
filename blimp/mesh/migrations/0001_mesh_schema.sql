-- Blimp Crew v2 — mesh schema (migration 0001)
-- Lives inside the existing duke_data database (:5433). v1 tables remain until migration.
-- Spec: blimp-v2-spec.md §15 (verbatim DDL, lightly completed: indexes and constraints as specified).

CREATE SCHEMA IF NOT EXISTS mesh;

-- Principals: humans and agents
CREATE TABLE IF NOT EXISTS mesh.principals (
  id            text PRIMARY KEY,                 -- 'logan','cofounder','duke','hermes','frank','svc-verifier',...
  kind          text NOT NULL CHECK (kind IN ('human','agent','service')),
  display_name  text NOT NULL,
  discord_user_id text,
  wiring_name   text,                             -- e.g. 'nanook' for frank
  authority     jsonb NOT NULL DEFAULT '{}'::jsonb,
  active        boolean NOT NULL DEFAULT true
);

-- Append-only, hash-chained event log
CREATE TABLE IF NOT EXISTS mesh.events (
  id            bigserial PRIMARY KEY,
  ts            timestamptz NOT NULL DEFAULT now(),
  actor         text NOT NULL REFERENCES mesh.principals(id),
  type          text NOT NULL,                    -- see spec 16.9 catalog
  subject_type  text,                             -- task, message, huddle, approval, ...
  subject_id    text,
  payload       jsonb NOT NULL,
  causation_id  bigint,                           -- event that caused this one
  correlation_id text,                            -- e.g. task id or workflow id
  prev_hash     bytea,
  hash          bytea NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_subject ON mesh.events (subject_type, subject_id, id);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON mesh.events (type, ts);

-- Enforce append-only
CREATE OR REPLACE RULE events_no_update AS ON UPDATE TO mesh.events DO INSTEAD NOTHING;
CREATE OR REPLACE RULE events_no_delete AS ON DELETE TO mesh.events DO INSTEAD NOTHING;

-- the Boss's words, verbatim, append-only (C5)
CREATE TABLE IF NOT EXISTS mesh.mandates (
  id            text PRIMARY KEY,                 -- 'M-0001'
  received_at   timestamptz NOT NULL,
  source        text NOT NULL,                    -- discord message id / channel
  verbatim      text NOT NULL,
  sha256        bytea NOT NULL,
  tags          text[] NOT NULL DEFAULT '{}'
);

-- Measurable outcomes the crew traces work to
CREATE TABLE IF NOT EXISTS mesh.objectives (
  id            text PRIMARY KEY,                 -- 'O-2026Q4-01'
  title         text NOT NULL,
  metric        jsonb NOT NULL,                   -- {name, target, unit, source_query}
  mandate_ids   text[] NOT NULL DEFAULT '{}',
  status        text NOT NULL DEFAULT 'active',
  period        daterange NOT NULL
);

-- Tasks: one owner, one contract, one thread
CREATE TABLE IF NOT EXISTS mesh.tasks (
  id            text PRIMARY KEY,                 -- 'T-0142'
  title         text NOT NULL,
  type          text NOT NULL,
  state         text NOT NULL,                    -- DRAFT|PROPOSED|READY|WORKING|INPUT_REQUIRED|AUTH_REQUIRED|IN_REVIEW|BLOCKED|PARKED|DONE|FAILED|CANCELED|REJECTED
  substate      text,
  owner         text REFERENCES mesh.principals(id),
  reviewer      text REFERENCES mesh.principals(id),
  collaborators text[] NOT NULL DEFAULT '{}',
  objective_id  text REFERENCES mesh.objectives(id),
  mandate_id    text REFERENCES mesh.mandates(id),
  parent_id     text REFERENCES mesh.tasks(id),
  context_id    text NOT NULL UNIQUE,             -- A2A contextId
  client_id     text,
  risk_tier     text NOT NULL CHECK (risk_tier IN ('R0','R1','R2','R3')),
  effort_class  text NOT NULL CHECK (effort_class IN ('S','M','L')),
  priority      numeric NOT NULL DEFAULT 0,
  pinned        boolean NOT NULL DEFAULT false,
  contract      jsonb NOT NULL,                   -- spec 16.2
  contract_frozen_at timestamptz,
  budget        jsonb NOT NULL,
  spent         jsonb NOT NULL DEFAULT '{"tokens":0,"turns":0}'::jsonb,
  lease_holder  text,
  lease_expires_at timestamptz,
  due_at        timestamptz,
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  version       integer NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_tasks_owner_state ON mesh.tasks (owner, state, priority DESC);

CREATE TABLE IF NOT EXISTS mesh.task_edges (
  from_id text REFERENCES mesh.tasks(id),
  to_id   text REFERENCES mesh.tasks(id),
  kind    text NOT NULL CHECK (kind IN ('blocks','relates','subtask')),
  PRIMARY KEY (from_id, to_id, kind)
);

-- Per-task progress ledger (the resume point)
CREATE TABLE IF NOT EXISTS mesh.ledgers (
  task_id    text PRIMARY KEY REFERENCES mesh.tasks(id),
  version    integer NOT NULL,
  body       jsonb NOT NULL,                      -- spec 16.3
  updated_by text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
-- Versioned history; PK is (task_id, version) — LIKE copies ledgers' task_id-only PK, so fix it.
CREATE TABLE IF NOT EXISTS mesh.ledger_history (LIKE mesh.ledgers INCLUDING ALL);
DO $$ BEGIN
  ALTER TABLE mesh.ledger_history DROP CONSTRAINT IF EXISTS ledger_history_pkey;
  ALTER TABLE mesh.ledger_history ADD PRIMARY KEY (task_id, version);
END $$;

-- Threads: one context, one Discord thread
CREATE TABLE IF NOT EXISTS mesh.threads (
  context_id        text PRIMARY KEY,
  kind              text NOT NULL CHECK (kind IN ('task','huddle','incident','dm')),
  task_id           text REFERENCES mesh.tasks(id),
  discord_thread_id text,
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- A2A v1.0 messages with Blimp extensions
CREATE TABLE IF NOT EXISTS mesh.messages (
  id          text PRIMARY KEY,                   -- A2A messageId (ULID)
  context_id  text NOT NULL REFERENCES mesh.threads(context_id),
  task_id     text REFERENCES mesh.tasks(id),
  sender      text NOT NULL REFERENCES mesh.principals(id),
  recipients  text[] NOT NULL,
  cc          text[] NOT NULL DEFAULT '{}',
  act         text NOT NULL,
  expects     text NOT NULL DEFAULT 'none',
  priority    text NOT NULL DEFAULT 'P2',
  reply_by    timestamptz,
  purpose     text NOT NULL,
  parts       jsonb NOT NULL,                     -- A2A parts
  refs        jsonb NOT NULL DEFAULT '{}'::jsonb, -- decisions, artifacts, receipts, tasks
  tainted     boolean NOT NULL DEFAULT false,
  visibility  text NOT NULL DEFAULT 'thread',
  created_at  timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  read_at     timestamptz,
  answered_by text REFERENCES mesh.messages(id)
);
CREATE INDEX IF NOT EXISTS idx_messages_ctx ON mesh.messages (context_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_unread ON mesh.messages (recipients) WHERE read_at IS NULL;

-- Content-addressed artifacts
CREATE TABLE IF NOT EXISTS mesh.artifacts (
  id         text PRIMARY KEY,                    -- 'AR-0311'
  task_id    text REFERENCES mesh.tasks(id),
  name       text NOT NULL,
  media_type text NOT NULL,
  uri        text NOT NULL,                       -- cas://sha256/... or git://repo@sha:path
  sha256     bytea NOT NULL,
  tainted    boolean NOT NULL DEFAULT false,
  supersedes text REFERENCES mesh.artifacts(id),
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Decision records (ADR-style; continues D-008+)
CREATE TABLE IF NOT EXISTS mesh.decisions (
  id          text PRIMARY KEY,                   -- 'D-008'...
  title       text NOT NULL,
  status      text NOT NULL CHECK (status IN ('proposed','accepted','superseded','rejected')),
  context     text NOT NULL,
  decision    text NOT NULL,
  alternatives jsonb NOT NULL DEFAULT '[]'::jsonb,
  consequences text,
  dissent     text,
  deciders    text[] NOT NULL,
  logan_verbatim text,                            -- when the Boss decided (C5)
  supersedes  text REFERENCES mesh.decisions(id),
  huddle_id   text,
  task_ids    text[] NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Bounded deliberation
CREATE TABLE IF NOT EXISTS mesh.huddles (
  id           text PRIMARY KEY,                  -- 'H-0007'
  topic        text NOT NULL,
  kind         text NOT NULL,                     -- design_review, planning, retro, dispute, incident
  participants text[] NOT NULL,
  chair        text NOT NULL,
  question     text NOT NULL,
  state        text NOT NULL,
  round        integer NOT NULL DEFAULT 0,
  max_rounds   integer NOT NULL DEFAULT 3,
  budget_tokens integer NOT NULL DEFAULT 60000,
  spent_tokens integer NOT NULL DEFAULT 0,
  context_id   text NOT NULL REFERENCES mesh.threads(context_id),
  decision_id  text REFERENCES mesh.decisions(id),
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- Every bounded agent execution
CREATE TABLE IF NOT EXISTS mesh.turns (
  id              text PRIMARY KEY,               -- 'TU-5531'
  agent           text NOT NULL REFERENCES mesh.principals(id),
  task_id         text REFERENCES mesh.tasks(id),
  trigger_event_ids bigint[] NOT NULL,
  priority        text NOT NULL,
  model           text NOT NULL,
  prompt_manifest jsonb NOT NULL,                 -- layer, version, sha256, tokens
  context_pack_sha256 bytea NOT NULL,
  input_tokens    integer, output_tokens integer, cache_read_tokens integer, cache_write_tokens integer,
  cost_usd        numeric(12,6),                  -- computed by Ballast from prices.yaml
  tool_calls      integer,
  tainted         boolean NOT NULL DEFAULT false,
  outcome         text,                           -- completed, limit_reached, error, canceled
  trace_id        text,
  started_at      timestamptz NOT NULL,
  ended_at        timestamptz
);
CREATE INDEX IF NOT EXISTS idx_turns_agent ON mesh.turns (agent, started_at DESC);

-- Immutable tool-call receipts, written by Toolgate only
CREATE TABLE IF NOT EXISTS mesh.receipts (
  id            text PRIMARY KEY,                 -- 'RC-8820'
  turn_id       text REFERENCES mesh.turns(id),
  task_id       text REFERENCES mesh.tasks(id),
  agent         text NOT NULL,
  tool          text NOT NULL,
  risk_tier     text NOT NULL,
  args_digest   bytea NOT NULL,
  result_digest bytea,
  status        text NOT NULL CHECK (status IN ('ok','error','denied','timeout')),
  denial_reason text,
  exit_code     integer,
  summary       text,
  artifact_id   text REFERENCES mesh.artifacts(id),
  approval_id   text,
  taint_in      boolean NOT NULL DEFAULT false,
  idempotency_key text UNIQUE,
  started_at    timestamptz NOT NULL,
  ended_at      timestamptz
);
CREATE INDEX IF NOT EXISTS idx_receipts_task ON mesh.receipts (task_id, tool, started_at DESC);

-- R3 approvals: single-use tokens bound to an action hash
CREATE TABLE IF NOT EXISTS mesh.approvals (
  id            text PRIMARY KEY,                 -- 'AP-0091'
  task_id       text REFERENCES mesh.tasks(id),
  requested_by  text NOT NULL,
  executor      text NOT NULL,
  tool          text NOT NULL,
  args          jsonb NOT NULL,
  action_hash   bytea NOT NULL,
  risk_tier     text NOT NULL DEFAULT 'R3',
  why           text NOT NULL,
  rollback      text,
  evidence      text[] NOT NULL DEFAULT '{}',
  cost_estimate_usd numeric(12,2),                -- deterministic tool output only
  status        text NOT NULL CHECK (status IN ('pending','granted','denied','expired','consumed')),
  decided_by    text,
  decided_at    timestamptz,
  decision_note text,                             -- the Boss verbatim
  expires_at    timestamptz NOT NULL,
  consumed_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Budgets (Ballast)
CREATE TABLE IF NOT EXISTS mesh.budgets (
  scope       text NOT NULL,                      -- 'global','agent:duke','task:T-0142','huddle:H-0007'
  period      text NOT NULL,                      -- '2026-10', '2026-10-14', 'lifetime'
  limit_usd   numeric(12,2),
  limit_tokens bigint,
  spent_usd   numeric(12,6) NOT NULL DEFAULT 0,
  spent_tokens bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (scope, period)
);

-- Lessons ledger continues from L-025
CREATE TABLE IF NOT EXISTS mesh.lessons (
  id            text PRIMARY KEY,                 -- 'L-025'...
  trigger       text NOT NULL,
  rule          text NOT NULL,
  applied_to    text NOT NULL,
  verification  text NOT NULL,
  status        text NOT NULL CHECK (status IN ('candidate','accepted','verified_applied','rejected')),
  evidence_ids  text[] NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Single scheduler
CREATE TABLE IF NOT EXISTS mesh.schedules (
  id        text PRIMARY KEY,
  cron      text NOT NULL,
  tz        text NOT NULL DEFAULT 'Asia/Kuala_Lumpur',
  owner     text NOT NULL,
  action    jsonb NOT NULL,
  priority  text NOT NULL DEFAULT 'P3',
  enabled   boolean NOT NULL DEFAULT true,
  last_run_at timestamptz,
  next_run_at timestamptz
);

-- Pulse stamps (generalize v1 pulse:* KV)
CREATE TABLE IF NOT EXISTS mesh.pulses (
  job        text PRIMARY KEY,
  last_ok_at timestamptz,
  last_status text,
  detail     jsonb,
  max_age    interval NOT NULL DEFAULT interval '26 hours'
);

-- Liveness registry: producer/consumer broken links raise liveness.broken (L-020)
CREATE TABLE IF NOT EXISTS mesh.liveness (
  component  text PRIMARY KEY,
  role       text NOT NULL CHECK (role IN ('producer','consumer','both')),
  depends_on text[] NOT NULL DEFAULT '{}',
  enabled    boolean NOT NULL,
  last_seen_at timestamptz
);

-- Memory write-path audit log
CREATE TABLE IF NOT EXISTS mesh.memory_log (
  id         bigserial PRIMARY KEY,
  bank       text NOT NULL,
  hindsight_id text,
  action     text NOT NULL CHECK (action IN ('proposed','committed','merged','conflict','held','tombstoned','erased')),
  subject    text,
  sources    text[] NOT NULL DEFAULT '{}',
  actor      text NOT NULL,
  ts         timestamptz NOT NULL DEFAULT now()
);

-- Clients with consent scope and market-derived currency
CREATE TABLE IF NOT EXISTS mesh.clients (
  id            text PRIMARY KEY,                 -- 'CL-become'
  display_name  text NOT NULL,
  market        text NOT NULL CHECK (market IN ('SG','MY','OTHER')),
  currency      text GENERATED ALWAYS AS (CASE market WHEN 'SG' THEN 'SGD' WHEN 'MY' THEN 'MYR' ELSE 'USD' END) STORED,
  consent_scope jsonb NOT NULL,                   -- what may be stored/shown/shared
  contacts_enc  bytea,                            -- encrypted; never logged
  status        text NOT NULL
);

-- Seed principals (idempotent)
INSERT INTO mesh.principals (id, kind, display_name, wiring_name) VALUES
  ('logan','human','the Boss',NULL),
  ('cofounder','human','a second user',NULL),
  ('duke','agent','Duke','duke'),
  ('hermes','agent','Hermes','hermes'),
  ('frank','agent','Frank','nanook'),
  ('svc-verifier','service','Verifier',NULL),
  ('svc-reader','service','Reader',NULL),
  ('svc-scribe','service','Scribe',NULL),
  ('svc-tooltester','service','Tool-tester',NULL)
ON CONFLICT (id) DO NOTHING;

-- Blimp Crew v2 — migration 0007: turn dispatch (spec §8.6 wake policy, §10.2 fallback
-- "jobs table with SELECT ... FOR UPDATE SKIP LOCKED plus explicit checkpoint rows", §10.5).
-- DBOS is not adopted yet; this is the spec's own fallback with the same semantics. Additive only.

-- One row per pending/running agent turn. Coalescing (spec 8.6): while a request for an
-- agent at a priority is still queued, new wake events are appended to it instead of
-- creating another turn ("ten events cost one context pack, not ten").
CREATE TABLE IF NOT EXISTS mesh.turn_requests (
  id                bigserial PRIMARY KEY,
  agent             text NOT NULL REFERENCES mesh.principals(id),
  task_id           text REFERENCES mesh.tasks(id),
  priority          text NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
  initiator         text NOT NULL,                 -- logan | schedule | <agent> (who caused the wake)
  trigger_event_ids bigint[] NOT NULL DEFAULT '{}',
  status            text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','poison','dropped')),
  not_before        timestamptz NOT NULL DEFAULT now(),   -- debounce / office-hours window
  attempts          integer NOT NULL DEFAULT 0,
  host              text,                          -- which host process claimed it
  heartbeat_at      timestamptz,                   -- renewed by the host while running (code, not LLM)
  turn_id           text,
  turn_key_hash     text,                          -- sha256 of a per-turn secret (runtimes without a per-turn process: Hermes)
  last_error        text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  started_at        timestamptz,
  finished_at       timestamptz
);
CREATE INDEX IF NOT EXISTS idx_turn_requests_ready ON mesh.turn_requests (agent, status, not_before);
CREATE UNIQUE INDEX IF NOT EXISTS uq_turn_requests_coalesce
  ON mesh.turn_requests (agent, priority, COALESCE(task_id, '')) WHERE status = 'queued';

-- How far the router has read the event log.
CREATE TABLE IF NOT EXISTS mesh.router_cursor (
  id             boolean PRIMARY KEY DEFAULT true CHECK (id),
  last_event_id  bigint NOT NULL DEFAULT 0,
  updated_at     timestamptz NOT NULL DEFAULT now()
);
INSERT INTO mesh.router_cursor (id, last_event_id) VALUES (true, 0) ON CONFLICT DO NOTHING;

-- Session continuity per (agent, task) (spec 10.4: "a session per task, resumed across turns").
CREATE TABLE IF NOT EXISTS mesh.host_sessions (
  agent        text NOT NULL,
  task_key     text NOT NULL,                     -- task id, or 'chat' for task-less turns
  session_id   text NOT NULL,
  turns        integer NOT NULL DEFAULT 0,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (agent, task_key)
);

-- Kill switch state (spec 14.6): /freeze, /pause <agent>, /drafts-only, /readonly.
CREATE TABLE IF NOT EXISTS mesh.system_modes (
  scope      text PRIMARY KEY,                    -- 'global' or 'agent:<id>'
  mode       text NOT NULL CHECK (mode IN ('normal','frozen','paused','drafts_only','readonly')),
  set_by     text NOT NULL,
  set_at     timestamptz NOT NULL DEFAULT now()
);

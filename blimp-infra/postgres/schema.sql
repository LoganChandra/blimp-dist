-- Duke's business data schema (Postgres).
-- NOTE: Long-term memory (facts, conversations, decisions, doc retrieval)
-- is handled by Hindsight (running as a Docker container).
-- This schema is for structured BUSINESS data that doesn't fit Hindsight's
-- memory model — agent task queue, client pipeline, pricing log, project state.

-- pgvector still useful for any custom embedding needs we add later.
CREATE EXTENSION IF NOT EXISTS vector;

-- ------------------------------------------------------------------
-- tasks: Duke's work queue (also user-assigned)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
  id           BIGSERIAL PRIMARY KEY,
  title        TEXT         NOT NULL,
  description  TEXT,
  status       VARCHAR(20)  DEFAULT 'pending',     -- pending, in_progress, completed, blocked, cancelled
  priority     VARCHAR(10)  DEFAULT 'P2',          -- P0, P1, P2, P3
  assigned_to  VARCHAR(50),                         -- 'duke', 'logan', user_id
  parent_id    BIGINT       REFERENCES tasks(id),  -- subtasks
  metadata     JSONB        DEFAULT '{}'::jsonb,
  created_at   TIMESTAMPTZ  DEFAULT NOW(),
  updated_at   TIMESTAMPTZ  DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks (status);
CREATE INDEX IF NOT EXISTS tasks_assigned_idx ON tasks (assigned_to, status);

-- ------------------------------------------------------------------
-- prospects: client pipeline for the agency
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prospects (
  id              BIGSERIAL PRIMARY KEY,
  company_name    TEXT,
  industry        VARCHAR(100),
  country         VARCHAR(50),
  decision_maker  TEXT,
  contact_channel VARCHAR(50),                      -- 'IG', 'WhatsApp', 'LinkedIn', 'Email', 'Referral'
  team_size       INTEGER,
  revenue_est     NUMERIC,
  revenue_currency VARCHAR(10),
  pain_points     JSONB        DEFAULT '[]'::jsonb,
  qualification_score INTEGER,
  stage           VARCHAR(30)  DEFAULT 'new',       -- new, qualified, discovery, quoted, won, lost, parked
  quoted_amount   NUMERIC,
  quoted_currency VARCHAR(10),
  notes           TEXT,
  metadata        JSONB        DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ  DEFAULT NOW(),
  updated_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS prospects_stage_idx    ON prospects (stage);
CREATE INDEX IF NOT EXISTS prospects_industry_idx ON prospects (industry);

-- ------------------------------------------------------------------
-- pricing_log: every quote Duke computes, for pattern recognition
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing_log (
  id            BIGSERIAL PRIMARY KEY,
  prospect_id   BIGINT       REFERENCES prospects(id),
  inputs        JSONB        NOT NULL,              -- complexity, integrations, languages, urgency, etc.
  quote_amount  NUMERIC      NOT NULL,
  quote_currency VARCHAR(10) NOT NULL,
  reasoning     TEXT,                                -- Duke's reasoning
  framework_version VARCHAR(20),                     -- track which pricing framework version was used
  outcome       VARCHAR(20),                         -- 'sent', 'accepted', 'rejected', 'pending'
  created_at    TIMESTAMPTZ  DEFAULT NOW()
);

-- ------------------------------------------------------------------
-- project_state: KV store for current state, flags, ongoing context
-- (small, structured state Duke needs to read/write atomically — Hindsight
--  isn't designed for KV reads, so we keep this in Postgres)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_state (
  key         VARCHAR(100) PRIMARY KEY,
  value       JSONB        NOT NULL,
  updated_at  TIMESTAMPTZ  DEFAULT NOW()
);

-- ------------------------------------------------------------------
-- a2a_messages: Agent2Agent task queue (Duke <-> Hermes inter-agent comms)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS a2a_messages (
  id              BIGSERIAL    PRIMARY KEY,
  task_id         TEXT         NOT NULL,            -- UUIDv4 string per task
  parent_task_id  TEXT,                              -- replies link to original
  from_agent      VARCHAR(50)  NOT NULL,             -- 'duke' | 'hermes'
  to_agent        VARCHAR(50)  NOT NULL,
  state           VARCHAR(20)  NOT NULL DEFAULT 'submitted',
                                                     -- submitted | working | input-required | completed | failed | canceled
  skill           VARCHAR(100),                      -- optional skill name (e.g. 'design-architecture')
  body            JSONB        NOT NULL,             -- A2A JSON-RPC message payload (role, parts, metadata)
  result          JSONB,                             -- final result/artifact when completed
  created_at      TIMESTAMPTZ  DEFAULT NOW(),
  updated_at      TIMESTAMPTZ  DEFAULT NOW(),
  acked_at        TIMESTAMPTZ                        -- when recipient first read it
);

CREATE INDEX IF NOT EXISTS a2a_to_state_idx ON a2a_messages (to_agent, state);
CREATE INDEX IF NOT EXISTS a2a_task_idx     ON a2a_messages (task_id);
CREATE INDEX IF NOT EXISTS a2a_parent_idx   ON a2a_messages (parent_task_id);

-- ------------------------------------------------------------------
-- orchestrator_runs: LangGraph supervisor runs, USD budget tracking
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orchestrator_runs (
  id              BIGSERIAL    PRIMARY KEY,
  thread_id       TEXT         NOT NULL UNIQUE,      -- LangGraph thread_id
  task            TEXT         NOT NULL,             -- original task description
  pattern         VARCHAR(50)  NOT NULL DEFAULT 'generator-verifier',
  status          VARCHAR(20)  NOT NULL DEFAULT 'running', -- running, completed, failed, blocked, budget_exceeded
  turn_count      INTEGER      DEFAULT 0,
  max_turns       INTEGER      DEFAULT 6,
  usd_spent       NUMERIC(10,4) DEFAULT 0,
  usd_budget      NUMERIC(10,4) DEFAULT 1.00,        -- $1 default per task
  final_result    TEXT,
  created_at      TIMESTAMPTZ  DEFAULT NOW(),
  updated_at      TIMESTAMPTZ  DEFAULT NOW(),
  completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS orch_runs_status_idx ON orchestrator_runs (status, created_at DESC);

-- ------------------------------------------------------------------
-- Helper view: recent activity
-- ------------------------------------------------------------------
CREATE OR REPLACE VIEW recent_activity AS
SELECT 'task' AS kind, id, updated_at AS at, status AS context, title AS summary
FROM tasks
WHERE status IN ('in_progress', 'pending')
UNION ALL
SELECT 'prospect' AS kind, id, updated_at AS at, stage AS context, company_name AS summary
FROM prospects
WHERE stage NOT IN ('lost', 'parked')
UNION ALL
SELECT 'quote' AS kind, id, created_at AS at, COALESCE(outcome, 'pending') AS context,
       'Quote ' || quote_currency || ' ' || quote_amount AS summary
FROM pricing_log
ORDER BY at DESC
LIMIT 50;

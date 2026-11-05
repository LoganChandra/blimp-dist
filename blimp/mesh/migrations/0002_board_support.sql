-- Blimp Crew v2 — migration 0002: board support (spec §9, §15, §21.3 `blimp migrate`)
-- Additive only. Safe to re-run.

-- Which migrations have been applied (the runner in blimp/mesh/migrate.py writes here).
CREATE TABLE IF NOT EXISTS mesh.schema_migrations (
  name        text PRIMARY KEY,
  sha256      text NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now()
);

-- Human-readable IDs (T-0142, D-011, M-0102, AR-0311, RC-8820, AP-0091, H-0007, TU-5531),
-- issued by code, never by agents. One row per prefix; UPDATE ... RETURNING is atomic.
CREATE TABLE IF NOT EXISTS mesh.id_counters (
  prefix  text PRIMARY KEY,
  last    bigint NOT NULL DEFAULT 0
);
-- Decisions and lessons continue the v1 registries (D-001..D-010 and L-001..L-024 exist).
INSERT INTO mesh.id_counters (prefix, last) VALUES ('D', 10), ('L', 24) ON CONFLICT (prefix) DO NOTHING;

-- Board read paths.
CREATE INDEX IF NOT EXISTS idx_tasks_state ON mesh.tasks (state);
CREATE INDEX IF NOT EXISTS idx_tasks_lease ON mesh.tasks (lease_expires_at) WHERE lease_holder IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_task ON mesh.messages (task_id, created_at);

-- Where an imported row came from, so the v1 import is idempotent (spec §15 migration mapping).
CREATE TABLE IF NOT EXISTS mesh.import_map (
  source      text NOT NULL,      -- 'v1.tasks', 'v1.a2a_messages'
  source_id   text NOT NULL,
  target_id   text NOT NULL,      -- T-####, or a message id
  imported_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, source_id)
);

-- One-intent-per-message lint is "warn, then reject" (spec §8.11): remember who was warned.
CREATE TABLE IF NOT EXISTS mesh.lint_warnings (
  sender      text NOT NULL,
  rule        text NOT NULL,
  context_id  text NOT NULL,
  warned_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sender, rule, context_id)
);

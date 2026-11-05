-- blimp-mesh: event-driven push for A2A (2026-09-26). Additive + idempotent.
-- Rollback: mesh-rollback.sql. Nothing here deletes or rewrites existing rows.
--
-- Every INSERT into a2a_messages fires pg_notify('blimp_mesh', {id,to,from,thread,hop}).
-- Each agent's mesh channel server LISTENs and pushes the message into its live
-- session. No polling, no timers: an idle agent costs zero tokens until a
-- message actually arrives (D-001 "dont waste the tokens" still holds).

ALTER TABLE a2a_messages ADD COLUMN IF NOT EXISTS thread_id     TEXT;
ALTER TABLE a2a_messages ADD COLUMN IF NOT EXISTS hop           INTEGER NOT NULL DEFAULT 0;
ALTER TABLE a2a_messages ADD COLUMN IF NOT EXISTS expects_reply BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE a2a_messages ADD COLUMN IF NOT EXISTS pushed_at     TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS a2a_thread_idx ON a2a_messages (thread_id);

-- Who is online, and how to reach them. Written by each mesh server on start
-- and on every push; a row older than ~2 min with no live pid is "offline".
CREATE TABLE IF NOT EXISTS agent_presence (
  agent       VARCHAR(50) PRIMARY KEY,
  status      VARCHAR(20) NOT NULL DEFAULT 'online',   -- online | offline
  transport   VARCHAR(30) NOT NULL,                    -- claude-channel | hermes-webhook
  pid         INTEGER,
  started_at  TIMESTAMPTZ DEFAULT NOW(),
  last_seen   TIMESTAMPTZ DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION a2a_mesh_before() RETURNS trigger AS $$
BEGIN
  NEW.thread_id := COALESCE(NEW.thread_id, NEW.parent_task_id, NEW.task_id);
  IF NEW.parent_task_id IS NOT NULL THEN
    SELECT COALESCE(MAX(hop), -1) + 1 INTO NEW.hop
      FROM a2a_messages WHERE thread_id = NEW.thread_id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION a2a_mesh_notify() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('blimp_mesh', json_build_object(
    'id', NEW.id, 'to', NEW.to_agent, 'from', NEW.from_agent,
    'thread', NEW.thread_id, 'hop', NEW.hop)::text);
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS a2a_mesh_before ON a2a_messages;
CREATE TRIGGER a2a_mesh_before BEFORE INSERT ON a2a_messages
  FOR EACH ROW EXECUTE FUNCTION a2a_mesh_before();
DROP TRIGGER IF EXISTS a2a_mesh_notify ON a2a_messages;
CREATE TRIGGER a2a_mesh_notify AFTER INSERT ON a2a_messages
  FOR EACH ROW EXECUTE FUNCTION a2a_mesh_notify();

-- Backfill thread ids for history (safe: only fills NULLs).
UPDATE a2a_messages SET thread_id = COALESCE(parent_task_id, task_id) WHERE thread_id IS NULL;

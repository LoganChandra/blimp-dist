-- Blimp Crew v2 — migration 0003: Discord Bridge outbox (spec §10.1 outbox, §8.7, §18.1)
-- Additive only.

-- Transactional outbox: exactly-once EFFECT on Discord via idempotency keys. A row is
-- written in the same transaction as the event that caused it (or by the projector,
-- which is itself idempotent by key); the sender drains it and records Discord's id as
-- proof of delivery (no id = not delivered = retried).
CREATE TABLE IF NOT EXISTS mesh.outbox (
  id               bigserial PRIMARY KEY,
  idempotency_key  text NOT NULL UNIQUE,
  op               text NOT NULL CHECK (op IN ('thread_create','post','edit')),
  channel          text NOT NULL,             -- logical: board, hq, approvals, incidents, huddles, digest, audit
  context_id       text,                      -- task/huddle/incident thread this belongs to
  persona          text,                      -- duke | hermes | frank | deck (webhook identity)
  content          text NOT NULL,
  edit_of_key      text,                      -- for op=edit: the outbox key of the message to edit
  status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed','skipped')),
  attempts         integer NOT NULL DEFAULT 0,
  last_error       text,
  discord_message_id text,
  discord_thread_id  text,
  source_event_id  bigint,
  created_at       timestamptz NOT NULL DEFAULT now(),
  sent_at          timestamptz,
  next_attempt_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON mesh.outbox (next_attempt_at) WHERE status = 'pending';

-- How far the Bridge projector has read the event log (single row).
CREATE TABLE IF NOT EXISTS mesh.bridge_cursor (
  id              boolean PRIMARY KEY DEFAULT true CHECK (id),
  last_event_id   bigint NOT NULL DEFAULT 0,
  updated_at      timestamptz NOT NULL DEFAULT now()
);
INSERT INTO mesh.bridge_cursor (id, last_event_id) VALUES (true, 0) ON CONFLICT DO NOTHING;

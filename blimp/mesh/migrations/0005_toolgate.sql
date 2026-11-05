-- Blimp Crew v2 — migration 0005: Toolgate persistence (spec §13.4 receipts, §13.5 approvals)
-- Additive only.

-- Receipts are immutable (spec 13.4: "written by Toolgate, never by agents"). v1 agents have
-- no Deck turn yet, so turn_id may be NULL until phase 3 hosts exist.
CREATE OR REPLACE RULE receipts_no_update AS ON UPDATE TO mesh.receipts DO INSTEAD NOTHING;
CREATE OR REPLACE RULE receipts_no_delete AS ON DELETE TO mesh.receipts DO INSTEAD NOTHING;

-- The receipt's deterministic output, kept for the verifier and /why (never LLM-written).
ALTER TABLE mesh.receipts ADD COLUMN IF NOT EXISTS result jsonb;
ALTER TABLE mesh.receipts ADD COLUMN IF NOT EXISTS check_id text;   -- acceptance check this receipt evidences

CREATE INDEX IF NOT EXISTS idx_approvals_status ON mesh.approvals (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_receipts_turn ON mesh.receipts (turn_id);

-- Turn-scoped state Toolgate needs across calls (taint, pre-declared plan), keyed by turn.
CREATE TABLE IF NOT EXISTS mesh.turn_state (
  turn_id          text PRIMARY KEY,
  agent            text NOT NULL,
  task_id          text,
  tainted          boolean NOT NULL DEFAULT false,
  taint_sources    text[] NOT NULL DEFAULT '{}',
  planned_actions  text[] NOT NULL DEFAULT '{}',
  planned_locked   boolean NOT NULL DEFAULT false,   -- set at the first tainted read (plan-then-execute)
  sensitive        boolean NOT NULL DEFAULT false,
  external         boolean NOT NULL DEFAULT false,
  expires_at       timestamptz NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);

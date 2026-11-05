-- Blimp Crew v2 — migration 0006: separation of duties for runnable checks; safe idempotency
-- (fixes from the phase 2 security review, 2026-09-30). Additive only.

-- Who wrote the current contract, and who (someone else) approved its runnable checks.
-- A test/cmd check runs a shell command; until the spec 10.6 sandbox exists it may only run
-- after a principal OTHER than the contract's author has approved the exact contract version.
ALTER TABLE mesh.tasks ADD COLUMN IF NOT EXISTS contract_author text REFERENCES mesh.principals(id);
ALTER TABLE mesh.tasks ADD COLUMN IF NOT EXISTS checks_approved_by text REFERENCES mesh.principals(id);
ALTER TABLE mesh.tasks ADD COLUMN IF NOT EXISTS checks_approved_sha text;         -- sha256 of the contract they approved

-- Idempotency keys are reserved atomically before an effect runs, and bound to the caller,
-- the tool and the exact arguments (a key can never replay someone else's result).
CREATE TABLE IF NOT EXISTS mesh.idempotency (
  key          text PRIMARY KEY,
  agent        text NOT NULL,
  tool         text NOT NULL,
  args_digest  bytea NOT NULL,
  receipt_id   text,                 -- NULL while the call is in flight
  reserved_at  timestamptz NOT NULL DEFAULT now()
);

-- Blimp Crew v2 — migration 0004: spend samples for the idle-burn SLO (spec §4.4, §19.3, §23 phase 1 exit)
-- Additive only.

-- Hourly readings of provider usage counters, taken by a zero-token HTTP probe (no model
-- call). The difference between consecutive samples is what was actually spent, which
-- catches spend the turn log cannot see (e.g. a Hermes cron job running outside Deck).
CREATE TABLE IF NOT EXISTS mesh.spend_samples (
  provider     text NOT NULL,              -- 'openrouter', later 'anthropic' if API-billed
  taken_at     timestamptz NOT NULL DEFAULT now(),
  usage_usd    numeric(14,6) NOT NULL,     -- cumulative usage counter as reported by the provider
  credits_usd  numeric(14,6),              -- total credits, when the provider reports it
  PRIMARY KEY (provider, taken_at)
);

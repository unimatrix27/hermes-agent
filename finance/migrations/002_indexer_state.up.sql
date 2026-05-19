-- Issue unimatrix27/ideas#21 — Finance reconciliation indexer state.
--
-- Adds bank.indexer_state, keyed by mailbox, for the receipt indexer's
-- delta-token cursor + last-run summary. Idempotent on re-apply; the legacy
-- bank.* tables and receipt_candidates / receipt_matches (#20) are untouched.

BEGIN;

CREATE TABLE IF NOT EXISTS bank.indexer_state (
    mailbox          text PRIMARY KEY,
    delta_token      text,
    last_run_at      timestamptz,
    last_error       text,
    last_summary     jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

COMMIT;

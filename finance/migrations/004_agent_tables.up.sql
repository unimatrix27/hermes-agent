-- Issue unimatrix27/ideas#23 — agent persistence tables.
--
-- Per #23: "define the tool against the tables' shapes as documented in
-- #25, and ship a migration 004 that creates them. Otherwise #25 cannot
-- even start without an extra round-trip." This migration creates the
-- TABLES only; the agent skill that writes to them ships in #25.
--
-- Two tables:
--
--   bank.agent_anomalies         — destinations for `flag_anomaly`
--   bank.agent_reconcile_runs    — destinations for `finalize_run`
--
-- Neither table replaces or modifies legacy artefacts (bank.agent_runs
-- and bank.agent_call_log already exist for the older outlook_auto_rule
-- pipeline and are left untouched).

BEGIN;

CREATE TABLE IF NOT EXISTS bank.agent_reconcile_runs (
    id                bigserial PRIMARY KEY,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finalized_at      timestamptz,
    summary_md        text,
    proposed_changes  jsonb,
    tool_call_summary jsonb,
    invoked_by        text NOT NULL DEFAULT 'llm',
    notes             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent_reconcile_runs_invoked_by_chk
        CHECK (invoked_by IN ('llm', 'user', 'cron'))
);

CREATE INDEX IF NOT EXISTS agent_reconcile_runs_started_at_idx
    ON bank.agent_reconcile_runs (started_at DESC);

CREATE INDEX IF NOT EXISTS agent_reconcile_runs_finalized_at_idx
    ON bank.agent_reconcile_runs (finalized_at DESC);


CREATE TABLE IF NOT EXISTS bank.agent_anomalies (
    id           bigserial PRIMARY KEY,
    bank_tx_id   bigint REFERENCES bank.transactions(id) ON DELETE RESTRICT,
    reason       text   NOT NULL,
    severity     text   NOT NULL,
    status       text   NOT NULL DEFAULT 'open',
    raised_by    text   NOT NULL DEFAULT 'llm',
    run_id       bigint REFERENCES bank.agent_reconcile_runs(id) ON DELETE SET NULL,
    legacy_meta  jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz,
    CONSTRAINT agent_anomalies_severity_chk
        CHECK (severity IN ('info', 'warn', 'block')),
    CONSTRAINT agent_anomalies_status_chk
        CHECK (status IN ('open', 'acknowledged', 'resolved')),
    CONSTRAINT agent_anomalies_raised_by_chk
        CHECK (raised_by IN ('llm', 'user', 'system'))
);

CREATE INDEX IF NOT EXISTS agent_anomalies_bank_tx_id_idx
    ON bank.agent_anomalies (bank_tx_id);

CREATE INDEX IF NOT EXISTS agent_anomalies_status_idx
    ON bank.agent_anomalies (status);

CREATE INDEX IF NOT EXISTS agent_anomalies_created_at_idx
    ON bank.agent_anomalies (created_at DESC);

CREATE INDEX IF NOT EXISTS agent_anomalies_severity_idx
    ON bank.agent_anomalies (severity);

COMMIT;

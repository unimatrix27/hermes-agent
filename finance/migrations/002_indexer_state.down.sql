-- Rollback for 002_indexer_state.up.sql.
-- Drops only what the up migration created.

BEGIN;

DROP TABLE IF EXISTS bank.indexer_state;

COMMIT;

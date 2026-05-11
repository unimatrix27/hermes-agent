-- Rollback for 001_receipt_tables.up.sql.
-- Drops only what the up migration created. Legacy bank.* tables are untouched.

BEGIN;

DROP TABLE IF EXISTS bank.receipt_matches;
DROP TABLE IF EXISTS bank.receipt_candidates;

COMMIT;

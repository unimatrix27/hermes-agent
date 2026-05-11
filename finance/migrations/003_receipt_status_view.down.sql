-- Roll back unimatrix27/ideas#23 view.
BEGIN;
DROP VIEW IF EXISTS bank.receipt_status_v;
COMMIT;

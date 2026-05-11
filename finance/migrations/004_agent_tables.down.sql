-- Roll back unimatrix27/ideas#23 agent tables.
BEGIN;
DROP TABLE IF EXISTS bank.agent_anomalies;
DROP TABLE IF EXISTS bank.agent_reconcile_runs;
COMMIT;

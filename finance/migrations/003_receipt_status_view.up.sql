-- Issue unimatrix27/ideas#23 — Finance reconciliation toolbox.
--
-- Adds the additive `bank.receipt_status_v` view used by the
-- `list_open_transactions` tool to surface the six-bucket classification
-- agreed in #27:
--
--   done              — already forwarded to DATEV (legacy bank.belege_sent
--                       or a receipt_matches row with decision_status='sent')
--   available_to_send — an approved match is waiting for send_match()
--   manual_needed     — a portal-only / manual_review_legacy match needs a human
--   ambiguous         — at least one 'proposed' row, but no approval yet
--   missing           — non-ignored tx with no match rows at all
--   ignored           — transactions.ignored = true (never touched by the agent)
--
-- bank.belege_missing (existing view) is NOT modified.

BEGIN;

CREATE OR REPLACE VIEW bank.receipt_status_v AS
WITH match_rollup AS (
    SELECT
        bank_tx_id,
        COUNT(*) FILTER (WHERE decision_status = 'proposed')      AS proposed_count,
        COUNT(*) FILTER (WHERE decision_status = 'approved')      AS approved_count,
        COUNT(*) FILTER (WHERE decision_status = 'sent')          AS sent_count,
        COUNT(*) FILTER (WHERE decision_status = 'manual_needed') AS manual_needed_count,
        COUNT(*) FILTER (WHERE decision_status = 'ignored')       AS ignored_count,
        COUNT(*) FILTER (WHERE decision_status = 'rejected')      AS rejected_count
    FROM bank.receipt_matches
    GROUP BY bank_tx_id
)
SELECT
    t.id                              AS bank_tx_id,
    t.booking_date,
    t.amount,
    t.signed_amount,
    t.currency,
    t.credit_debit,
    t.counterparty_name,
    t.remittance_information,
    t.ignored,
    COALESCE(mr.proposed_count, 0)      AS proposed_count,
    COALESCE(mr.approved_count, 0)      AS approved_count,
    COALESCE(mr.sent_count, 0)          AS sent_count,
    COALESCE(mr.manual_needed_count, 0) AS manual_needed_count,
    COALESCE(mr.ignored_count, 0)       AS match_ignored_count,
    COALESCE(mr.rejected_count, 0)      AS rejected_count,
    EXISTS (
        SELECT 1 FROM bank.belege_sent bs WHERE bs.bank_tx_id = t.id
    ) AS legacy_belege_sent_exists,
    CASE
        WHEN t.ignored = true THEN 'ignored'
        WHEN COALESCE(mr.sent_count, 0) > 0
          OR EXISTS (SELECT 1 FROM bank.belege_sent bs WHERE bs.bank_tx_id = t.id)
            THEN 'done'
        WHEN COALESCE(mr.manual_needed_count, 0) > 0 THEN 'manual_needed'
        WHEN COALESCE(mr.approved_count, 0) > 0     THEN 'available_to_send'
        WHEN COALESCE(mr.proposed_count, 0) > 0     THEN 'ambiguous'
        ELSE 'missing'
    END AS status
FROM bank.transactions t
LEFT JOIN match_rollup mr ON mr.bank_tx_id = t.id;

COMMENT ON VIEW bank.receipt_status_v IS
    'Six-bucket reconciliation status per bank.transactions row. '
    'Additive — bank.belege_missing remains the legacy authoritative view.';

COMMIT;

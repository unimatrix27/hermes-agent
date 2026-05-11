-- Issue unimatrix27/ideas#20 — Finance reconciliation data foundation.
--
-- Creates two new tables in the existing bank.* schema. Legacy tables
-- (transactions, belege_sent, belege_to_send, belege_missing, match_proposals)
-- are left untouched.

BEGIN;

CREATE TABLE IF NOT EXISTS bank.receipt_candidates (
    id                    bigserial PRIMARY KEY,
    source_system         text        NOT NULL,
    mailbox               text,
    outlook_message_id    text,
    internet_message_id   text,
    received_at           timestamptz,
    sent_at               timestamptz,
    from_email            text,
    subject               text,
    attachment_name       text,
    attachment_sha256     text,
    local_blob_path       text,
    text_sha256           text,
    extracted_text        text,
    extracted_json        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    parse_status          text        NOT NULL DEFAULT 'pending',
    parse_error           text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT receipt_candidates_source_system_chk
        CHECK (source_system IN ('graph', 'manual_upload', 'portal', 'datev_sent')),
    CONSTRAINT receipt_candidates_parse_status_chk
        CHECK (parse_status IN ('pending', 'ok', 'failed', 'portal_required'))
);

CREATE UNIQUE INDEX IF NOT EXISTS receipt_candidates_attachment_sha256_uniq
    ON bank.receipt_candidates (attachment_sha256)
    WHERE attachment_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS receipt_candidates_internet_message_id_idx
    ON bank.receipt_candidates (internet_message_id);

CREATE INDEX IF NOT EXISTS receipt_candidates_outlook_message_id_idx
    ON bank.receipt_candidates (outlook_message_id);

CREATE INDEX IF NOT EXISTS receipt_candidates_source_parse_idx
    ON bank.receipt_candidates (source_system, parse_status);

CREATE INDEX IF NOT EXISTS receipt_candidates_extracted_json_gin
    ON bank.receipt_candidates USING gin (extracted_json);


CREATE TABLE IF NOT EXISTS bank.receipt_matches (
    id                       bigserial PRIMARY KEY,
    receipt_candidate_id     bigint REFERENCES bank.receipt_candidates(id) ON DELETE RESTRICT,
    bank_tx_id               bigint NOT NULL REFERENCES bank.transactions(id) ON DELETE RESTRICT,
    confidence               text,
    match_type               text   NOT NULL,
    reason_codes             jsonb  NOT NULL DEFAULT '[]'::jsonb,
    decision_status          text   NOT NULL,
    decided_by               text   NOT NULL,
    legacy_belege_sent_id    bigint,
    legacy_meta              jsonb,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT receipt_matches_confidence_chk
        CHECK (confidence IS NULL OR confidence IN ('very_high', 'high', 'medium', 'low')),
    CONSTRAINT receipt_matches_match_type_chk
        CHECK (match_type IN (
            'exact_invoice_number',
            'exact_amount_date',
            'vendor_period',
            'refund_to_invoice',
            'manual',
            'portal_only',
            'manual_review_legacy'
        )),
    CONSTRAINT receipt_matches_decision_status_chk
        CHECK (decision_status IN (
            'proposed', 'approved', 'rejected', 'sent', 'ignored', 'manual_needed'
        )),
    CONSTRAINT receipt_matches_decided_by_chk
        CHECK (decided_by IN ('code', 'user', 'llm', 'legacy_outlook_rule')),
    CONSTRAINT receipt_matches_candidate_tx_uniq
        UNIQUE (receipt_candidate_id, bank_tx_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS receipt_matches_legacy_belege_sent_uniq
    ON bank.receipt_matches (legacy_belege_sent_id)
    WHERE legacy_belege_sent_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS receipt_matches_tx_idx
    ON bank.receipt_matches (bank_tx_id);

CREATE INDEX IF NOT EXISTS receipt_matches_decision_status_idx
    ON bank.receipt_matches (decision_status);

CREATE INDEX IF NOT EXISTS receipt_matches_legacy_meta_gin
    ON bank.receipt_matches USING gin (legacy_meta);

COMMIT;

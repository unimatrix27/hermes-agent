"""Matcher integration tests for unimatrix27/ideas#22.

Every named TX outcome in #22's acceptance criteria is asserted here against
an `InMemoryMatcherAdapter` loaded from the fixture pack (#24). No DB.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from finance.matcher import (
    Candidate,
    InMemoryMatcherAdapter,
    ProposedMatch,
    Transaction,
    identify_vendor,
    run_matcher,
)
from finance.parsers import parse as parse_candidate

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "finance"


# Per PR #1's backfill, these TXs already have decision_status='sent' in
# bank.receipt_matches and must be skipped by the matcher.
ALREADY_SENT_TX_IDS = {5, 53, 66}


def _load_transactions() -> list[Transaction]:
    rows = []
    for line in (FIXTURE_ROOT / "transactions.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(Transaction.from_row(json.loads(line)))
    return rows


def _load_candidate(vendor: str, name: str, cid: int) -> Candidate:
    txt = (FIXTURE_ROOT / vendor / f"{name}.txt").read_text(encoding="utf-8")
    meta = json.loads((FIXTURE_ROOT / vendor / f"{name}.meta.json").read_text(encoding="utf-8"))
    extracted = parse_candidate({
        "extracted_text": txt,
        "from_email": meta["from"],
        "subject": meta["subject"],
        "attachment_name": meta["attachment_name"],
        "internet_message_id": meta["internet_message_id"],
        "received_at": meta["received_at"],
    })
    assert extracted is not None, f"parser returned None for {vendor}/{name}"
    return Candidate(
        id=cid,
        extracted_json=extracted,
        parse_status=extracted["parse_status"],
    )


@pytest.fixture
def adapter() -> InMemoryMatcherAdapter:
    a = InMemoryMatcherAdapter()
    a.transactions = _load_transactions()
    a.candidates = [
        _load_candidate("sipgate", "B4373121", 101),
        _load_candidate("sipgate", "B4411208", 102),
        _load_candidate("sipgate", "B4459838", 103),
        _load_candidate("notion", "ZWLWGPDN-0002", 201),
        _load_candidate("lucky_penny", "6945-10683", 301),
        _load_candidate("lucky_penny", "CN-6945-10021", 302),
        _load_candidate("vodafone", "122203440401", 401),
        _load_candidate("vodafone", "portal_notification_2026_04", 402),
    ]
    # Pre-existing 'sent' matches from the #20 backfill.
    for tx_id in ALREADY_SENT_TX_IDS:
        a.matches.append({
            "id": -tx_id,
            "bank_tx_id": tx_id,
            "receipt_candidate_id": None,
            "confidence": None,
            "match_type": "manual_review_legacy",
            "reason_codes": ["legacy_backfill"],
            "decision_status": "sent",
            "decided_by": "legacy_outlook_rule",
            "legacy_meta": {"origin": "legacy_seed_for_test"},
        })
    return a


def _proposed_for(adapter: InMemoryMatcherAdapter, tx_id: int) -> list[dict]:
    return [
        m for m in adapter.matches
        if m["bank_tx_id"] == tx_id
        and m.get("decided_by") == "code"
    ]


# ──────────────────────────────────────────────────────────────────────────
# Vendor identification
# ──────────────────────────────────────────────────────────────────────────

def test_identify_vendor_per_tx() -> None:
    by_id = {t.id: t for t in _load_transactions()}
    assert identify_vendor(by_id[1]) == "vodafone"
    assert identify_vendor(by_id[5]) == "sipgate"
    assert identify_vendor(by_id[20]) == "lucky_penny"
    assert identify_vendor(by_id[31]) == "sipgate"
    assert identify_vendor(by_id[39]) == "lucky_penny"
    assert identify_vendor(by_id[66]) == "notion"
    assert identify_vendor(by_id[88]) == "notion"
    # TX 83 (Google ADS) is not a known vendor.
    assert identify_vendor(by_id[83]) is None


# ──────────────────────────────────────────────────────────────────────────
# Sipgate acceptance cases
# ──────────────────────────────────────────────────────────────────────────

def test_sipgate_b4373121_proposes_very_high_on_tx56(adapter) -> None:
    run_matcher(adapter)
    rows = _proposed_for(adapter, tx_id=56)
    sipgate = [r for r in rows if "vendor:sipgate" in r["reason_codes"]]
    assert len(sipgate) == 1
    r = sipgate[0]
    assert r["confidence"] == "very_high"
    assert r["match_type"] == "exact_invoice_number"
    assert r["decision_status"] == "proposed"
    assert r["decided_by"] == "code"
    assert "invoice_no:B4373121" in r["reason_codes"]
    assert "amount_eq:40.00" in r["reason_codes"]
    assert "date_within:1d" in r["reason_codes"]
    assert "mcc:4814" in r["reason_codes"]


def test_sipgate_b4411208_proposes_very_high_on_tx31(adapter) -> None:
    run_matcher(adapter)
    rows = _proposed_for(adapter, tx_id=31)
    assert len(rows) == 1
    r = rows[0]
    assert r["confidence"] == "very_high"
    assert r["match_type"] == "exact_invoice_number"
    assert "invoice_no:B4411208" in r["reason_codes"]
    assert "amount_eq:55.00" in r["reason_codes"]


def test_sipgate_b4459838_skipped_because_tx5_already_sent(adapter) -> None:
    run_matcher(adapter)
    rows = _proposed_for(adapter, tx_id=5)
    assert rows == [], "TX 5 already 'sent' from the #20 backfill — matcher must skip"


# ──────────────────────────────────────────────────────────────────────────
# Lucky Penny acceptance cases
# ──────────────────────────────────────────────────────────────────────────

def test_lucky_penny_6945_10683_proposes_high_on_tx39(adapter) -> None:
    run_matcher(adapter)
    rows = _proposed_for(adapter, tx_id=39)
    invoice_rows = [
        r for r in rows
        if r["match_type"] == "exact_amount_date"
        and "vendor:lucky_penny" in r["reason_codes"]
    ]
    assert len(invoice_rows) == 1
    r = invoice_rows[0]
    assert r["confidence"] == "high"
    assert r["decision_status"] == "proposed"
    assert "invoice_no:6945-10683" in r["reason_codes"]
    assert "amount_eq:59.50" in r["reason_codes"]
    assert "mcc:5817" in r["reason_codes"]


def test_lucky_penny_credit_note_proposes_high_on_tx20(adapter) -> None:
    run_matcher(adapter)
    rows = _proposed_for(adapter, tx_id=20)
    refund_rows = [r for r in rows if r["match_type"] == "refund_to_invoice"]
    assert len(refund_rows) == 1
    r = refund_rows[0]
    assert r["confidence"] == "high"
    assert r["decision_status"] == "proposed"
    assert "refund_ref:6945-10683" in r["reason_codes"]
    assert "amount_eq:9.50" in r["reason_codes"]
    assert "direction:refund" in r["reason_codes"]


# ──────────────────────────────────────────────────────────────────────────
# Notion acceptance cases
# ──────────────────────────────────────────────────────────────────────────

def test_notion_tx66_skipped_already_sent(adapter) -> None:
    run_matcher(adapter)
    rows = _proposed_for(adapter, tx_id=66)
    assert rows == [], "TX 66 already 'sent' — matcher must skip"


def test_notion_tx88_no_proposal_because_no_candidate_exists(adapter) -> None:
    run_matcher(adapter)
    rows = _proposed_for(adapter, tx_id=88)
    assert rows == [], (
        "TX 88 (234.00, 2026-03-05): no Notion candidate matches by amount or "
        "billing window, so no proposed row should be written"
    )


# ──────────────────────────────────────────────────────────────────────────
# Vodafone acceptance cases
# ──────────────────────────────────────────────────────────────────────────

def test_vodafone_tx53_skipped_already_sent(adapter) -> None:
    run_matcher(adapter)
    rows = _proposed_for(adapter, tx_id=53)
    assert rows == [], "TX 53 already 'sent' — matcher must skip"


def test_vodafone_tx1_writes_portal_only_AND_invoice_in_remittance(adapter) -> None:
    run_matcher(adapter)
    rows = _proposed_for(adapter, tx_id=1)

    portal = [r for r in rows if r["match_type"] == "portal_only"]
    assert len(portal) == 1, f"expected exactly one portal_only row, got {portal}"
    p = portal[0]
    assert p["decision_status"] == "manual_needed"
    assert p["confidence"] is None
    assert "portal_required:vodafone" in p["reason_codes"]
    assert "vendor:vodafone" in p["reason_codes"]

    remit = [
        r for r in rows
        if r["match_type"] == "exact_invoice_number"
        and r["receipt_candidate_id"] is None
    ]
    assert len(remit) == 1, f"expected one invoice-in-remittance row, got {remit}"
    r = remit[0]
    assert r["confidence"] == "very_high"
    assert r["decision_status"] == "proposed"
    assert "invoice_no_in_remittance:122064713086" in r["reason_codes"]
    assert "vendor:vodafone" in r["reason_codes"]


def test_vodafone_ignored_tx27_skipped(adapter) -> None:
    """TX 27 (Vodafone, ignored=true) must produce zero matcher writes,
    even though its remittance carries an invoice number the Vodafone
    rule otherwise picks up."""
    run_matcher(adapter)
    rows = _proposed_for(adapter, tx_id=27)
    assert rows == []


# ──────────────────────────────────────────────────────────────────────────
# Invariants
# ──────────────────────────────────────────────────────────────────────────

def test_matcher_never_writes_approved_or_sent_or_rejected(adapter) -> None:
    run_matcher(adapter)
    code_written = [m for m in adapter.matches if m.get("decided_by") == "code"]
    for m in code_written:
        assert m["decision_status"] in ("proposed", "manual_needed"), (
            f"matcher wrote disallowed state {m['decision_status']!r}: {m}"
        )


def test_matcher_decided_by_is_always_code(adapter) -> None:
    run_matcher(adapter)
    fresh = [m for m in adapter.matches if m.get("decided_by") != "legacy_outlook_rule"]
    assert fresh, "expected at least one matcher-written row"
    assert all(m["decided_by"] == "code" for m in fresh)


def test_matcher_is_idempotent(adapter) -> None:
    """Re-running with no new state must produce zero new rows."""
    first = run_matcher(adapter)
    rows_after_first = list(adapter.matches)

    second = run_matcher(adapter)
    assert second.inserted == 0, "idempotent re-run inserted rows: {second.inserted}"
    assert second.updated == 0, "idempotent re-run updated rows: {second.updated}"
    assert adapter.matches == rows_after_first, "match state changed on second run"


def test_matcher_updates_reason_codes_when_signals_change(adapter) -> None:
    """If a re-run yields different reason_codes for the same (tx, candidate)
    pair, the existing row is updated (not duplicated)."""
    run_matcher(adapter)
    # Mutate an existing matcher-written row's reason_codes to simulate drift.
    target = None
    for m in adapter.matches:
        if m.get("decided_by") == "code" and m["bank_tx_id"] == 56:
            target = m
            break
    assert target is not None
    target["reason_codes"] = ["stale"]

    summary = run_matcher(adapter)
    assert summary.inserted == 0
    assert summary.updated >= 1
    # And the canonical reason_codes are back.
    assert "invoice_no:B4373121" in target["reason_codes"]


# ──────────────────────────────────────────────────────────────────────────
# Sanity: Google ADS TX 83 (kanban) has no parser, so no proposal
# ──────────────────────────────────────────────────────────────────────────

def test_tx83_google_ads_no_proposal(adapter) -> None:
    run_matcher(adapter)
    rows = _proposed_for(adapter, tx_id=83)
    assert rows == []

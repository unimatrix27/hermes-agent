"""Offline tests for the finance toolbox (unimatrix27/ideas#23).

Every test runs against ``InMemoryToolAdapter`` + ``FakeGraphMailSender``
+ ``RecordingNotifier`` — no Postgres, no network, no Graph credentials.

Coverage is structured around the #23 acceptance checklist:

1. read verbs: shape + filter behavior, read-only invariant (row counts
   unchanged across calls)
2. write verbs: argument validation, state transitions, idempotency
3. ``mark_ignored`` hard guard against ``ignored=true → false``
4. ``send_match`` four-step pipeline:
   * happy path
   * step (a/b) failure → no belege_sent row, no status change
   * step (c) success + (d) failure → leave belege_sent, surface partial
   * second call on same match_id → existing row, no re-send
5. ``search_for_missing_receipt`` graceful-degradation path
6. concurrent ``approve_match`` calls produce a stable result, no
   duplicate writes
7. ``flag_anomaly`` / ``finalize_run`` write exactly one row each and
   never mutate other tables
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from finance.tools import (
    FakeGraphMailSender,
    InMemoryToolAdapter,
    RecordingNotifier,
    approve_match,
    finalize_run,
    flag_anomaly,
    get_proposals,
    get_run_history,
    get_tx_context,
    list_open_transactions,
    mark_ignored,
    mark_manual_needed,
    read_anomalies,
    reject_match,
    run_indexer,
    run_matcher,
    search_for_missing_receipt,
    send_match,
)
from finance.tools.adapter import InvalidTransition, NotFound, ToolError


# ──────────────────────────────────────────────────────────────────────
# Fixture factory
# ──────────────────────────────────────────────────────────────────────


def _make_adapter(tmp_path: Path) -> InMemoryToolAdapter:
    """Build an InMemoryToolAdapter seeded with a small, named scenario.

    Three transactions + three candidates + one already-sent legacy entry,
    chosen to cover every status bucket in tests:

    * tx 100 (Sipgate, non-ignored, no matches, > 14 days ago)  → 'missing'
    * tx 200 (Notion, non-ignored, one 'proposed' match)         → 'ambiguous'
    * tx 300 (Vodafone, ignored)                                 → 'ignored'
    """
    adapter = InMemoryToolAdapter()

    # Three transactions.
    adapter.transactions = [
        {
            "id": 100, "amount": 40.00, "signed_amount": -40.00, "currency": "EUR",
            "credit_debit": "D", "booking_date": date(2026, 2, 19),
            "counterparty_name": "SIPGATE",
            "remittance_information": "Sipgate / MCC: 4814", "ignored": False,
        },
        {
            "id": 200, "amount": 10.00, "signed_amount": -10.00, "currency": "EUR",
            "credit_debit": "D", "booking_date": date(2026, 3, 5),
            "counterparty_name": "NOTION LABS",
            "remittance_information": "Notion Labs", "ignored": False,
        },
        {
            "id": 300, "amount": 30.00, "signed_amount": -30.00, "currency": "EUR",
            "credit_debit": "D", "booking_date": date(2026, 4, 10),
            "counterparty_name": "Vodafone GmbH",
            "remittance_information": "Rechnungsnr: 122000000001", "ignored": True,
        },
    ]

    # Two ok candidates + one portal-required, plus a tiny on-disk PDF
    # so send_match has a real blob to read.
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    sipgate_pdf = blob_dir / "sipgate-B4373121.pdf"
    sipgate_pdf.write_bytes(b"%PDF-fake-bytes-sipgate-001\n")
    notion_pdf = blob_dir / "notion-ZWLWGPDN-0002.pdf"
    notion_pdf.write_bytes(b"%PDF-fake-bytes-notion-002\n")

    adapter.candidates = [
        {
            "id": 1001,
            "source_system": "graph",
            "mailbox": "rechnung@lineo.finance",
            "attachment_sha256": "sha-sipgate-001",
            "attachment_name": "sipgate-B4373121.pdf",
            "local_blob_path": str(sipgate_pdf),
            "extracted_text": "Sipgate invoice text " + ("x" * 3000),
            "extracted_json": {
                "vendor": "sipgate", "invoice_number": "B4373121",
                "gross_amount": 40.00, "invoice_date": "2026-02-19",
            },
            "parse_status": "ok",
        },
        {
            "id": 2001,
            "source_system": "graph",
            "mailbox": "rechnung@lineo.finance",
            "attachment_sha256": "sha-notion-002",
            "attachment_name": "notion-ZWLWGPDN-0002.pdf",
            "local_blob_path": str(notion_pdf),
            "extracted_text": "Notion invoice text",
            "extracted_json": {
                "vendor": "notion", "invoice_number": "ZWLWGPDN-0002",
                "gross_amount": 10.00, "invoice_date": "2026-03-05",
            },
            "parse_status": "ok",
        },
    ]

    # Seed one proposed match for tx 200 ↔ candidate 2001.
    adapter.insert_match(
        bank_tx_id=200,
        receipt_candidate_id=2001,
        match_type="exact_amount_date",
        confidence="high",
        decision_status="proposed",
        decided_by="code",
        reason_codes=["vendor:notion", "amount_eq:10.00", "date_within:0d"],
        legacy_meta={"origin": "matcher_test"},
    )

    return adapter


@pytest.fixture
def adapter(tmp_path: Path) -> InMemoryToolAdapter:
    return _make_adapter(tmp_path)


# ──────────────────────────────────────────────────────────────────────
# Read verbs — shape + filter + read-only invariant
# ──────────────────────────────────────────────────────────────────────


def _row_count_snapshot(adapter: InMemoryToolAdapter) -> tuple[int, ...]:
    return (
        len(adapter.transactions),
        len(adapter.candidates),
        len(adapter.matches),
        len(adapter.belege_sent),
        len(adapter.anomalies),
        len(adapter.reconcile_runs),
    )


def test_list_open_transactions_emits_six_bucket_shape(adapter):
    rows = list_open_transactions(adapter)
    by_id = {r["bank_tx_id"]: r for r in rows}
    assert by_id[100]["status"] == "missing"
    assert by_id[200]["status"] == "ambiguous"
    assert by_id[300]["status"] == "ignored"
    assert by_id[200]["proposed_count"] == 1
    assert by_id[100]["proposed_count"] == 0


def test_list_open_transactions_filters_by_month_vendor_status(adapter):
    only_march = list_open_transactions(adapter, month="2026-03")
    assert [r["bank_tx_id"] for r in only_march] == [200]

    only_vodafone = list_open_transactions(adapter, vendor="vodafone")
    assert [r["bank_tx_id"] for r in only_vodafone] == [300]

    only_missing = list_open_transactions(adapter, status="missing")
    assert [r["bank_tx_id"] for r in only_missing] == [100]


def test_read_verbs_are_read_only(adapter):
    snap = _row_count_snapshot(adapter)
    _ = list_open_transactions(adapter)
    _ = get_tx_context(adapter, 200)
    _ = get_proposals(adapter)
    _ = get_run_history(adapter)
    _ = read_anomalies(adapter)
    assert _row_count_snapshot(adapter) == snap


def test_get_tx_context_returns_match_candidate_and_excerpt(adapter):
    ctx = get_tx_context(adapter, 200, excerpt_chars=10)
    assert ctx["transaction"]["id"] == 200
    assert len(ctx["matches"]) == 1
    assert ctx["matches"][0]["decision_status"] == "proposed"
    assert len(ctx["candidates"]) == 1
    cand = ctx["candidates"][0]
    assert "extracted_text" not in cand
    assert "extracted_text_excerpt" in cand
    assert len(cand["extracted_text_excerpt"]) == 10


def test_get_tx_context_raises_not_found_for_missing_tx(adapter):
    with pytest.raises(NotFound):
        get_tx_context(adapter, 9999)


def test_get_proposals_filters_by_min_confidence(adapter):
    # Add a low + a very_high proposal so the filter has something to bite.
    adapter.insert_match(
        bank_tx_id=100, receipt_candidate_id=1001, match_type="exact_amount_date",
        confidence="low", decision_status="proposed", decided_by="code",
        reason_codes=["weak"], legacy_meta={},
    )
    adapter.insert_match(
        bank_tx_id=100, receipt_candidate_id=None, match_type="exact_invoice_number",
        confidence="very_high", decision_status="proposed", decided_by="code",
        reason_codes=["strong"], legacy_meta={"origin": "matcher_test_vh"},
    )
    high = get_proposals(adapter, min_confidence="high")
    assert all(p["confidence"] in ("high", "very_high") for p in high)
    very_high = get_proposals(adapter, min_confidence="very_high")
    assert all(p["confidence"] == "very_high" for p in very_high)


# ──────────────────────────────────────────────────────────────────────
# Write verbs — approve / reject / mark_manual_needed
# ──────────────────────────────────────────────────────────────────────


def test_approve_match_sets_decision_and_audit_trail(adapter):
    match_id = adapter.matches[0]["id"]
    row = approve_match(adapter, match_id, reason="amount + date + vendor", decided_by="llm")
    assert row["decision_status"] == "approved"
    assert row["decided_by"] == "llm"
    notes = row["legacy_meta"]["agent_notes"]
    assert notes[-1]["reason"] == "amount + date + vendor"
    assert notes[-1]["decision_status"] == "approved"


def test_approve_match_rejects_empty_reason(adapter):
    match_id = adapter.matches[0]["id"]
    with pytest.raises(ToolError):
        approve_match(adapter, match_id, reason="   ")


def test_approve_match_blocks_terminal_states(adapter):
    match_id = adapter.matches[0]["id"]
    # Force into 'sent' externally.
    adapter.matches[0]["decision_status"] = "sent"
    with pytest.raises(InvalidTransition):
        approve_match(adapter, match_id, reason="should fail")


def test_reject_match_sets_status(adapter):
    match_id = adapter.matches[0]["id"]
    row = reject_match(adapter, match_id, reason="bogus")
    assert row["decision_status"] == "rejected"


def test_mark_manual_needed_inserts_new_when_none(adapter):
    pre = len(adapter.matches)
    row = mark_manual_needed(adapter, 100, reason="vendor unknown", decided_by="llm")
    assert row["match_type"] == "manual_review_legacy"
    assert row["decision_status"] == "manual_needed"
    assert row["legacy_meta"]["origin"] == "tool_mark_manual_needed"
    assert len(adapter.matches) == pre + 1


def test_mark_manual_needed_updates_existing(adapter):
    first = mark_manual_needed(adapter, 100, reason="first reason")
    second = mark_manual_needed(adapter, 100, reason="second reason")
    assert second["id"] == first["id"]
    notes = second["legacy_meta"]["agent_notes"]
    assert notes[-1]["reason"] == "second reason"
    assert sum(1 for m in adapter.matches if m["bank_tx_id"] == 100 and m["decision_status"] == "manual_needed") == 1


def test_mark_manual_needed_raises_for_unknown_tx(adapter):
    with pytest.raises(NotFound):
        mark_manual_needed(adapter, 9999, reason="...")


# ──────────────────────────────────────────────────────────────────────
# mark_ignored — invariant: true → false is HARD ERROR
# ──────────────────────────────────────────────────────────────────────


def test_mark_ignored_sets_true_and_writes_audit(adapter):
    result = mark_ignored(adapter, 100, reason="internal transfer", decided_by="llm")
    assert result["transaction"]["ignored"] is True
    assert result["audit_match"]["decision_status"] == "ignored"
    assert result["audit_match"]["legacy_meta"]["agent_notes"][-1]["reason"] == "internal transfer"


def test_mark_ignored_rejects_already_ignored_row(adapter):
    # tx 300 is seeded ignored=true.
    with pytest.raises(InvalidTransition) as exc_info:
        mark_ignored(adapter, 300, reason="should fail")
    assert "ignored=true" in str(exc_info.value) or "true→false" in str(exc_info.value)
    # And no audit row was written.
    assert all(
        m.get("legacy_meta", {}).get("origin") != "tool_mark_ignored"
        for m in adapter.matches
    )


def test_mark_ignored_cannot_flip_true_to_false_via_any_path(adapter):
    # Belt-and-braces: even an explicit caller request to set ignored=False
    # has no API to do so — there is no mark_unignored verb. Confirming
    # via the adapter's lower-level invariant: set_transaction_ignored
    # only accepts expect_currently=False (i.e. flips to True).
    with pytest.raises(InvalidTransition):
        adapter.set_transaction_ignored(tx_id=300, expect_currently=False)


# ──────────────────────────────────────────────────────────────────────
# send_match — four-step pipeline
# ──────────────────────────────────────────────────────────────────────


def _approve_tx200_match(adapter: InMemoryToolAdapter) -> int:
    """Approve the seeded proposed match for tx 200 so send_match can run."""
    match_id = adapter.matches[0]["id"]
    approve_match(adapter, match_id, reason="approved for send test")
    return match_id


def test_send_match_happy_path(adapter):
    match_id = _approve_tx200_match(adapter)
    sender = FakeGraphMailSender()
    result = send_match(adapter, match_id, graph_sender=sender,
                       datev_recipient="datev@example.com",
                       source_mailbox="rechnung@lineo.finance")
    assert result["sent"] is True
    assert result["match_status_updated"] is True
    assert result["belege_sent"]["bank_tx_id"] == 200
    assert result["belege_sent"]["recipient"] == "datev@example.com"
    assert result["belege_sent"]["via"] == "agent_match"
    # match advanced to 'sent'.
    final = adapter.get_match(match_id)
    assert final["decision_status"] == "sent"
    # exactly one belege_sent row.
    assert len(adapter.belege_sent) == 1
    # Graph was called exactly once.
    assert len(sender.sent_calls) == 1


def test_send_match_idempotent_on_natural_key(adapter):
    match_id = _approve_tx200_match(adapter)
    sender = FakeGraphMailSender()
    first = send_match(adapter, match_id, graph_sender=sender,
                       datev_recipient="datev@example.com")
    # Second call must find the existing belege_sent row, NOT re-send.
    # Re-approve so decision_status is back in an approved-compatible state
    # (mirrors the "agent retries" scenario before discovering the dedup).
    adapter.matches[0]["decision_status"] = "approved"
    second = send_match(adapter, match_id, graph_sender=sender,
                        datev_recipient="datev@example.com")
    assert first["sent"] is True
    assert second["sent"] is True
    assert second.get("idempotent") is True
    assert second["belege_sent"]["id"] == first["belege_sent"]["id"]
    # Still exactly one belege_sent row.
    assert len(adapter.belege_sent) == 1
    # Only ONE Graph send, not two.
    assert len(sender.sent_calls) == 1


def test_send_match_step_a_failure_writes_no_belege_sent(adapter):
    match_id = _approve_tx200_match(adapter)
    sender = FakeGraphMailSender(fail_step_a=True, error_text="sendMail 503")
    result = send_match(adapter, match_id, graph_sender=sender)
    assert result["sent"] is False
    assert result["step"] == "sendMail"
    assert "503" in result["error"]
    assert len(adapter.belege_sent) == 0
    # decision_status unchanged.
    assert adapter.get_match(match_id)["decision_status"] == "approved"


def test_send_match_step_b_failure_writes_no_belege_sent(adapter):
    match_id = _approve_tx200_match(adapter)
    sender = FakeGraphMailSender(fail_step_b=True, error_text="sent-items missing")
    result = send_match(adapter, match_id, graph_sender=sender)
    assert result["sent"] is False
    assert result["step"] == "sent_items_lookup"
    assert len(adapter.belege_sent) == 0
    assert adapter.get_match(match_id)["decision_status"] == "approved"


def test_send_match_step_c_success_step_d_failure_keeps_belege_row(adapter):
    match_id = _approve_tx200_match(adapter)

    # Wrap the adapter so step (d) fails — patch update_match_decision
    # for the second 'expect_current_status=("approved",)' call only.
    original_update = adapter.update_match_decision
    call_log = {"updates": 0}

    def updating_with_failure(**kw):
        # The agent_note inserted by send_match's step (d) contains
        # "sent via send_match" — that's the call to fail. Any earlier
        # update (approval) passes through.
        if kw.get("decision_status") == "sent":
            call_log["updates"] += 1
            raise RuntimeError("simulated decision_status update failure")
        return original_update(**kw)

    adapter.update_match_decision = updating_with_failure  # type: ignore[assignment]

    sender = FakeGraphMailSender()
    result = send_match(adapter, match_id, graph_sender=sender)

    assert result["sent"] is True
    assert result["match_status_updated"] is False
    assert "decision_status update failed" in (result["warning"] or "")
    # belege_sent row was written.
    assert len(adapter.belege_sent) == 1
    # The match still shows 'approved' (we never advanced it).
    assert adapter.get_match(match_id)["decision_status"] == "approved"
    assert call_log["updates"] == 1


def test_send_match_rejects_non_approved_match(adapter):
    match_id = adapter.matches[0]["id"]  # 'proposed', not approved
    sender = FakeGraphMailSender()
    with pytest.raises(InvalidTransition):
        send_match(adapter, match_id, graph_sender=sender)


def test_send_match_raises_for_missing_match(adapter):
    sender = FakeGraphMailSender()
    with pytest.raises(NotFound):
        send_match(adapter, 99999, graph_sender=sender)


def test_send_match_refuses_synthetic_match_without_candidate(adapter):
    # Vodafone-style remittance-only match: no candidate id.
    row = adapter.insert_match(
        bank_tx_id=100, receipt_candidate_id=None, match_type="exact_invoice_number",
        confidence="very_high", decision_status="proposed", decided_by="code",
        reason_codes=["invoice_no_in_remittance:122..."], legacy_meta={"origin": "matcher_remittance_invoice"},
    )
    approve_match(adapter, row["id"], reason="approve synthetic")
    sender = FakeGraphMailSender()
    with pytest.raises(ToolError, match="no receipt_candidate_id"):
        send_match(adapter, row["id"], graph_sender=sender)


# ──────────────────────────────────────────────────────────────────────
# flag_anomaly + read_anomalies
# ──────────────────────────────────────────────────────────────────────


def test_flag_anomaly_writes_one_row(adapter):
    row = flag_anomaly(adapter, tx_id=100, reason="unclear vendor", severity="warn")
    assert row["severity"] == "warn"
    assert row["status"] == "open"
    assert row["bank_tx_id"] == 100
    assert len(adapter.anomalies) == 1


def test_flag_anomaly_rejects_bad_severity(adapter):
    with pytest.raises(ToolError):
        flag_anomaly(adapter, tx_id=100, reason="...", severity="critical")


def test_flag_anomaly_does_not_mutate_other_tables(adapter):
    snap_no_anom = (
        len(adapter.transactions),
        len(adapter.candidates),
        len(adapter.matches),
        len(adapter.belege_sent),
    )
    flag_anomaly(adapter, tx_id=200, reason="dup match suspected", severity="info")
    assert (
        len(adapter.transactions),
        len(adapter.candidates),
        len(adapter.matches),
        len(adapter.belege_sent),
    ) == snap_no_anom


def test_read_anomalies_filters_by_status_and_since(adapter):
    flag_anomaly(adapter, tx_id=100, reason="a", severity="info")
    flag_anomaly(adapter, tx_id=200, reason="b", severity="warn")
    adapter.anomalies[0]["status"] = "resolved"
    open_rows = read_anomalies(adapter, status="open")
    assert len(open_rows) == 1
    assert open_rows[0]["bank_tx_id"] == 200
    future = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert read_anomalies(adapter, since=future) == []


# ──────────────────────────────────────────────────────────────────────
# search_for_missing_receipt — graceful degradation
# ──────────────────────────────────────────────────────────────────────


def test_search_for_missing_receipt_graceful_when_no_skill(adapter):
    result = search_for_missing_receipt(100)
    assert result["candidates_proposed"] == []
    assert "hunter skill not registered" in result["notes"]
    assert result["skill_present"] is False
    assert result["tx_id"] == 100


def test_search_for_missing_receipt_dispatches_when_skill_present(adapter):
    class FakeHunter:
        def dispatch_find_missing_receipt(self, *, tx_id: int) -> dict[str, Any]:
            return {
                "candidates_proposed": [{"candidate_id": 7777, "confidence": "high"}],
                "notes": "fake hunter ok",
            }
    result = search_for_missing_receipt(100, skill_registry=FakeHunter())
    assert result["skill_present"] is True
    assert result["candidates_proposed"][0]["candidate_id"] == 7777


# ──────────────────────────────────────────────────────────────────────
# finalize_run — row + notifier
# ──────────────────────────────────────────────────────────────────────


def test_finalize_run_writes_row_and_notifies(adapter):
    n = RecordingNotifier()
    result = finalize_run(
        adapter,
        summary_md="# Run done\n* 3 sent, 1 flagged",
        proposed_changes={"add_vendor": "DKB"},
        notifier=n,
    )
    row = result["reconcile_run"]
    assert row["summary_md"].startswith("# Run done")
    assert row["proposed_changes"]["add_vendor"] == "DKB"
    assert result["notification_dispatched"] is True
    assert len(n.calls) == 1
    assert "Run done" in n.calls[0]["summary_md"]


def test_finalize_run_rejects_empty_summary(adapter):
    with pytest.raises(ToolError):
        finalize_run(adapter, summary_md="   ")


# ──────────────────────────────────────────────────────────────────────
# run_indexer + run_matcher (thin wrappers; verify they delegate)
# ──────────────────────────────────────────────────────────────────────


def test_run_indexer_delegates_to_indexer_run(adapter):
    captured: dict[str, Any] = {}

    class FakeAgg:
        def to_dict(self) -> dict[str, Any]:
            return {
                "scanned": 5, "new": 2, "dedup_skipped": 1,
                "portal_required": 0, "parse_failed": 2,
            }

    def fake_run(**kwargs):
        captured.update(kwargs)
        return FakeAgg()

    def cfg_factory():
        return object()

    out = run_indexer(
        mailbox="rechnung@lineo.finance", since=None,
        indexer_run=fake_run,
        indexer_adapter=object(), fetcher=object(), parser=object(),
        config_factory=cfg_factory,
    )
    assert out["scanned"] == 5
    assert out["dedup_skipped"] == 1
    assert "config" in captured
    assert "adapter" in captured
    assert "fetcher" in captured


def test_run_matcher_normalises_summary_keys(adapter):
    class Sum:
        inserted = 3
        updated = 1
        skipped_existing = 2
        txs_seen = 12
        txs_with_proposals = 4

    def fake_matcher_run(_a):
        return Sum()

    result = run_matcher(object(), matcher_run=fake_matcher_run)
    assert result == {
        "proposed_new": 3, "proposed_updated": 1,
        "skipped_existing": 2, "txs_seen": 12, "txs_with_proposals": 4,
    }


# ──────────────────────────────────────────────────────────────────────
# Concurrent approve_match safety
# ──────────────────────────────────────────────────────────────────────


def test_concurrent_approve_match_is_stable(adapter):
    match_id = adapter.matches[0]["id"]

    results: list[dict[str, Any]] = []
    errors: list[Exception] = []

    def worker():
        try:
            row = approve_match(adapter, match_id, reason="concurrent", decided_by="llm")
            results.append(row)
        except Exception as exc:  # noqa: BLE001 — we want to surface it
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # All callers see the same final status.
    assert all(r["decision_status"] == "approved" for r in results)
    # No duplicate row was created.
    assert sum(1 for m in adapter.matches if m["id"] == match_id) == 1
    # 8 audit notes appended, no row corruption.
    final = adapter.get_match(match_id)
    assert len(final["legacy_meta"]["agent_notes"]) == 8


# ──────────────────────────────────────────────────────────────────────
# Read-only invariant across the whole verb set
# ──────────────────────────────────────────────────────────────────────


def test_read_verbs_dont_mutate_belege_sent_or_transactions(adapter):
    tx_snapshot = [dict(t) for t in adapter.transactions]
    bs_snapshot = list(adapter.belege_sent)
    _ = list_open_transactions(adapter)
    _ = get_tx_context(adapter, 200)
    _ = get_proposals(adapter)
    _ = get_run_history(adapter)
    _ = read_anomalies(adapter)
    assert adapter.transactions == tx_snapshot
    assert adapter.belege_sent == bs_snapshot

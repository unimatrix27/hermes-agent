"""CLI compatibility tests for finance-reconcile-v2."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from finance.reconcile_v2 import cli
from finance.reconcile_v2.adapter import InMemoryAdapter


def _adapter() -> InMemoryAdapter:
    adapter = InMemoryAdapter()
    adapter.transactions.append({
        "id": 83,
        "amount": 146.31,
        "signed_amount": -146.31,
        "currency": "EUR",
        "credit_debit": "D",
        "booking_date": date(2026, 4, 2),
        "counterparty_name": "Google ADS8524834313",
        "remittance_information": "Google ADS8524834313 / MCC: 7311",
        "ignored": False,
    })
    adapter.anomalies.extend([
        {
            "id": 7,
            "bank_tx_id": 83,
            "reason": "needs human review",
            "severity": "warn",
            "status": "open",
            "created_at": datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc),
        },
        {
            "id": 8,
            "bank_tx_id": 83,
            "reason": "resolved older issue",
            "severity": "info",
            "status": "resolved",
            "created_at": datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc),
        },
    ])
    adapter.reconcile_runs.extend([
        {
            "id": 1,
            "started_at": datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc),
            "finalized_at": datetime(2026, 4, 5, 9, 1, tzinfo=timezone.utc),
            "summary_md": "March run",
            "notes": json.dumps({"month_scope": "2026-03"}),
            "invoked_by": "cron",
            "created_at": datetime(2026, 4, 5, 9, 1, tzinfo=timezone.utc),
        },
        {
            "id": 2,
            "started_at": datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
            "finalized_at": datetime(2026, 5, 5, 9, 1, tzinfo=timezone.utc),
            "summary_md": "April run",
            "notes": json.dumps({"month_scope": "2026-04"}),
            "invoked_by": "cron",
            "created_at": datetime(2026, 5, 5, 9, 1, tzinfo=timezone.utc),
        },
    ])
    return adapter


@pytest.fixture()
def offline_cli(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(cli, "_connect", lambda: object())
    monkeypatch.setattr(cli, "PostgresAdapter", lambda conn: adapter)
    return adapter


def _run_and_load(argv, capsys):
    rc = cli.main(argv)
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    return json.loads(captured.out)


def test_get_tx_context_accepts_v1_tx_id_flag(offline_cli, capsys):
    payload = _run_and_load(["get_tx_context", "--tx-id", "83", "--no-inbox"], capsys)

    assert payload["transaction"]["id"] == 83
    assert [a["id"] for a in payload["anomalies"]] == [7, 8]


def test_read_anomalies_lists_open_items_by_default(offline_cli, capsys):
    payload = _run_and_load(["read_anomalies"], capsys)

    assert [a["id"] for a in payload["anomalies"]] == [7]


def test_read_anomalies_accepts_status_and_tx_id_filters(offline_cli, capsys):
    payload = _run_and_load(["read_anomalies", "--status", "resolved", "--tx-id", "83"], capsys)

    assert [a["id"] for a in payload["anomalies"]] == [8]


def test_get_run_history_filters_by_month_scope_in_notes(offline_cli, capsys):
    payload = _run_and_load(["get_run_history", "--month", "2026-04"], capsys)

    assert [r["id"] for r in payload["runs"]] == [2]
    assert payload["runs"][0]["tool_call_summary"]["month_scope"] == "2026-04"

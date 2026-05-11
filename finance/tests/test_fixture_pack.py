"""Presence tests for the finance reconciliation fixture pack.

Per unimatrix27/ideas#24 the goal is for the harness to load the fixtures
without error. The real parser / matcher tests ship with #22.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "finance"

PDF_FIXTURES = [
    ("sipgate", "B4373121"),
    ("sipgate", "B4411208"),
    ("sipgate", "B4459838"),
    ("notion", "ZWLWGPDN-0002"),
    ("lucky_penny", "6945-10683"),
    ("lucky_penny", "CN-6945-10021"),
    ("vodafone", "122203440401"),
    ("vodafone", "portal_notification_2026_04"),
]

NAMED_TX_IDS = [1, 5, 20, 27, 31, 39, 53, 56, 66, 68, 88]
META_KEYS = {"from", "subject", "received_at", "attachment_name", "internet_message_id"}


@pytest.mark.parametrize("vendor,name", PDF_FIXTURES)
def test_pdf_fixture_present_and_nonempty(vendor: str, name: str) -> None:
    txt = FIXTURE_ROOT / vendor / f"{name}.txt"
    meta = FIXTURE_ROOT / vendor / f"{name}.meta.json"
    assert txt.is_file(), f"missing {txt}"
    assert meta.is_file(), f"missing {meta}"
    body = txt.read_text(encoding="utf-8")
    assert len(body.strip()) > 50, f"{txt} is suspiciously short ({len(body)} chars)"


@pytest.mark.parametrize("vendor,name", PDF_FIXTURES)
def test_meta_json_shape(vendor: str, name: str) -> None:
    meta = json.loads((FIXTURE_ROOT / vendor / f"{name}.meta.json").read_text(encoding="utf-8"))
    assert set(meta.keys()) == META_KEYS, f"meta keys mismatch for {vendor}/{name}: {set(meta)}"


def test_invoice_pdf_texts_contain_invoice_number() -> None:
    # Only invoice-style PDFs need to mention their invoice number near the top.
    # The Vodafone "portal_notification_2026_04" body intentionally has no invoice number.
    for vendor, name in PDF_FIXTURES:
        if name.startswith("portal_notification_"):
            continue
        body = (FIXTURE_ROOT / vendor / f"{name}.txt").read_text(encoding="utf-8")
        head = body[:1500]
        assert name in head, f"{vendor}/{name}: invoice number not found near top of .txt"


def test_transactions_jsonl_has_named_ids() -> None:
    rows = [
        json.loads(line)
        for line in (FIXTURE_ROOT / "transactions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = {r["id"] for r in rows}
    missing = set(NAMED_TX_IDS) - ids
    assert not missing, f"transactions.jsonl missing named TX ids: {missing}"


def test_transactions_iban_is_redacted() -> None:
    rows = [
        json.loads(line)
        for line in (FIXTURE_ROOT / "transactions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for r in rows:
        iban = r.get("counterparty_iban")
        if iban is not None:
            assert iban == "DE**", (
                f"counterparty_iban not redacted on TX {r['id']}: {iban!r}"
            )


def test_beleg_match_samples_include_all_manual_review() -> None:
    rows = [
        json.loads(line)
        for line in (FIXTURE_ROOT / "beleg_match_samples.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    manual_review = [r for r in rows if (r["beleg_match"] or {}).get("via") == "manual_review"]
    assert len(manual_review) == 3, (
        f"expected 3 manual_review rows verbatim, got {len(manual_review)}"
    )
    # The Google Ads / kanban-task row is one of them.
    assert any((r["beleg_match"] or {}).get("kanban_task") == "t_51751302"
               for r in manual_review), "kanban_task='t_51751302' row missing from manual_review pack"


def test_belege_sent_samples_cover_all_via() -> None:
    rows = [
        json.loads(line)
        for line in (FIXTURE_ROOT / "belege_sent_samples.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    via_values = {r["via"] for r in rows}
    expected = {"outlook_auto_rule", "manual_inbox_match", "agent_match", "manual"}
    assert expected.issubset(via_values), f"missing via values: {expected - via_values}"
    null_tx = sum(1 for r in rows if r.get("bank_tx_id") is None)
    with_att = sum(1 for r in rows if (r.get("attachment_filenames") or []))
    assert null_tx >= 2, f"need >=2 rows with bank_tx_id IS NULL, got {null_tx}"
    assert with_att >= 2, f"need >=2 rows with non-empty attachment_filenames, got {with_att}"

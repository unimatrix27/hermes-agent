"""Vendor-parser unit tests for unimatrix27/ideas#22.

Each parser is exercised against the real fixture pack (#24). Failed-parse
shape is exercised separately to enforce the "honest tool boundaries" rule
from #27.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance.parsers import detect_vendor, parse

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "finance"


def _load(vendor: str, name: str) -> dict:
    txt = (FIXTURE_ROOT / vendor / f"{name}.txt").read_text(encoding="utf-8")
    meta = json.loads((FIXTURE_ROOT / vendor / f"{name}.meta.json").read_text(encoding="utf-8"))
    return {
        "extracted_text": txt,
        "from_email": meta["from"],
        "subject": meta["subject"],
        "attachment_name": meta["attachment_name"],
        "internet_message_id": meta["internet_message_id"],
        "received_at": meta["received_at"],
    }


# ──────────────────────────────────────────────────────────────────────────
# Sipgate
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "name,inv_date,gross",
    [
        ("B4373121", "2026-02-19", 40.00),
        ("B4411208", "2026-03-01", 55.00),
        ("B4459838", "2026-04-01", 55.00),
    ],
)
def test_sipgate_invoice(name: str, inv_date: str, gross: float) -> None:
    got = parse(_load("sipgate", name))
    assert got is not None
    assert got["vendor"] == "sipgate"
    assert got["parse_status"] == "ok"
    assert got["invoice_number"] == name
    assert got["invoice_date"] == inv_date
    assert got["gross_amount"] == pytest.approx(gross)
    assert got["currency"] == "EUR"
    assert got["direction"] == "payment"


# ──────────────────────────────────────────────────────────────────────────
# Notion
# ──────────────────────────────────────────────────────────────────────────

def test_notion_zwlwgpdn_0002() -> None:
    got = parse(_load("notion", "ZWLWGPDN-0002"))
    assert got is not None
    assert got["vendor"] == "notion"
    assert got["parse_status"] == "ok"
    assert got["invoice_number"] == "ZWLWGPDN-0002"
    assert got["invoice_date"] == "2026-04-04"
    assert got["gross_amount"] == pytest.approx(444.91)
    assert got["currency"] == "EUR"
    assert got["direction"] == "payment"


# ──────────────────────────────────────────────────────────────────────────
# Lucky Penny / Paddle
# ──────────────────────────────────────────────────────────────────────────

def test_lucky_penny_invoice() -> None:
    got = parse(_load("lucky_penny", "6945-10683"))
    assert got is not None
    assert got["vendor"] == "lucky_penny"
    assert got["parse_status"] == "ok"
    assert got["invoice_number"] == "6945-10683"
    assert got["gross_amount"] == pytest.approx(59.50)
    assert got["direction"] == "payment"
    assert got["period_start"] == "2026-01-20"
    assert got["period_end"] == "2026-02-20"


def test_lucky_penny_credit_note() -> None:
    got = parse(_load("lucky_penny", "CN-6945-10021"))
    assert got is not None
    assert got["vendor"] == "lucky_penny"
    assert got["parse_status"] == "ok"
    assert got["invoice_number"] == "CN-6945-10021"
    assert got["refunded_invoice_number"] == "6945-10683"
    assert got["gross_amount"] == pytest.approx(9.50)
    assert got["direction"] == "refund"


# ──────────────────────────────────────────────────────────────────────────
# Vodafone
# ──────────────────────────────────────────────────────────────────────────

def test_vodafone_pdf_invoice() -> None:
    got = parse(_load("vodafone", "122203440401"))
    assert got is not None
    assert got["vendor"] == "vodafone"
    assert got["parse_status"] == "ok"
    assert got["invoice_number"] == "122203440401"
    assert got["customer_number"] == "120113676"
    assert got["gross_amount"] == pytest.approx(58.55)
    assert got["currency"] == "EUR"
    assert got["direction"] == "payment"


def test_vodafone_portal_notification() -> None:
    got = parse(_load("vodafone", "portal_notification_2026_04"))
    assert got is not None
    assert got["vendor"] == "vodafone"
    assert got["parse_status"] == "portal_required"
    assert "MeinVodafone" in got["parse_error"]
    # Date and amount come from the body even without a PDF.
    assert got["invoice_date"] == "2026-04-14"
    assert got["gross_amount"] == pytest.approx(58.55)


# ──────────────────────────────────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────────────────────────────────

def test_dispatch_returns_none_for_unknown_vendor() -> None:
    got = parse({
        "extracted_text": "Hello, this is some unrelated mail.",
        "from_email": "stranger@example.com",
        "subject": "Newsletter",
        "attachment_name": "",
        "internet_message_id": "<x@example.com>",
    })
    assert got is None


def test_detect_vendor_by_sender_domain() -> None:
    assert detect_vendor("", {"from": "team@sipgate.de"}) == "sipgate"
    assert detect_vendor("", {"from": "billing@makenotion.com"}) == "notion"
    assert detect_vendor("", {"from": "noreply@paddle.com"}) == "lucky_penny"
    assert detect_vendor("", {"from": "nicht.antworten@kundenservice.vodafone.com"}) == "vodafone"


def test_parser_failure_returns_failed_status() -> None:
    """A Sipgate-looking mail with garbage body still yields a structured error."""
    got = parse({
        "extracted_text": "sipgate GmbH support@support.sipgate.de\n(no usable invoice fields)\n",
        "from_email": "team@sipgate.de",
        "subject": "Neue Rechnung",
        "attachment_name": "invoice_sipgatede_xxxx.pdf",
        "internet_message_id": "<x@sipgate>",
    })
    assert got is not None
    assert got["parse_status"] == "failed"
    assert got["vendor"] == "sipgate"
    assert "parse_error" in got

"""Paddle / Lucky Penny invoice + credit-note parser.

Two shapes share most of the layout:
  - Tax invoice (`Tax invoice PAID`): carries `Invoice reference: 6945-10683`
    and a `Total` line. Direction is `payment`.
  - Credit note (`Credit note`): carries `Credit note reference: CN-6945-10021`
    and `Refunded invoice reference: 6945-10683`. Direction is `refund`.

Both formats include a `Billing period:` line (invoices) or implicit period
in the product description (credit notes).
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping, Optional

INVOICE_REF_RE = re.compile(r"Invoice reference:\s*(\S+)")
CN_REF_RE = re.compile(r"Credit note reference:\s*(\S+)")
REFUNDED_REF_RE = re.compile(r"Refunded invoice reference:\s*(\S+)")
TOTAL_RE = re.compile(r"\nTotal\s*\n€\s*([0-9]+\.[0-9]{2})")
AMOUNT_PAID_RE = re.compile(r"Amount paid\s*\n€\s*([0-9]+\.[0-9]{2})")
TOTAL_REFUNDED_RE = re.compile(r"Total refunded\s*\n€\s*([0-9]+\.[0-9]{2})")
BILLING_PERIOD_RE = re.compile(
    r"Billing period:\s*"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})\s*-\s*"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})"
)

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}


def _parse_period(text: str) -> tuple[date, date] | None:
    m = BILLING_PERIOD_RE.search(text)
    if not m:
        return None
    try:
        start = date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
        end = date(int(m.group(6)), MONTHS[m.group(5)], int(m.group(4)))
    except (KeyError, ValueError):
        return None
    return start, end


def parse(extracted_text: str, email_meta: Mapping[str, Any]) -> Optional[dict]:
    if not extracted_text:
        return None

    is_credit_note = "Credit note" in extracted_text and "Tax invoice" not in extracted_text

    if is_credit_note:
        m = CN_REF_RE.search(extracted_text)
        if not m:
            return {
                "vendor": "lucky_penny",
                "parse_status": "failed",
                "parse_error": "no credit note reference found",
            }
        invoice_no = m.group(1)
        refunded = REFUNDED_REF_RE.search(extracted_text)
        if not refunded:
            return {
                "vendor": "lucky_penny",
                "parse_status": "failed",
                "parse_error": f"credit note {invoice_no} missing refunded invoice reference",
            }
        amt = TOTAL_REFUNDED_RE.search(extracted_text)
        if not amt:
            return {
                "vendor": "lucky_penny",
                "parse_status": "failed",
                "parse_error": f"credit note {invoice_no} missing refunded amount",
            }
        period = _parse_period(extracted_text)
        return {
            "vendor": "lucky_penny",
            "parse_status": "ok",
            "invoice_number": invoice_no,
            "refunded_invoice_number": refunded.group(1),
            "gross_amount": float(amt.group(1)),
            "currency": "EUR",
            "direction": "refund",
            "period_start": period[0].isoformat() if period else None,
            "period_end": period[1].isoformat() if period else None,
        }

    m = INVOICE_REF_RE.search(extracted_text)
    if not m:
        return {
            "vendor": "lucky_penny",
            "parse_status": "failed",
            "parse_error": "no invoice reference found",
        }
    invoice_no = m.group(1)
    amt = AMOUNT_PAID_RE.search(extracted_text) or TOTAL_RE.search(extracted_text)
    if not amt:
        return {
            "vendor": "lucky_penny",
            "parse_status": "failed",
            "parse_error": f"invoice {invoice_no} missing total amount",
        }
    period = _parse_period(extracted_text)
    return {
        "vendor": "lucky_penny",
        "parse_status": "ok",
        "invoice_number": invoice_no,
        "gross_amount": float(amt.group(1)),
        "currency": "EUR",
        "direction": "payment",
        "period_start": period[0].isoformat() if period else None,
        "period_end": period[1].isoformat() if period else None,
    }

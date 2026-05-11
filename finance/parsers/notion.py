"""Notion Labs invoice parser.

Bank remittance is `NOTION LABS, INC. / MCC: 7372` — no invoice number, so
PDF parse is mandatory. The invoice text has `Invoice number ZWLWGPDN-0002`
on a single line, dates in `April 4, 2026` form, and an `Amount due €444.91`
line near the bottom.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping, Optional

INVOICE_NO_RE = re.compile(r"Invoice number\s+([A-Z0-9]+-\d+)")
# Notion uses "April 4, 2026" — month name in English.
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{1,2}),\s+(\d{4})"
)
AMOUNT_DUE_RE = re.compile(r"Amount due\s*\n?\s*€\s*([0-9]+\.[0-9]{2})", re.IGNORECASE)
TOTAL_RE = re.compile(r"Total\s*\n?\s*€\s*([0-9]+\.[0-9]{2})", re.IGNORECASE)

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}


def _english_date(text: str) -> date | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    return date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))


def parse(extracted_text: str, email_meta: Mapping[str, Any]) -> Optional[dict]:
    if not extracted_text:
        return None

    m = INVOICE_NO_RE.search(extracted_text)
    if not m:
        return {
            "vendor": "notion",
            "parse_status": "failed",
            "parse_error": "no invoice number found",
        }
    invoice_no = m.group(1)

    # The first English-date occurrence is "Date of issue" → invoice date.
    invoice_date = _english_date(extracted_text)

    m = AMOUNT_DUE_RE.search(extracted_text) or TOTAL_RE.search(extracted_text)
    if not m:
        return {
            "vendor": "notion",
            "parse_status": "failed",
            "parse_error": f"no amount due found for invoice {invoice_no}",
        }
    gross = float(m.group(1))

    return {
        "vendor": "notion",
        "parse_status": "ok",
        "invoice_number": invoice_no,
        "invoice_date": invoice_date.isoformat() if invoice_date else None,
        "gross_amount": gross,
        "currency": "EUR",
        "direction": "payment",
    }

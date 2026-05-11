"""Sipgate invoice parser.

Sipgate's MCC-4814 charges carry no invoice number in the bank remittance
(just `SIPGATE / MCC: 4814`), so the PDF parse is the only path to the
invoice number. Layout is stable across the fixture pack: the words
`Rechnungsnummer`, `Rechnungsdatum`, and `Rechnungsbetrag` each appear on
their own line, with the value on the line immediately after.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping, Optional

INVOICE_NO_RE = re.compile(r"\bB\d{7}\b")
DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
# "40,00 EUR"
EUR_AMOUNT_RE = re.compile(r"([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2})\s*EUR")


def _german_decimal_to_float(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def _value_after_label(lines: list[str], label: str) -> Optional[str]:
    """Sipgate's layout has a label line followed by its value line."""
    for i, line in enumerate(lines):
        if line.strip() == label and i + 1 < len(lines):
            return lines[i + 1].strip()
    return None


def parse(extracted_text: str, email_meta: Mapping[str, Any]) -> Optional[dict]:
    if not extracted_text:
        return None
    lines = extracted_text.splitlines()

    invoice_no = _value_after_label(lines, "Rechnungsnummer")
    if invoice_no is None or not INVOICE_NO_RE.fullmatch(invoice_no):
        m = INVOICE_NO_RE.search(extracted_text)
        if m is None:
            return {
                "vendor": "sipgate",
                "parse_status": "failed",
                "parse_error": "no invoice number found",
            }
        invoice_no = m.group(0)

    invoice_date_str = _value_after_label(lines, "Rechnungsdatum")
    invoice_date: date | None = None
    if invoice_date_str:
        m = DATE_RE.fullmatch(invoice_date_str.strip())
        if m:
            d, mo, y = map(int, m.groups())
            invoice_date = date(y, mo, d)

    # Amount: line after "Rechnungsbetrag" looks like "40,00 EUR".
    gross_str = _value_after_label(lines, "Rechnungsbetrag")
    gross: float | None = None
    if gross_str:
        m = EUR_AMOUNT_RE.search(gross_str)
        if m:
            gross = _german_decimal_to_float(m.group(1))
    if gross is None:
        return {
            "vendor": "sipgate",
            "parse_status": "failed",
            "parse_error": f"no gross amount found for invoice {invoice_no}",
        }

    return {
        "vendor": "sipgate",
        "parse_status": "ok",
        "invoice_number": invoice_no,
        "invoice_date": invoice_date.isoformat() if invoice_date else None,
        "gross_amount": gross,
        "currency": "EUR",
        "direction": "payment",
    }

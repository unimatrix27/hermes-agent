"""VM Finovia / DATEV-originated invoice parser."""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping, Optional


_AMOUNT_RE = re.compile(r"Rechnungsbetrag\s+von\s+([0-9.]+,[0-9]{2})\s+EUR", re.I)
_INVOICE_RE = re.compile(r"\b(20\d{2}/\d{2,})\b")
_DATE_RE = re.compile(r"Rechnungsdatum:\s*(\d{1,2})\.(\d{1,2})\.(20\d{2})", re.I)


def _parse_de_amount(value: str) -> float:
    return float(value.replace(".", "").replace(",", "."))


def _parse_de_date(match: re.Match[str]) -> str:
    day, month, year = match.groups()
    return date(int(year), int(month), int(day)).isoformat()


def parse(extracted_text: str, email_meta: Mapping[str, Any]) -> Optional[dict]:
    text = extracted_text or ""
    subject = email_meta.get("subject") or ""
    haystack = f"{subject}\n{text}"
    if "finovia" not in haystack.lower() and "vm-finovia" not in haystack.lower():
        return None

    invoice_match = _INVOICE_RE.search(haystack)
    amount_match = _AMOUNT_RE.search(text)
    date_match = _DATE_RE.search(text)

    if not invoice_match:
        return {
            "vendor": "finovia",
            "parse_status": "failed",
            "parse_error": "finovia invoice number not found",
        }
    if not amount_match:
        return {
            "vendor": "finovia",
            "invoice_number": invoice_match.group(1),
            "parse_status": "failed",
            "parse_error": "finovia gross amount not found",
        }

    out: dict[str, Any] = {
        "vendor": "finovia",
        "parse_status": "ok",
        "invoice_number": invoice_match.group(1),
        "gross_amount": _parse_de_amount(amount_match.group(1)),
        "currency": "EUR",
        "direction": "payment",
    }
    if date_match:
        out["invoice_date"] = _parse_de_date(date_match)
    return out

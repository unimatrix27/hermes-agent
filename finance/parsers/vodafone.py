"""Vodafone mobile invoice + portal-notification parser.

Two shapes:
  - PDF invoice: contains `Rechnungs-Nummer:` and `Kunden-Nummer:` labels,
    line-broken so the number sits a line or two after the label. Gross
    amount appears as `Bruttorechnungsbetrag` (a label line followed by an
    amount line) or — as in the real Vodafone layout — as the largest
    `Summe ... EUR` block at the bottom of page 1.
  - Portal notification (`MeinVodafone`): no PDF, no invoice number. The
    only structured field we can lift is the bill date from
    "Deine Rechnung vom DD.MM.YYYY findest Du in Deinem persönlichen
    Service-Portal MeinVodafone." We mark parse_status='portal_required'
    so the matcher knows to write a manual_needed / portal_only row.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping, Optional

INVOICE_NO_RE = re.compile(r"\b1\d{11}\b")  # Vodafone invoice numbers are 12-digit, lead with 12
KDNR_RE = re.compile(r"\b\d{9}\b")
EUR_AMOUNT_RE = re.compile(r"([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2})\s*EUR")
PORTAL_DATE_RE = re.compile(
    r"Deine Rechnung vom\s+(\d{2})\.(\d{2})\.(\d{4})\s+findest Du in Deinem persönlichen Service-Portal MeinVodafone"
)
PORTAL_FALLBACK_RE = re.compile(
    r"Rechnung vom\s+(\d{2})\.(\d{2})\.(\d{4})"
)
PORTAL_AMOUNT_RE = re.compile(r"Summe beträgt\s+([0-9]+,[0-9]{2})\s+Euro")


def _german_decimal_to_float(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def _is_portal_notification(text: str) -> bool:
    return "MeinVodafone" in text and "Rechnungs-Nummer" not in text


def _parse_portal(text: str, email_meta: Mapping[str, Any]) -> dict:
    out: dict[str, Any] = {
        "vendor": "vodafone",
        "parse_status": "portal_required",
        "parse_error": "Vodafone portal notification — invoice PDF only available via MeinVodafone",
        "direction": "payment",
        "currency": "EUR",
    }
    m = PORTAL_DATE_RE.search(text) or PORTAL_FALLBACK_RE.search(text)
    if m:
        d, mo, y = map(int, m.groups())
        out["invoice_date"] = date(y, mo, d).isoformat()
    m = PORTAL_AMOUNT_RE.search(text)
    if m:
        out["gross_amount"] = _german_decimal_to_float(m.group(1))
    return out


def _parse_pdf(text: str, email_meta: Mapping[str, Any]) -> dict:
    lines = text.splitlines()

    invoice_no: str | None = None
    customer_no: str | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "Rechnungs-Nummer:":
            for nxt in lines[i + 1 : i + 4]:
                m = INVOICE_NO_RE.search(nxt)
                if m:
                    invoice_no = m.group(0)
                    break
        elif stripped == "Kunden-Nummer:":
            for nxt in lines[i + 1 : i + 4]:
                m = KDNR_RE.search(nxt)
                if m:
                    customer_no = m.group(0)
                    break

    if invoice_no is None:
        m = INVOICE_NO_RE.search(text)
        if m:
            invoice_no = m.group(0)
    if invoice_no is None:
        return {
            "vendor": "vodafone",
            "parse_status": "failed",
            "parse_error": "no invoice number found in PDF",
        }

    gross: float | None = None
    # Vodafone's "Höhe von X,XX EUR buchen wir am ..." line is unambiguous —
    # it's the actual direct-debit amount Vodafone charges. Prefer it.
    m = re.search(r"Höhe von\s+([0-9]+,[0-9]{2})\s+EUR", text)
    if m:
        gross = _german_decimal_to_float(m.group(1))
    else:
        # Fallback: under the "Bruttorechnungsbetrag" block the three EUR
        # amounts are netto / USt / brutto in that order — brutto is the third.
        for i, line in enumerate(lines):
            if line.strip() == "Bruttorechnungsbetrag":
                amounts: list[float] = []
                for nxt in lines[i + 1 : i + 10]:
                    am = EUR_AMOUNT_RE.search(nxt)
                    if am:
                        amounts.append(_german_decimal_to_float(am.group(1)))
                    if len(amounts) >= 3:
                        break
                if len(amounts) >= 3:
                    gross = amounts[2]
                elif amounts:
                    gross = amounts[-1]
                break

    out = {
        "vendor": "vodafone",
        "parse_status": "ok",
        "invoice_number": invoice_no,
        "currency": "EUR",
        "direction": "payment",
    }
    if customer_no:
        out["customer_number"] = customer_no
    if gross is not None:
        out["gross_amount"] = gross
    else:
        out["parse_status"] = "failed"
        out["parse_error"] = f"no gross amount found for invoice {invoice_no}"
    return out


def parse(extracted_text: str, email_meta: Mapping[str, Any]) -> Optional[dict]:
    if not extracted_text:
        return None
    if _is_portal_notification(extracted_text):
        return _parse_portal(extracted_text, email_meta)
    return _parse_pdf(extracted_text, email_meta)

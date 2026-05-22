"""Vendor parsers for finance reconciliation (unimatrix27/ideas#22).

Parsers are pure functions: (extracted_text, email_metadata) -> extracted_json.
Output dict either carries `parse_status='ok'` with structured fields, or
`parse_status='failed'`/`'portal_required'` with a `parse_error` string —
never silently produces wrong data (per #27's honest-tool-boundaries rule).

Dispatch order: sender domain first, subject heuristics second, then text
fingerprints. Unknown vendors → None (the matcher will log and skip).
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from . import finovia, lucky_penny, notion, sipgate, vodafone

VendorParser = Callable[[str, Mapping[str, Any]], Optional[dict]]

VENDOR_PARSERS: dict[str, VendorParser] = {
    "sipgate": sipgate.parse,
    "notion": notion.parse,
    "lucky_penny": lucky_penny.parse,
    "vodafone": vodafone.parse,
    "finovia": finovia.parse,
}


def detect_vendor(extracted_text: str, email_meta: Mapping[str, Any]) -> str | None:
    """Pick a vendor key from sender/subject/text. None = unknown."""
    sender = (email_meta.get("from") or "").lower()
    subject = (email_meta.get("subject") or "").lower()
    attachment = (email_meta.get("attachment_name") or "").lower()
    text = extracted_text or ""

    if "sipgate.de" in sender or "sipgate" in subject or "sipgate" in attachment.lower():
        return "sipgate"
    if "sipgate gmbh" in text.lower():
        return "sipgate"

    if "makenotion.com" in sender or "notion" in subject.lower() or "notion-invoice" in attachment:
        return "notion"
    if "notion labs" in text.lower():
        return "notion"

    if "paddle" in sender or "lucky-penny" in attachment or "luckypenny" in attachment:
        return "lucky_penny"
    if "lucky penny software" in text.lower() or "via paddle.com" in text.lower():
        return "lucky_penny"

    if "vodafone" in sender or "vodafone" in subject or "vodafone" in attachment:
        return "vodafone"
    if "vodafone gmbh" in text.lower() or "meinvodafone" in text.lower():
        return "vodafone"

    if "finovia" in sender or "finovia" in subject or "finovia" in attachment:
        return "finovia"
    if "vm finovia" in text.lower() or "vm-finovia" in text.lower():
        return "finovia"

    return None


def parse(candidate: Mapping[str, Any]) -> dict | None:
    """Single dispatch entrypoint.

    A `candidate` is a mapping with at minimum:
      - extracted_text: str
      - and any of: from_email, subject, attachment_name, internet_message_id.

    Returns the extracted_json dict the matcher consumes, or None when the
    vendor is unknown. On a recognised vendor whose parse fails, returns
    {'parse_status': 'failed', 'parse_error': ..., 'vendor': vendor}.
    """
    text = candidate.get("extracted_text") or ""
    meta = {
        "from": candidate.get("from_email") or candidate.get("from"),
        "subject": candidate.get("subject"),
        "attachment_name": candidate.get("attachment_name"),
        "internet_message_id": candidate.get("internet_message_id"),
        "received_at": candidate.get("received_at"),
    }
    vendor = detect_vendor(text, meta)
    if vendor is None:
        return None
    parser = VENDOR_PARSERS[vendor]
    result = parser(text, meta)
    if result is None:
        return {
            "vendor": vendor,
            "parse_status": "failed",
            "parse_error": f"{vendor} parser returned no result",
        }
    return result


__all__ = ["parse", "detect_vendor", "VENDOR_PARSERS"]

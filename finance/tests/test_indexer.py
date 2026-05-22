"""Indexer unit tests for unimatrix27/ideas#21.

Every test runs offline against:

* a ``FakeGraphClient`` standing in for ``tools.microsoft_graph_client``
  (only the three methods the indexer calls are faked: ``get_json``,
  ``collect_paginated``, ``download_to_file``);
* an ``InMemoryIndexerAdapter`` (mirrors the ``MatcherAdapter`` pattern
  from PR #3) replacing the Postgres adapter.

PDF binaries are never shipped. Where a test needs a real PDF round-trip,
it generates a tiny single-page PDF with ``pymupdf`` from a slice of the
fixture text — the fixture ``.txt`` files from PR #2 are still the
ground-truth anchor for "what extracted_text should look like".
"""
from __future__ import annotations

import asyncio
import base64
from email.message import EmailMessage
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from finance.indexer import (
    DEFAULT_PORTAL_REQUIRED_SENDERS,
    GraphFetcher,
    IndexerConfig,
    InMemoryIndexerAdapter,
    LocalBlobBackend,
    extract_pdf_text,
    run,
    _is_delta_token_expired,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "finance"
SIPGATE_TXT = (FIXTURE_ROOT / "sipgate" / "B4373121.txt").read_text(encoding="utf-8")
VODAFONE_NOTIFICATION_TXT = (
    FIXTURE_ROOT / "vodafone" / "portal_notification_2026_04.txt"
).read_text(encoding="utf-8")
VODAFONE_NOTIFICATION_META = json.loads(
    (FIXTURE_ROOT / "vodafone" / "portal_notification_2026_04.meta.json").read_text(
        encoding="utf-8"
    )
)


# ──────────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────────


def _build_sipgate_pdf(tmp_path: Path, *, name: str = "synthetic-B4373121.pdf") -> bytes:
    """Tiny pymupdf-generated PDF carrying a slice of B4373121.txt.

    The synthetic content is just enough for the real Sipgate parser to
    recognise the invoice (Rechnungsnummer / Rechnungsdatum / Rechnungsbetrag
    plus the vendor marker). Shipping a real PDF would violate the public-fork
    fixture policy from #24.
    """
    snippet = (
        "sipgate GmbH\n"
        "Gladbacher Str. 74, 40219 Düsseldorf\n"
        "Rechnungsdatum\n19.02.2026\n"
        "Leistungsdatum\n19.02.2026\n"
        "Rechnungsnummer\nB4373121\n"
        "Rechnungsbetrag\n40,00 EUR\n"
    )
    doc = pymupdf.open()
    page = doc.new_page()
    rect = pymupdf.Rect(40, 40, 560, 800)
    page.insert_textbox(rect, snippet, fontsize=10)
    out = tmp_path / name
    doc.save(str(out))
    doc.close()
    return out.read_bytes()


def _build_blank_pdf(tmp_path: Path) -> bytes:
    """A PDF with no text — exercises the pymupdf-yields-no-content branch."""
    doc = pymupdf.open()
    doc.new_page()
    p = tmp_path / "blank.pdf"
    doc.save(str(p))
    doc.close()
    return p.read_bytes()


def _build_finovia_pdf(tmp_path: Path, *, invoice: str = "2026/1285") -> bytes:
    snippet = (
        "Lineo Finance GmbH\n"
        "VM Finovia GmbH Steuer- und Rechtsberatung\n"
        "RECHNUNG\n"
        f"{invoice}\n"
        "Rechnungsdatum: 30.04.2026\n"
        "April 2026 Management Fee Pauschale 19,00 10.000,00\n"
        "Per SEPA-Lastschrift wird der Rechnungsbetrag von 11.923,80 EUR "
        "zum Mandat 211190000001 abgebucht.\n"
    )
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(40, 40, 560, 800), snippet, fontsize=10)
    out = tmp_path / "RE_2026_1285.pdf"
    doc.save(str(out))
    doc.close()
    return out.read_bytes()


def _wrap_pdf_in_smime_p7m(pdf_bytes: bytes, *, filename: str = "RE_2026/1285.pdf") -> bytes:
    msg = EmailMessage()
    msg.set_content("signed DATEV invoice container")
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)
    return msg.as_bytes()


def _make_msg(
    *,
    msg_id: str,
    internet_message_id: str,
    sender: str,
    subject: str = "",
    has_attachments: bool = True,
    received: str = "2026-04-15T08:00:00Z",
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "internetMessageId": internet_message_id,
        "from": {"emailAddress": {"address": sender, "name": sender}},
        "subject": subject,
        "receivedDateTime": received,
        "hasAttachments": has_attachments,
        "parentFolderId": "inbox",
    }


class FakeGraphAPIError(Exception):
    """Shape-compatible stand-in for MicrosoftGraphAPIError used in tests."""

    def __init__(self, status: int, *, message: str = "", payload: Any | None = None) -> None:
        super().__init__(f"fake-graph {status}: {message}")
        self.status_code = status
        self.payload = payload or {"error": {"code": "fake", "message": message}}


class FakeGraphClient:
    """Implements the three methods the indexer's GraphFetcher uses.

    Mirrors the upstream MicrosoftGraphClient's surface narrowly so the
    indexer's pagination, delta-token recovery, dedupe, and download paths
    can all be exercised offline.
    """

    def __init__(self) -> None:
        # FIFO queues — each delta call pops the next response. Use either
        # dicts (pages) or exceptions (typically FakeGraphAPIError(410)).
        self.delta_responses: list[Any] = []
        self.attachments_by_message: dict[str, list[dict[str, Any]]] = {}
        self.full_messages: dict[str, dict[str, Any]] = {}
        # (message_id, attachment_id) -> raw bytes
        self.download_bodies: dict[tuple[str, str], bytes] = {}

        self.delta_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.download_calls: list[tuple[str, str]] = []

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if "/messages/delta" in path or "deltatoken" in path.lower():
            self.delta_calls.append((path, params))
            if not self.delta_responses:
                raise AssertionError("FakeGraphClient: no more delta responses queued")
            resp = self.delta_responses.pop(0)
            if isinstance(resp, BaseException):
                raise resp
            return resp
        # Single-message fetch (body lookup)
        for msg_id, msg in self.full_messages.items():
            if path.endswith(f"/messages/{msg_id}"):
                return msg
        raise AssertionError(f"FakeGraphClient: unexpected get_json path {path}")

    async def collect_paginated(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        m = re.search(r"/messages/([^/]+)/attachments", path)
        if m:
            return list(self.attachments_by_message.get(m.group(1), []))
        return []

    async def download_to_file(
        self,
        path: str,
        destination: Path,
        *,
        headers: dict[str, str] | None = None,
        chunk_size: int = 65536,
    ) -> dict[str, Any]:
        m = re.search(r"/messages/([^/]+)/attachments/([^/]+)/\$value", path)
        if not m:
            raise AssertionError(f"FakeGraphClient: unexpected download path {path}")
        mid, aid = m.group(1), m.group(2)
        self.download_calls.append((mid, aid))
        body = self.download_bodies[(mid, aid)]
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return {
            "path": str(dest),
            "size_bytes": len(body),
            "content_type": "application/pdf",
        }


def _make_fetcher(client: FakeGraphClient) -> GraphFetcher:
    return GraphFetcher(client)


def _make_config(tmp_path: Path, *, mailboxes: list[str] | None = None) -> IndexerConfig:
    return IndexerConfig(
        mailboxes=mailboxes or ["rechnung@lineo.finance"],
        blob_backend=LocalBlobBackend(tmp_path / "blobs"),
        blob_tmp_root=tmp_path / "tmp",
    )


# ──────────────────────────────────────────────────────────────────────────
# Sanity: fixture anchor matches what we expect to see in the wild
# ──────────────────────────────────────────────────────────────────────────


def test_fixture_anchor_b4373121_carries_invoice_number():
    """The synthetic PDF round-trip must produce text that's substantively
    aligned with the real fixture (PR #2's B4373121.txt). We don't byte-match
    the fixture — pymupdf reflow differs — but the invoice number and vendor
    marker must survive round-trip so the Sipgate parser fires."""
    assert "Rechnungsnummer" in SIPGATE_TXT
    assert "B4373121" in SIPGATE_TXT
    assert "sipgate GmbH" in SIPGATE_TXT


# ──────────────────────────────────────────────────────────────────────────
# 1) PDF text extraction (pymupdf path, with fixture as the anchor)
# ──────────────────────────────────────────────────────────────────────────


def test_extract_pdf_text_pymupdf_path(tmp_path: Path):
    """pymupdf is the default extractor; round-trip a slice of B4373121.txt
    through a generated PDF and verify the invoice number survives."""
    _build_sipgate_pdf(tmp_path)
    text, err = extract_pdf_text(tmp_path / "synthetic-B4373121.pdf")
    assert err is None
    assert text is not None
    assert "B4373121" in text
    assert "sipgate" in text.lower()


def test_extract_pdf_text_returns_none_when_pymupdf_yields_no_content(tmp_path: Path):
    """Per #27's honest-tool-boundaries rule: ambiguous extraction must
    return (None, reason), not a confident guess. marker-pdf is gated."""
    _build_blank_pdf(tmp_path)
    text, err = extract_pdf_text(tmp_path / "blank.pdf")
    assert text is None
    assert err is not None and "below" in err


# ──────────────────────────────────────────────────────────────────────────
# 2) End-to-end pymupdf → parser dispatch → candidate row
# ──────────────────────────────────────────────────────────────────────────


def test_pdf_attachment_writes_one_candidate_per_attachment(tmp_path: Path):
    pdf_bytes = _build_sipgate_pdf(tmp_path)
    pdf_sha = __import__("hashlib").sha256(pdf_bytes).hexdigest()
    msg = _make_msg(
        msg_id="msg-1",
        internet_message_id="<sipgate-B4373121@example>",
        sender="team@sipgate.de",
        subject="Deine sipgate-Rechnung B4373121",
    )

    client = FakeGraphClient()
    client.delta_responses = [{"value": [msg], "@odata.deltaLink": "https://x?$deltatoken=tok1"}]
    client.attachments_by_message["msg-1"] = [
        {
            "id": "att-1",
            "name": "rechnung-B4373121.pdf",
            "contentType": "application/pdf",
            "size": len(pdf_bytes),
        }
    ]
    client.download_bodies[("msg-1", "att-1")] = pdf_bytes

    adapter = InMemoryIndexerAdapter()
    config = _make_config(tmp_path)

    agg = run(
        config=config,
        adapter=adapter,
        fetcher=_make_fetcher(client),
    )

    assert agg.scanned == 1
    assert agg.new == 1
    assert agg.dedup_skipped == 0
    candidate = adapter.candidates[0]
    assert candidate["attachment_sha256"] == pdf_sha
    assert candidate["mailbox"] == "rechnung@lineo.finance"
    assert candidate["source_system"] == "graph"
    # Real Sipgate parser fired on the round-tripped text.
    assert candidate["extracted_json"].get("vendor") == "sipgate"
    assert candidate["parse_status"] == "ok"
    assert candidate["extracted_json"].get("invoice_number") == "B4373121"
    # State persisted, delta token captured.
    state = adapter.get_state("rechnung@lineo.finance::inbox")
    assert state is not None and state.delta_token == "tok1"


def test_smime_p7m_attachment_is_flattened_to_inner_finovia_pdf(tmp_path: Path):
    """DATEV e-invoice mail may expose only smime.p7m at Graph's top level.
    The indexer must flatten that wrapper and index the real inner PDF so the
    matcher can propose the Finovia transaction without manual intervention.
    """
    pdf_bytes = _build_finovia_pdf(tmp_path)
    p7m_bytes = _wrap_pdf_in_smime_p7m(pdf_bytes)
    msg = _make_msg(
        msg_id="finovia-msg-1285",
        internet_message_id="<finovia-1285@example>",
        sender="e-invoice@datev.de",
        subject="VM Finovia GmbH Steuer- und Rechtsberatung: Ihre Rechnung 2026/1285 vom 30.04.2026",
    )

    client = FakeGraphClient()
    client.delta_responses = [{"value": [msg], "@odata.deltaLink": "https://x?$deltatoken=tok-finovia"}]
    client.attachments_by_message["finovia-msg-1285"] = [
        {
            "id": "smime-1",
            "name": "smime.p7m",
            "contentType": "multipart/signed",
            "size": len(p7m_bytes),
        }
    ]
    client.download_bodies[("finovia-msg-1285", "smime-1")] = p7m_bytes

    adapter = InMemoryIndexerAdapter()
    agg = run(config=_make_config(tmp_path), adapter=adapter, fetcher=_make_fetcher(client))

    assert agg.scanned == 1
    assert agg.new == 1
    assert agg.parse_failed == 0
    candidate = adapter.candidates[0]
    assert candidate["attachment_name"] == "RE_2026/1285.pdf"
    assert candidate["parse_status"] == "ok"
    assert candidate["extracted_json"]["vendor"] == "finovia"
    assert candidate["extracted_json"]["invoice_number"] == "2026/1285"
    assert candidate["extracted_json"]["gross_amount"] == 11923.80


# ──────────────────────────────────────────────────────────────────────────
# 3) Attachment SHA dedupe across two runs
# ──────────────────────────────────────────────────────────────────────────


def test_attachment_sha_dedupe_across_runs(tmp_path: Path):
    pdf_bytes = _build_sipgate_pdf(tmp_path)
    # First run: original message
    msg_first = _make_msg(
        msg_id="msg-1",
        internet_message_id="<sipgate-B4373121@example>",
        sender="team@sipgate.de",
    )
    # Second run: the same PDF arrives again under a different message id
    # and internet_message_id (forwarded thread). The SHA-256 of the
    # attachment is identical → must not produce a second candidate row.
    msg_second = _make_msg(
        msg_id="msg-2",
        internet_message_id="<forwarded@example>",
        sender="finance@somewhere",
    )

    adapter = InMemoryIndexerAdapter()
    config = _make_config(tmp_path)

    # First indexer pass
    client1 = FakeGraphClient()
    client1.delta_responses = [
        {"value": [msg_first], "@odata.deltaLink": "https://x?$deltatoken=tok1"}
    ]
    client1.attachments_by_message["msg-1"] = [
        {
            "id": "att-1",
            "name": "rechnung-B4373121.pdf",
            "contentType": "application/pdf",
            "size": len(pdf_bytes),
        }
    ]
    client1.download_bodies[("msg-1", "att-1")] = pdf_bytes
    run(config=config, adapter=adapter, fetcher=_make_fetcher(client1))

    assert len(adapter.candidates) == 1

    # Second indexer pass — same PDF, new message
    client2 = FakeGraphClient()
    client2.delta_responses = [
        {"value": [msg_second], "@odata.deltaLink": "https://x?$deltatoken=tok2"}
    ]
    client2.attachments_by_message["msg-2"] = [
        {
            "id": "att-9",
            "name": "FWD rechnung-B4373121.pdf",
            "contentType": "application/pdf",
            "size": len(pdf_bytes),
        }
    ]
    client2.download_bodies[("msg-2", "att-9")] = pdf_bytes
    agg2 = run(config=config, adapter=adapter, fetcher=_make_fetcher(client2))

    assert len(adapter.candidates) == 1, "SHA dedupe must keep one row"
    assert agg2.dedup_skipped == 1
    assert agg2.new == 0


# ──────────────────────────────────────────────────────────────────────────
# 4) Delta-token expiry recovery (410 / invalid-token)
# ──────────────────────────────────────────────────────────────────────────


def test_is_delta_token_expired_classifier():
    assert _is_delta_token_expired(410, "Gone")
    assert _is_delta_token_expired(400, "syncStateNotFound")
    assert _is_delta_token_expired(400, "Invalid delta token")
    assert not _is_delta_token_expired(200, "ok")
    assert not _is_delta_token_expired(429, "Throttled")


def test_delta_token_expiry_recovery(tmp_path: Path):
    """Mailbox has a persisted delta_token. Graph rejects it with 410.
    The indexer must:
      1) detect the expiry,
      2) fall back to a fresh delta call (no token),
      3) re-walk messages, de-duping any already-indexed by internet_message_id.
    """
    pdf_bytes = _build_sipgate_pdf(tmp_path)
    msg = _make_msg(
        msg_id="msg-1",
        internet_message_id="<sipgate-B4373121@example>",
        sender="team@sipgate.de",
    )

    adapter = InMemoryIndexerAdapter()
    # Pre-existing state: indexer believes it has a valid delta token AND has
    # already indexed this message from a prior run (so re-walking must not
    # double-insert). State is keyed by mailbox::folder composite per the
    # folder-scoped delta requirement.
    adapter.upsert_state(
        "rechnung@lineo.finance::inbox",
        delta_token="stale-token",
        last_run_at=datetime.now(timezone.utc),
        last_summary={},
    )
    adapter.insert_candidate(
        source_system="graph",
        mailbox="rechnung@lineo.finance",
        outlook_message_id="msg-1",
        internet_message_id="<sipgate-B4373121@example>",
        received_at=None,
        from_email="team@sipgate.de",
        subject="",
        attachment_name="rechnung-B4373121.pdf",
        attachment_sha256=__import__("hashlib").sha256(pdf_bytes).hexdigest(),
        local_blob_path=str(tmp_path / "blobs" / "old.pdf"),
        text_sha256=None,
        extracted_text=None,
        extracted_json={"vendor": "sipgate", "parse_status": "ok"},
        parse_status="ok",
        parse_error=None,
    )
    candidates_before = len(adapter.candidates)

    client = FakeGraphClient()
    # First call (with stale token) → 410.
    # Second call (recovery, no token) → fresh page with the same message.
    client.delta_responses = [
        FakeGraphAPIError(410, message="syncStateNotFound"),
        {"value": [msg], "@odata.deltaLink": "https://x?$deltatoken=fresh"},
    ]
    client.attachments_by_message["msg-1"] = [
        {
            "id": "att-1",
            "name": "rechnung-B4373121.pdf",
            "contentType": "application/pdf",
            "size": len(pdf_bytes),
        }
    ]
    client.download_bodies[("msg-1", "att-1")] = pdf_bytes

    agg = run(
        config=_make_config(tmp_path),
        adapter=adapter,
        fetcher=_make_fetcher(client),
    )

    # Recovery happened; new token persisted.
    assert agg.mailboxes[0].delta_reset is True
    assert agg.mailboxes[0].delta_reset_reason is not None
    state = adapter.get_state("rechnung@lineo.finance::inbox")
    assert state is not None and state.delta_token == "fresh"
    # The re-walked message was the one we already had → dedupe via
    # internet_message_id keeps the candidate count flat.
    assert len(adapter.candidates) == candidates_before
    # The second delta call carried no $deltatoken (used $filter / fresh).
    second_call_params = client.delta_calls[1][1] or {}
    assert "$deltatoken" not in second_call_params


# ──────────────────────────────────────────────────────────────────────────
# 5) Vodafone notification → portal_required
# ──────────────────────────────────────────────────────────────────────────


def test_vodafone_portal_required_path(tmp_path: Path):
    """A mail from a configured portal_required sender with no PDF
    attachment lands as parse_status='portal_required'. Body content is the
    real Vodafone notification phrasing from PR #2's fixture pack — proves
    we're not anchoring on stale wording."""
    assert "Deine Rechnung" in VODAFONE_NOTIFICATION_TXT
    assert "Service-Portal MeinVodafone" in VODAFONE_NOTIFICATION_TXT
    sender = VODAFONE_NOTIFICATION_META["from"]
    assert any(s in sender for s in DEFAULT_PORTAL_REQUIRED_SENDERS)

    msg = _make_msg(
        msg_id="vmsg-1",
        internet_message_id=VODAFONE_NOTIFICATION_META["internet_message_id"],
        sender=sender,
        subject=VODAFONE_NOTIFICATION_META["subject"],
        has_attachments=False,
    )

    client = FakeGraphClient()
    client.delta_responses = [
        {"value": [msg], "@odata.deltaLink": "https://x?$deltatoken=vodatoken"}
    ]
    # Full-body fetch returns the real fixture text wrapped as Graph would.
    client.full_messages["vmsg-1"] = {
        **msg,
        "body": {
            "contentType": "html",
            "content": (
                "<html><body>"
                "Deine Rechnung vom 14.04.2026 findest Du in Deinem "
                "persönlichen Service-Portal MeinVodafone."
                "</body></html>"
            ),
        },
    }

    adapter = InMemoryIndexerAdapter()
    agg = run(
        config=_make_config(tmp_path),
        adapter=adapter,
        fetcher=_make_fetcher(client),
    )

    assert agg.scanned == 1
    assert agg.new == 1
    assert agg.portal_required == 1
    assert agg.parse_failed == 0
    cand = adapter.candidates[0]
    assert cand["parse_status"] == "portal_required"
    assert cand["source_system"] == "graph"
    assert cand["attachment_name"] is None
    assert cand["extracted_json"]["vendor"] == "vodafone"


# ──────────────────────────────────────────────────────────────────────────
# 6) Coexistence with outlook_auto_rule (legacy belege_sent)
# ──────────────────────────────────────────────────────────────────────────


def test_coexistence_with_belege_sent_skips_indexing(tmp_path: Path):
    """A delta-pulled message whose internet_message_id is already in
    bank.belege_sent (one of the 163 server-side forwards) must NOT produce
    a new receipt_candidates row — the legacy forward is the source of
    truth for "this went to DATEV."""
    pdf_bytes = _build_sipgate_pdf(tmp_path)
    legacy_imid = "<legacy-already-forwarded@example>"
    msg = _make_msg(
        msg_id="msg-legacy",
        internet_message_id=legacy_imid,
        sender="team@sipgate.de",
    )

    adapter = InMemoryIndexerAdapter()
    adapter.belege_sent_imids.add(legacy_imid)

    client = FakeGraphClient()
    client.delta_responses = [
        {"value": [msg], "@odata.deltaLink": "https://x?$deltatoken=tok"}
    ]
    # If the indexer ignored the belege_sent dedupe and proceeded, the
    # attachments call below would fire — verify it does not (no entry).
    client.attachments_by_message["msg-legacy"] = [
        {"id": "att", "name": "x.pdf", "contentType": "application/pdf"}
    ]
    client.download_bodies[("msg-legacy", "att")] = pdf_bytes

    agg = run(
        config=_make_config(tmp_path),
        adapter=adapter,
        fetcher=_make_fetcher(client),
    )

    assert agg.scanned == 1
    assert agg.new == 0
    assert adapter.candidates == []
    assert client.download_calls == [], "must not download attachments for belege_sent dupes"


# ──────────────────────────────────────────────────────────────────────────
# 7) Idempotency — second run with no new mail writes nothing
# ──────────────────────────────────────────────────────────────────────────


def test_idempotency_no_new_mail_zero_writes(tmp_path: Path):
    """Re-running the indexer when the delta is empty produces zero new
    candidate rows and only updates indexer_state's last_run_at/summary."""
    adapter = InMemoryIndexerAdapter()
    client = FakeGraphClient()
    # Empty delta page — no new messages.
    client.delta_responses = [
        {"value": [], "@odata.deltaLink": "https://x?$deltatoken=tok-empty"}
    ]
    agg = run(
        config=_make_config(tmp_path),
        adapter=adapter,
        fetcher=_make_fetcher(client),
    )
    assert agg.scanned == 0
    assert agg.new == 0
    assert agg.portal_required == 0
    assert agg.parse_failed == 0
    assert adapter.candidates == []
    state = adapter.get_state("rechnung@lineo.finance::inbox")
    assert state is not None and state.delta_token == "tok-empty"

    # Second run, still empty — no candidate rows, state still updates.
    client2 = FakeGraphClient()
    client2.delta_responses = [
        {"value": [], "@odata.deltaLink": "https://x?$deltatoken=tok-still-empty"}
    ]
    agg2 = run(
        config=_make_config(tmp_path),
        adapter=adapter,
        fetcher=_make_fetcher(client2),
    )
    assert agg2.scanned == 0 and agg2.new == 0
    assert adapter.candidates == []

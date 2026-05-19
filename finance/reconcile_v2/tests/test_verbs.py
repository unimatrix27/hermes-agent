"""Offline tests for the v2 reconcile toolbox (closes unimatrix27/ideas#31).

Every test runs against ``InMemoryAdapter`` + ``FakeInboxClient`` +
``FakeMailSender`` + ``RecordingNotifier`` — no Postgres, no network.

Coverage:

1. ``list_open_txs`` filters by ignored + belege_sent + ignore_rules
2. ``get_tx_context`` returns DB + auto-search bundle
3. ``search_inbox`` refuses zero-filter calls; vendor/date/amount work
4. ``send_beleg`` happy path + idempotency + step-a failure + dedup-link / conflict
5. ``mark_ignored`` blocks the true→true and true→false transitions
6. ``finalize_run`` writes one row and dispatches notification
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from finance.reconcile_v2 import (
    FakeInboxClient,
    FakeMailSender,
    InMemoryAdapter,
    InvalidTransition,
    MailAttachment,
    MailMessage,
    NotFound,
    RecordingNotifier,
    ToolError,
    finalize_run,
    get_tx_context,
    list_open_txs,
    mark_ignored,
    search_inbox,
    send_beleg,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


def _make_adapter() -> InMemoryAdapter:
    a = InMemoryAdapter()
    a.transactions = [
        {  # open: no belege_sent, not ignored, April 2026
            "id": 100, "amount": 39.99, "signed_amount": -39.99, "currency": "EUR",
            "credit_debit": "D", "booking_date": date(2026, 4, 10),
            "counterparty_name": "Vodafone GmbH",
            "counterparty_iban": "DE12 5001 0517 0000 0000 11",
            "remittance_information": "Rechnung 122000000001",
            "ignored": False,
        },
        {  # already sent
            "id": 200, "amount": 12.00, "signed_amount": -12.00, "currency": "EUR",
            "credit_debit": "D", "booking_date": date(2026, 4, 12),
            "counterparty_name": "NOTION LABS",
            "counterparty_iban": None,
            "remittance_information": "Notion subscription",
            "ignored": False,
        },
        {  # already ignored
            "id": 300, "amount": 1500.00, "signed_amount": -1500.00, "currency": "EUR",
            "credit_debit": "D", "booking_date": date(2026, 4, 15),
            "counterparty_name": "Lohn Stuecker",
            "counterparty_iban": "DE89 3704 0044 0532 0130 00",
            "remittance_information": "Gehalt April",
            "ignored": True,
        },
        {  # different month — should not appear when month filter set
            "id": 400, "amount": 8.00, "signed_amount": -8.00, "currency": "EUR",
            "credit_debit": "D", "booking_date": date(2026, 3, 28),
            "counterparty_name": "Sipgate Wireless",
            "counterparty_iban": None,
            "remittance_information": "Sipgate Mar26",
            "ignored": False,
        },
    ]
    # seed a belege_sent row for tx 200
    a.belege_sent.append({
        "id": 1, "bank_tx_id": 200, "attachment_filenames": ["notion.pdf"],
        "outlook_message_id": "AAMkSeed-200", "internet_message_id": None,
        "source_mailbox": "rechnung@lineo.finance",
        "sent_at": datetime(2026, 4, 12, 9, 0, tzinfo=timezone.utc),
        "recipient": "x@datev", "subject": "seeded",
        "via": "agent_match", "bank_tx_amount": 12.00,
        "bank_tx_booking_date": date(2026, 4, 12),
        "confidence": None, "reasoning": "seed",
        "created_at": datetime(2026, 4, 12, 9, 1, tzinfo=timezone.utc),
    })
    a._next_belege_id = 2
    return a


def _empty_rules_path(tmp_path: Path) -> Path:
    p = tmp_path / "ignore_rules.md"
    p.write_text("# empty rules\n", encoding="utf-8")
    return p


# ──────────────────────────────────────────────────────────────────────
# 1. list_open_txs
# ──────────────────────────────────────────────────────────────────────


def test_list_open_txs_filters_ignored_and_sent(tmp_path):
    a = _make_adapter()
    result = list_open_txs(a, ignore_rules_path=_empty_rules_path(tmp_path))
    ids = sorted(r["id"] for r in result["open"])
    assert ids == [100, 400], result  # 200 sent, 300 ignored
    assert result["ignore_rules_count"] == 0
    assert result["would_ignore"] == []


def test_list_open_txs_applies_month_filter(tmp_path):
    a = _make_adapter()
    out = list_open_txs(a, month="2026-04",
                       ignore_rules_path=_empty_rules_path(tmp_path))
    assert [r["id"] for r in out["open"]] == [100]


def test_list_open_txs_applies_ignore_rules(tmp_path):
    a = _make_adapter()
    rules = tmp_path / "ignore_rules.md"
    rules.write_text(
        "# test rules\n"
        "verwendungszweck:Sipgate → recurring infra; reviewed quarterly\n",
        encoding="utf-8",
    )
    out = list_open_txs(a, ignore_rules_path=rules)
    open_ids = [r["id"] for r in out["open"]]
    would_ids = [r["tx"]["id"] for r in out["would_ignore"]]
    assert open_ids == [100]
    assert would_ids == [400]
    assert "recurring infra" in out["would_ignore"][0]["reason"]


# ──────────────────────────────────────────────────────────────────────
# 2. get_tx_context
# ──────────────────────────────────────────────────────────────────────


def test_get_tx_context_bundles_db_and_inbox(tmp_path):
    a = _make_adapter()
    pdf = tmp_path / "vodafone.pdf"
    pdf.write_bytes(b"%PDF-fake")
    inbox = FakeInboxClient(messages_by_mailbox={
        "rechnung@lineo.finance": [MailMessage(
            outlook_message_id="MSG-1", internet_message_id="<imid-1>",
            mailbox="rechnung@lineo.finance",
            from_address="billing@vodafone.de",
            subject="Ihre Vodafone Rechnung",
            received_at=datetime(2026, 4, 11, 8, 0, tzinfo=timezone.utc),
            body_text="Rechnungsbetrag 39,99 EUR",
            has_attachments=True,
            attachments=[MailAttachment(
                name="vodafone.pdf", content_type="application/pdf",
                size_bytes=12, sha256="sha-1", local_path=str(pdf),
                extracted_text="Rechnungsbetrag 39,99 EUR",
            )],
        )],
    })
    ctx = get_tx_context(a, 100, inbox=inbox)
    assert ctx["transaction"]["id"] == 100
    assert ctx["belege_sent"] == []  # tx 100 not sent yet
    assert ctx["anomalies"] == []
    assert len(ctx["likely_mails"]) == 1
    assert ctx["likely_mails"][0]["outlook_message_id"] == "MSG-1"
    # the search was scoped, not full-inbox
    call = inbox.search_calls[0]
    assert call["vendor"] == "Vodafone"  # legal-suffix stripped
    assert call["amount"] == 39.99
    assert call["date_window"] is not None


def test_get_tx_context_unknown_tx():
    a = _make_adapter()
    with pytest.raises(NotFound):
        get_tx_context(a, 9999)


# ──────────────────────────────────────────────────────────────────────
# 3. search_inbox
# ──────────────────────────────────────────────────────────────────────


def test_search_inbox_refuses_zero_filter():
    inbox = FakeInboxClient(messages_by_mailbox={})
    with pytest.raises(ToolError):
        search_inbox(inbox=inbox)


def test_search_inbox_vendor_and_date_window():
    inbox = FakeInboxClient(messages_by_mailbox={
        "rechnung@lineo.finance": [
            MailMessage(
                outlook_message_id="A", internet_message_id=None,
                mailbox="rechnung@lineo.finance",
                from_address="x@vodafone.de", subject="Vodafone Rechnung",
                received_at=datetime(2026, 4, 5, tzinfo=timezone.utc),
                body_text="Rechnung 39,99 EUR", has_attachments=False,
            ),
            MailMessage(
                outlook_message_id="B", internet_message_id=None,
                mailbox="rechnung@lineo.finance",
                from_address="other@example.com", subject="Promo",
                received_at=datetime(2026, 4, 5, tzinfo=timezone.utc),
                body_text="Discount inside", has_attachments=False,
            ),
        ],
    })
    out = search_inbox(
        inbox=inbox, vendor="Vodafone",
        date_from=date(2026, 4, 1), date_to=date(2026, 4, 30),
    )
    assert [m["outlook_message_id"] for m in out] == ["A"]


def test_search_inbox_amount_match():
    inbox = FakeInboxClient(messages_by_mailbox={
        "rechnung@lineo.finance": [
            MailMessage(
                outlook_message_id="A", internet_message_id=None,
                mailbox="rechnung@lineo.finance",
                from_address="x@vodafone.de", subject="Vodafone Rechnung",
                received_at=datetime(2026, 4, 5, tzinfo=timezone.utc),
                body_text="Total: 39,99 EUR", has_attachments=False,
            ),
            MailMessage(
                outlook_message_id="B", internet_message_id=None,
                mailbox="rechnung@lineo.finance",
                from_address="x@vodafone.de", subject="Other Vodafone bill",
                received_at=datetime(2026, 4, 5, tzinfo=timezone.utc),
                body_text="Total: 12,00 EUR", has_attachments=False,
            ),
        ],
    })
    out = search_inbox(inbox=inbox, vendor="Vodafone", amount=39.99)
    assert [m["outlook_message_id"] for m in out] == ["A"]


def test_search_inbox_date_pair_requires_both():
    inbox = FakeInboxClient(messages_by_mailbox={})
    with pytest.raises(ToolError):
        search_inbox(inbox=inbox, vendor="X", date_from=date(2026, 4, 1))


def test_search_inbox_swallows_graph_failures_returns_empty(caplog):
    """A blowing-up InboxClient (e.g. transient Graph 5xx, expired
    token) must not surface as an uncaught traceback; the verb logs and
    returns [] to mirror get_tx_context's auto-search policy.
    """
    class _Boom:
        def search(self, **kwargs):
            raise RuntimeError("Graph messages query returned 503: b'busy'")

    with caplog.at_level("WARNING", logger="finance.reconcile_v2.verbs"):
        out = search_inbox(inbox=_Boom(), vendor="Vodafone")
    assert out == []
    assert any("Graph search failed" in rec.getMessage() for rec in caplog.records)


def test_search_inbox_passes_through_value_error():
    """ValueError from the inbox client (filter-shape problem) is a
    caller bug, not a transient outage — let it bubble.
    """
    class _ValueErrorInbox:
        def search(self, **kwargs):
            raise ValueError("bad filter combo")

    with pytest.raises(ValueError):
        search_inbox(inbox=_ValueErrorInbox(), vendor="X")


# ──────────────────────────────────────────────────────────────────────
# Token-provider selection (auth boundary)
# ──────────────────────────────────────────────────────────────────────


def test_default_token_provider_prefers_app_only(monkeypatch):
    """When MSGRAPH_* creds are present, the shared app-only
    provider wins — we do not silently fall through to the delegated
    bundle.
    """
    from finance.reconcile_v2 import graph as graph_mod
    from tools.microsoft_graph_auth import MicrosoftGraphTokenProvider

    monkeypatch.setenv("MSGRAPH_TENANT_ID", "tenant")
    monkeypatch.setenv("MSGRAPH_CLIENT_ID", "client")
    monkeypatch.setenv("MSGRAPH_CLIENT_SECRET", "secret")
    provider = graph_mod._default_token_provider()
    assert isinstance(provider, MicrosoftGraphTokenProvider)


def test_default_token_provider_falls_back_to_delegated(monkeypatch, tmp_path):
    """When only LINEO_MS_* + an on-disk bundle exist (the operator's
    real setup), we hand back the delegated provider — no error, no
    new credential file invented.
    """
    from finance.reconcile_v2 import graph as graph_mod

    # No app-only credentials.
    monkeypatch.delenv("MSGRAPH_TENANT_ID", raising=False)
    monkeypatch.delenv("MSGRAPH_CLIENT_ID", raising=False)
    monkeypatch.delenv("MSGRAPH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("LINEO_MS_TENANT_ID", "lineo-tenant")
    monkeypatch.setenv("LINEO_MS_CLIENT_ID", "lineo-client")

    # Existing bundle (fake) at the documented path.
    bundle = tmp_path / "sebastian.json"
    bundle.write_text(json.dumps({"refresh_token": "rt", "access_token": "at"}))
    monkeypatch.setattr(graph_mod, "DELEGATED_TOKEN_FILE", bundle)

    provider = graph_mod._default_token_provider()
    assert isinstance(provider, graph_mod._DelegatedRefreshTokenProvider)
    assert provider.token_file == bundle


def test_default_token_provider_errors_when_neither_set(monkeypatch):
    """No app-only + no LINEO_MS_* => clean configuration error, not a
    confusing 401 later.
    """
    from finance.reconcile_v2 import graph as graph_mod
    from tools.microsoft_graph_auth import MicrosoftGraphConfigError

    for k in (
        "MSGRAPH_TENANT_ID", "MSGRAPH_CLIENT_ID", "MSGRAPH_CLIENT_SECRET",
        "LINEO_MS_TENANT_ID", "LINEO_MS_CLIENT_ID",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(MicrosoftGraphConfigError):
        graph_mod._default_token_provider()


# ──────────────────────────────────────────────────────────────────────
# 4. send_beleg
# ──────────────────────────────────────────────────────────────────────


def _vodafone_mail(tmp_path: Path) -> dict[str, Any]:
    pdf = tmp_path / "vodafone.pdf"
    pdf.write_bytes(b"%PDF-fake")
    return {
        "outlook_message_id":  "MSG-1",
        "internet_message_id": "<imid-1>",
        "mailbox":             "rechnung@lineo.finance",
        "from_address":        "billing@vodafone.de",
        "subject":             "Ihre Vodafone Rechnung",
        "received_at":         datetime(2026, 4, 11, 8, 0, tzinfo=timezone.utc),
        "body_text":           "...",
        "has_attachments":     True,
        "attachments": [{
            "name":           "vodafone.pdf",
            "content_type":   "application/pdf",
            "size_bytes":     12,
            "sha256":         "sha-1",
            "local_path":     str(pdf),
            "extracted_text": "...",
            "extract_error":  None,
        }],
    }


def test_send_beleg_happy_path(tmp_path):
    a = _make_adapter()
    mail = _vodafone_mail(tmp_path)
    sender = FakeMailSender()
    result = send_beleg(a, tx_id=100, mail=mail, sender=sender)
    assert result["sent"] is True
    assert result.get("idempotent") is not True
    assert result["belege_sent"]["bank_tx_id"] == 100
    assert sender.sent[0]["to_recipient"].endswith("uploadmail.datev.de")
    # exactly one belege_sent row beyond the seeded one
    assert len(a.belege_sent) == 2


def test_send_beleg_idempotent_on_same_attachment(tmp_path):
    a = _make_adapter()
    mail = _vodafone_mail(tmp_path)
    sender = FakeMailSender()
    first = send_beleg(a, tx_id=100, mail=mail, sender=sender)
    second = send_beleg(a, tx_id=100, mail=mail, sender=sender)
    assert first["sent"] is True
    assert second["sent"] is True and second["idempotent"] is True
    # sender called only once
    assert len(sender.sent) == 1
    # only one belege_sent row added for tx 100
    rows_100 = [b for b in a.belege_sent if b["bank_tx_id"] == 100]
    assert len(rows_100) == 1


def test_send_beleg_step_a_failure_writes_no_row(tmp_path):
    a = _make_adapter()
    mail = _vodafone_mail(tmp_path)
    sender = FakeMailSender(fail_step_a=True, error_text="graph down")
    before = len(a.belege_sent)
    result = send_beleg(a, tx_id=100, mail=mail, sender=sender)
    assert result["sent"] is False
    assert result["step"] == "sendMail"
    assert "graph down" in (result.get("error") or "")
    assert len(a.belege_sent) == before


def test_send_beleg_rejects_ignored_tx(tmp_path):
    a = _make_adapter()
    mail = _vodafone_mail(tmp_path)
    sender = FakeMailSender()
    with pytest.raises(InvalidTransition):
        send_beleg(a, tx_id=300, mail=mail, sender=sender)


def test_send_beleg_refuses_already_sent_outlook_message_id(tmp_path):
    """Same outlook_message_id (same forwarded mail) already in
    belege_sent for ANY tx → send_beleg refuses with already_sent.
    """
    a = _make_adapter()
    # Seed a belege_sent row tied to a *different* tx_id but with the
    # outlook_message_id we're about to try to forward.
    a.belege_sent.append({
        "id": 99, "bank_tx_id": 999,  # some other tx
        "outlook_message_id": "MSG-1",
        "internet_message_id": "<some-unrelated>",
        "attachment_filenames": ["unrelated.pdf"],
        "source_mailbox": "rechnung@lineo.finance",
        "sent_at": datetime(2026, 4, 9, 8, 0, tzinfo=timezone.utc),
        "recipient": "x@datev", "subject": "prior", "via": "agent_match",
        "bank_tx_amount": 99.99, "bank_tx_booking_date": date(2026, 4, 9),
        "confidence": None, "reasoning": "seeded prior",
        "created_at": datetime(2026, 4, 9, 8, 1, tzinfo=timezone.utc),
    })
    a._next_belege_id = 100
    mail = _vodafone_mail(tmp_path)  # outlook_message_id = "MSG-1"
    sender = FakeMailSender()
    result = send_beleg(a, tx_id=100, mail=mail, sender=sender)
    assert result["sent"] is False
    assert result["status"] == "already_sent"
    assert result["existing_belege_sent_id"] == 99
    assert result["existing_bank_tx_id"] == 999
    assert result["matched_on"] == "outlook_message_id"
    assert "sent_at" in result
    # sender was NOT called
    assert sender.sent == []
    # No new belege_sent row was written.
    assert {b["id"] for b in a.belege_sent} == {1, 99}


def test_send_beleg_links_orphan_outlook_message_id(tmp_path):
    """The dedup row was sent by a legacy outlook_auto_rule and has
    ``bank_tx_id IS NULL``. send_beleg must LINK the existing row to
    the candidate tx instead of refusing or sending a second mail.
    """
    a = _make_adapter()
    a.belege_sent.append({
        "id": 55, "bank_tx_id": None,  # orphan
        "outlook_message_id": "MSG-1",
        "internet_message_id": None,
        "attachment_filenames": [],
        "source_mailbox": "rechnung@lineo.finance",
        "sent_at": datetime(2026, 4, 9, 8, 0, tzinfo=timezone.utc),
        "recipient": "x@datev", "subject": "WG: prior auto-rule",
        "via": "outlook_auto_rule",
        "bank_tx_amount": None, "bank_tx_booking_date": None,
        "confidence": None, "reasoning": "seeded auto-rule",
        "created_at": datetime(2026, 4, 9, 8, 1, tzinfo=timezone.utc),
    })
    a._next_belege_id = 56
    mail = _vodafone_mail(tmp_path)  # outlook_message_id = "MSG-1"
    sender = FakeMailSender()
    result = send_beleg(a, tx_id=100, mail=mail, sender=sender)
    assert result["sent"] is True
    assert result["status"] == "linked_existing"
    assert result["belege_sent_id"] == 55
    assert result["matched_on"] == "outlook_message_id"
    assert result["linked_from_null"] is True
    # No mail was sent — the PDF is already in DATEV.
    assert sender.sent == []
    # No new belege_sent row written; the existing one is now linked.
    assert {b["id"] for b in a.belege_sent} == {1, 55}
    linked = next(b for b in a.belege_sent if b["id"] == 55)
    assert linked["bank_tx_id"] == 100


def test_send_beleg_links_orphan_filename_plus_amount(tmp_path):
    """Orphan dedup match via filename+amount → link, don't refuse."""
    a = _make_adapter()
    a.belege_sent.append({
        "id": 77, "bank_tx_id": None,  # orphan
        "outlook_message_id": "MSG-OLD",
        "internet_message_id": "<imid-old>",
        "attachment_filenames": ["vodafone.pdf"],
        "source_mailbox": "rechnung@lineo.finance",
        "sent_at": datetime(2026, 3, 11, 8, 0, tzinfo=timezone.utc),
        "recipient": "x@datev", "subject": "prior month",
        "via": "outlook_auto_rule",
        "bank_tx_amount": 39.99, "bank_tx_booking_date": date(2026, 3, 11),
        "confidence": None, "reasoning": "seeded prior month",
        "created_at": datetime(2026, 3, 11, 8, 1, tzinfo=timezone.utc),
    })
    a._next_belege_id = 78
    mail = _vodafone_mail(tmp_path)
    mail["outlook_message_id"] = "MSG-NEW"
    mail["internet_message_id"] = "<imid-new>"
    sender = FakeMailSender()
    result = send_beleg(a, tx_id=100, mail=mail, sender=sender)
    assert result["sent"] is True
    assert result["status"] == "linked_existing"
    assert result["belege_sent_id"] == 77
    assert result["matched_on"] == "attachment_filename+bank_tx_amount"
    assert result["linked_from_null"] is True
    assert sender.sent == []
    linked = next(b for b in a.belege_sent if b["id"] == 77)
    assert linked["bank_tx_id"] == 100


def test_send_beleg_refuses_already_sent_filename_plus_amount(tmp_path):
    """Same attachment filename + same bank_tx_amount as a prior send
    for a different tx_id → send_beleg refuses with already_sent.
    """
    a = _make_adapter()
    # Seed a prior send: different tx_id, same filename + same amount
    # as the tx we'll try to forward against (tx 100, amount 39.99,
    # attachment vodafone.pdf).
    a.belege_sent.append({
        "id": 77, "bank_tx_id": 555,  # some other tx
        "outlook_message_id": "MSG-OLD",
        "internet_message_id": "<imid-old>",
        "attachment_filenames": ["vodafone.pdf"],
        "source_mailbox": "rechnung@lineo.finance",
        "sent_at": datetime(2026, 3, 11, 8, 0, tzinfo=timezone.utc),
        "recipient": "x@datev", "subject": "prior month",
        "via": "agent_match",
        "bank_tx_amount": 39.99, "bank_tx_booking_date": date(2026, 3, 11),
        "confidence": None, "reasoning": "seeded prior month",
        "created_at": datetime(2026, 3, 11, 8, 1, tzinfo=timezone.utc),
    })
    a._next_belege_id = 78
    # Build a mail whose outlook_message_id / internet_message_id are
    # different from the seeded row, so only filename+amount can match.
    mail = _vodafone_mail(tmp_path)
    mail["outlook_message_id"] = "MSG-NEW"
    mail["internet_message_id"] = "<imid-new>"
    sender = FakeMailSender()
    result = send_beleg(a, tx_id=100, mail=mail, sender=sender)
    assert result["sent"] is False
    assert result["status"] == "already_sent"
    assert result["existing_belege_sent_id"] == 77
    assert result["existing_bank_tx_id"] == 555
    assert result["matched_on"] == "attachment_filename+bank_tx_amount"
    assert sender.sent == []
    # No new belege_sent row was written.
    assert {b["id"] for b in a.belege_sent} == {1, 77}


def test_send_beleg_attachment_choice_when_multiple(tmp_path):
    a = _make_adapter()
    pdf1 = tmp_path / "a.pdf"; pdf1.write_bytes(b"%PDF")
    pdf2 = tmp_path / "b.pdf"; pdf2.write_bytes(b"%PDF")
    mail = {
        "outlook_message_id": "MSG-2", "internet_message_id": None,
        "mailbox": "rechnung@lineo.finance", "from_address": "x@y",
        "subject": "two pdfs", "received_at": None, "body_text": "",
        "has_attachments": True,
        "attachments": [
            {"name": "a.pdf", "content_type": "application/pdf",
             "size_bytes": 4, "sha256": "sha-a",
             "local_path": str(pdf1), "extracted_text": None,
             "extract_error": None},
            {"name": "b.pdf", "content_type": "application/pdf",
             "size_bytes": 4, "sha256": "sha-b",
             "local_path": str(pdf2), "extracted_text": None,
             "extract_error": None},
        ],
    }
    sender = FakeMailSender()
    with pytest.raises(ToolError):
        send_beleg(a, tx_id=100, mail=mail, sender=sender)
    result = send_beleg(a, tx_id=100, mail=mail, sender=sender,
                       attachment_name="b.pdf")
    assert result["sent"] is True
    assert sender.sent[0]["attachment_name"] == "b.pdf"


# ──────────────────────────────────────────────────────────────────────
# 5. mark_ignored
# ──────────────────────────────────────────────────────────────────────


def test_mark_ignored_happy_path():
    a = _make_adapter()
    result = mark_ignored(a, tx_id=100, reason="test ignore")
    assert result["transaction"]["ignored"] is True
    assert result["audit_anomaly"]["bank_tx_id"] == 100
    assert result["audit_anomaly"]["severity"] == "info"


def test_mark_ignored_refuses_already_ignored():
    a = _make_adapter()
    with pytest.raises(InvalidTransition):
        mark_ignored(a, tx_id=300, reason="already ignored")


def test_mark_ignored_unknown_tx():
    a = _make_adapter()
    with pytest.raises(NotFound):
        mark_ignored(a, tx_id=9999, reason="?")


# ──────────────────────────────────────────────────────────────────────
# 6. finalize_run
# ──────────────────────────────────────────────────────────────────────


def test_finalize_run_writes_row_and_notifies():
    a = _make_adapter()
    notifier = RecordingNotifier()
    result = finalize_run(
        a,
        summary="April 2026: 1 open, 0 anomalies.",
        notes={"month": "2026-04", "model_id": "test-model"},
        invoked_by="cron", notifier=notifier,
    )
    assert result["notification_dispatched"] is True
    assert len(a.reconcile_runs) == 1
    assert a.reconcile_runs[0]["invoked_by"] == "cron"
    assert json.loads(a.reconcile_runs[0]["notes"])["month"] == "2026-04"
    assert len(notifier.calls) == 1
    assert "April 2026" in notifier.calls[0]["summary_md"]


def test_finalize_run_rejects_empty_summary():
    a = _make_adapter()
    with pytest.raises(ToolError):
        finalize_run(a, summary="")

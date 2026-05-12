"""The seven verbs of the v2 reconcile toolbox (closes unimatrix27/ideas#31).

Strictly seven; the SKILL.md tells the LLM not to invent more. Each
function is a single-purpose unit of work — never compound transactions
across tables.

    list_open_txs        — not ignored, no belege_sent row → returns rows
    get_tx_context       — tx details + likely-relevant mail attachments
    search_inbox         — vendor / amount / date_window → mail + attachments
    send_beleg           — send to DATEV + write belege_sent row
    mark_ignored         — set transactions.ignored=true
    flag_anomaly         — write agent_anomalies row
    finalize_run         — write agent_reconcile_runs row + notify

If you find yourself adding an eighth, stop and rethink. The point of
the v2 toolbox is the narrow surface (#31).
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from finance.reconcile_v2.adapter import (
    Adapter,
    InvalidTransition,
    NotFound,
    ToolError,
)
from finance.reconcile_v2.graph import (
    DEFAULT_DATEV_RECIPIENT,
    DEFAULT_SOURCE_MAILBOX,
    InboxClient,
    MailAttachment,
    MailMessage,
    MailSender,
)
from finance.reconcile_v2.ignore_rules import (
    DEFAULT_IGNORE_RULES_PATH,
    IgnoreRule,
    apply_rules,
    parse_rules_file,
)
from finance.reconcile_v2.notifier import Notifier, StdoutNotifier


# ──────────────────────────────────────────────────────────────────────
# 1. list_open_txs
# ──────────────────────────────────────────────────────────────────────


def list_open_txs(
    adapter: Adapter,
    *,
    month: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    ignore_rules_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Return every TX that is *not ignored* and has no ``belege_sent`` row.

    The DB filter handles the persistent ``ignored=true`` rows. The
    ``ignore_rules.md`` file is a second pass — TX that match a rule are
    excluded from ``open`` and surfaced under ``would_ignore`` so the
    agent can either invoke ``mark_ignored`` (to persist the rule) or
    just skip them this run.

    Returns::

        {
          "month":         "2026-04",
          "open":          [tx, ...],
          "would_ignore":  [{"tx": tx, "rule": "...", "reason": "..."}, ...],
          "ignore_rules_count": int,
        }
    """
    rules_path = ignore_rules_path or DEFAULT_IGNORE_RULES_PATH
    rules = parse_rules_file(rules_path)
    rows = adapter.list_open_transactions(month=month, limit=limit, offset=offset)
    kept, skipped = apply_rules(rows, rules)
    return {
        "month":              month,
        "open":               kept,
        "would_ignore":       skipped,
        "ignore_rules_count": len(rules),
        "ignore_rules_path":  str(rules_path),
    }


# ──────────────────────────────────────────────────────────────────────
# 2. get_tx_context
# ──────────────────────────────────────────────────────────────────────


def get_tx_context(
    adapter: Adapter,
    tx_id: int,
    *,
    inbox: Optional[InboxClient] = None,
    mailbox: str = DEFAULT_SOURCE_MAILBOX,
    date_window_days: int = 30,
    max_results: int = 10,
) -> dict[str, Any]:
    """Bundle the TX row, any existing ``belege_sent``, open anomalies,
    and a *narrow* inbox auto-search keyed on counterparty + amount +
    date window. The LLM reads the returned bodies / PDF text directly.

    The auto-search is on by default but the agent can suppress it by
    passing ``inbox=None`` if it only wants the DB context.
    """
    tx = adapter.get_transaction(tx_id)
    if tx is None:
        raise NotFound(f"no transaction with id {tx_id}")
    belege_sent = adapter.find_belege_sent_for_tx(tx_id)
    anomalies = adapter.list_anomalies(tx_id=tx_id, limit=20)

    likely_mails: list[dict[str, Any]] = []
    auto_search_note: Optional[str] = None
    if inbox is not None:
        booking = tx.get("booking_date")
        if booking is not None:
            window = (
                booking - timedelta(days=date_window_days),
                booking + timedelta(days=date_window_days),
            )
        else:
            window = None
        vendor = tx.get("counterparty_name") or None
        try:
            mails = inbox.search(
                mailbox=mailbox,
                vendor=_first_vendor_token(vendor),
                amount=float(tx["amount"]) if tx.get("amount") is not None else None,
                date_window=window,
                max_results=max_results,
            )
            likely_mails = [_serialize_mail(m) for m in mails]
        except ValueError as exc:
            # Zero-filter call would have been refused; surface the
            # situation but don't fail the whole verb.
            auto_search_note = f"auto-search skipped: {exc}"
        except Exception as exc:  # noqa: BLE001
            auto_search_note = f"auto-search failed: {type(exc).__name__}: {exc}"

    return {
        "transaction":      tx,
        "belege_sent":      belege_sent,
        "anomalies":        anomalies,
        "likely_mails":     likely_mails,
        "auto_search_note": auto_search_note,
        "mailbox":          mailbox,
    }


# ──────────────────────────────────────────────────────────────────────
# 3. search_inbox
# ──────────────────────────────────────────────────────────────────────


def search_inbox(
    *,
    inbox: InboxClient,
    mailbox: str = DEFAULT_SOURCE_MAILBOX,
    vendor: Optional[str] = None,
    amount: Optional[float] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    message_id: Optional[str] = None,
    max_results: int = 25,
) -> list[dict[str, Any]]:
    """Narrow mailbox search. *Must* supply at least one of vendor /
    amount / date_window / message_id — the InboxClient refuses
    zero-filter calls.

    Returns the mail bodies + PDF text directly so the LLM can read
    them without a second tool call.
    """
    window: Optional[tuple[date, date]] = None
    if date_from is not None or date_to is not None:
        if date_from is None or date_to is None:
            raise ToolError("date_from and date_to must be supplied together")
        if date_from > date_to:
            raise ToolError("date_from > date_to")
        window = (date_from, date_to)
    if vendor is None and amount is None and window is None and message_id is None:
        raise ToolError(
            "search_inbox requires at least one of: vendor, amount, "
            "date_from/date_to, message_id (no full-mailbox scans)"
        )
    mails = inbox.search(
        mailbox=mailbox,
        vendor=vendor,
        amount=amount,
        date_window=window,
        message_id=message_id,
        max_results=max_results,
    )
    return [_serialize_mail(m) for m in mails]


# ──────────────────────────────────────────────────────────────────────
# 4. send_beleg
# ──────────────────────────────────────────────────────────────────────


def send_beleg(
    adapter: Adapter,
    *,
    tx_id: int,
    mail: Mapping[str, Any],
    sender: MailSender,
    attachment_name: Optional[str] = None,
    datev_recipient: str = DEFAULT_DATEV_RECIPIENT,
    source_mailbox: str = DEFAULT_SOURCE_MAILBOX,
    decided_by: str = "llm",
    reasoning: Optional[str] = None,
) -> dict[str, Any]:
    """Forward one PDF attachment to DATEV and record the send.

    ``mail`` is a dict in the shape returned by ``search_inbox`` /
    ``get_tx_context``. It must carry exactly one attachment with a
    non-empty ``local_path``; if there are several attachments, the
    caller picks the right one and passes ``attachment_name=``.

    Idempotency: if ``bank.belege_sent`` already has a row for this TX
    *and* one of its ``attachment_filenames`` equals the chosen
    attachment's name, we return that row unchanged (no second send,
    no second row).

    Partial-failure semantics mirror the v1 ``send_match``:

    * step (a/b) fails → ``{sent: False, step, error}``; no row written.
    * step (c) fails  → mail is out but row write failed; return the
      partial state with ``warning`` so the agent can ``flag_anomaly``.
    """
    tx = adapter.get_transaction(tx_id)
    if tx is None:
        raise NotFound(f"no transaction with id {tx_id}")
    if tx.get("ignored"):
        raise InvalidTransition(
            f"tx {tx_id} is ignored=true; send_beleg refuses to act on it"
        )

    # Resolve the attachment.
    attachments = mail.get("attachments") or []
    if not attachments:
        raise ToolError(
            f"mail {mail.get('outlook_message_id')} has no attachments to send"
        )
    chosen: Optional[Mapping[str, Any]] = None
    if attachment_name is not None:
        for a in attachments:
            if a.get("name") == attachment_name:
                chosen = a
                break
        if chosen is None:
            raise ToolError(
                f"no attachment named {attachment_name!r} on mail "
                f"{mail.get('outlook_message_id')}"
            )
    elif len(attachments) == 1:
        chosen = attachments[0]
    else:
        raise ToolError(
            f"mail {mail.get('outlook_message_id')} has "
            f"{len(attachments)} attachments; pass attachment_name=<name>"
        )

    chosen_name = chosen.get("name") or "receipt.pdf"
    local_path = chosen.get("local_path")
    if not local_path:
        raise ToolError(
            f"attachment {chosen_name!r} has no local_path "
            f"(extract_error={chosen.get('extract_error')})"
        )
    blob = Path(local_path)
    if not blob.exists():
        raise ToolError(f"attachment file {blob} not found on disk")

    # Idempotency probe by (bank_tx_id, attachment filename).
    existing = adapter.find_belege_sent_for_tx(tx_id)
    for row in existing:
        fnames = row.get("attachment_filenames") or []
        if chosen_name in fnames:
            return {
                "sent":          True,
                "idempotent":    True,
                "belege_sent":   row,
            }

    subject = _build_subject(tx, mail, chosen_name)
    body_text = _build_body(tx, mail, chosen_name, reasoning=reasoning)

    result = sender.send(
        from_mailbox=source_mailbox,
        to_recipient=datev_recipient,
        subject=subject,
        body_text=body_text,
        attachment_path=blob,
        attachment_name=chosen_name,
    )
    if not result.ok:
        return {
            "sent":  False,
            "step":  result.step,
            "error": result.error,
        }
    meta = result.metadata
    assert meta is not None  # invariant of ok=True

    try:
        row = adapter.insert_belege_sent(
            outlook_message_id=meta.outlook_message_id,
            internet_message_id=meta.internet_message_id,
            source_mailbox=source_mailbox,
            sent_at=meta.sent_at,
            recipient=datev_recipient,
            subject=meta.subject or subject,
            attachment_filenames=meta.attachment_filenames or [chosen_name],
            via="agent_match",
            bank_tx_id=tx_id,
            bank_tx_amount=float(tx["amount"]) if tx.get("amount") is not None else None,
            bank_tx_booking_date=tx.get("booking_date"),
            confidence=None,  # v2 records confidence in `reasoning`, not the enum column
            reasoning=reasoning or _default_reasoning(tx, mail, chosen),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "sent":    True,
            "step":    "belege_sent_insert",
            "error":   f"{type(exc).__name__}: {exc}",
            "warning": (
                "the mail went out (outlook_message_id captured) but the "
                "belege_sent audit row was NOT written. Manual intervention "
                "required — recommend flag_anomaly."
            ),
            "sent_metadata": _meta_to_dict(meta),
        }

    return {
        "sent":         True,
        "belege_sent":  row,
        "sent_metadata": _meta_to_dict(meta),
    }


# ──────────────────────────────────────────────────────────────────────
# 5. mark_ignored
# ──────────────────────────────────────────────────────────────────────


def mark_ignored(
    adapter: Adapter,
    *,
    tx_id: int,
    reason: str,
    decided_by: str = "llm",
) -> dict[str, Any]:
    """Set ``bank.transactions.ignored = true``.

    Per #31: this is one-way. The verb refuses to flip ``true → false``
    and refuses a redundant ``true → true`` call (so the audit trail
    stays meaningful). The reason is recorded in a fresh
    ``agent_anomalies`` row with ``severity='info'`` — that gives us
    *why* without adding a column to ``bank.transactions``.
    """
    reason = _validate_reason(reason)
    tx = adapter.get_transaction(tx_id)
    if tx is None:
        raise NotFound(f"no transaction with id {tx_id}")
    if bool(tx.get("ignored")) is True:
        raise InvalidTransition(
            f"tx {tx_id} is already ignored=true; mark_ignored cannot flip "
            f"true→false and refuses redundant true→true calls"
        )
    tx_after = adapter.set_transaction_ignored(tx_id=tx_id, expect_currently=False)
    audit = adapter.insert_anomaly(
        bank_tx_id=tx_id,
        reason=f"[mark_ignored] {reason}",
        severity="info",
        raised_by=decided_by,
    )
    return {"transaction": tx_after, "audit_anomaly": audit}


# ──────────────────────────────────────────────────────────────────────
# 6. flag_anomaly
# ──────────────────────────────────────────────────────────────────────


def flag_anomaly(
    adapter: Adapter,
    *,
    reason: str,
    severity: str = "warn",
    tx_id: Optional[int] = None,
    raised_by: str = "llm",
    run_id: Optional[int] = None,
) -> dict[str, Any]:
    """Append one row to ``bank.agent_anomalies``.

    ``severity`` is constrained to {info, warn, block} by the DB check
    constraint and re-checked here for clearer errors.
    """
    reason = _validate_reason(reason)
    if severity not in ("info", "warn", "block"):
        raise ToolError(f"severity must be info|warn|block, got {severity!r}")
    if tx_id is not None:
        tx = adapter.get_transaction(tx_id)
        if tx is None:
            raise NotFound(f"no transaction with id {tx_id}")
    return adapter.insert_anomaly(
        bank_tx_id=tx_id,
        reason=reason,
        severity=severity,
        raised_by=raised_by,
        run_id=run_id,
    )


# ──────────────────────────────────────────────────────────────────────
# 7. finalize_run
# ──────────────────────────────────────────────────────────────────────


def finalize_run(
    adapter: Adapter,
    *,
    summary: str,
    notes: Optional[Mapping[str, Any]] = None,
    invoked_by: str = "llm",
    notifier: Optional[Notifier] = None,
    notification_title: Optional[str] = "hermes reconcile-v2 run",
) -> dict[str, Any]:
    """Write the ``bank.agent_reconcile_runs`` row and dispatch the
    summary via the notifier.

    ``notes`` is stored in the existing ``tool_call_summary`` jsonb
    column (we reuse it so v1 and v2 rows have parallel shapes).
    """
    if not summary or not summary.strip():
        raise ToolError("summary must be a non-empty string")
    row = adapter.insert_reconcile_run(
        summary_md=summary,
        notes=notes,
        invoked_by=invoked_by,
    )
    notifier = notifier or StdoutNotifier()
    notifier.notify(summary, title=notification_title)
    return {"reconcile_run": row, "notification_dispatched": True}


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


def _validate_reason(reason: str) -> str:
    if not reason or not reason.strip():
        raise ToolError("reason must be a non-empty string")
    return reason.strip()


def _serialize_mail(m: MailMessage) -> dict[str, Any]:
    return {
        "outlook_message_id":  m.outlook_message_id,
        "internet_message_id": m.internet_message_id,
        "mailbox":             m.mailbox,
        "from_address":        m.from_address,
        "subject":             m.subject,
        "received_at":         m.received_at,
        "body_text":           m.body_text,
        "has_attachments":     m.has_attachments,
        "attachments":         [_serialize_attachment(a) for a in m.attachments],
    }


def _serialize_attachment(a: MailAttachment) -> dict[str, Any]:
    return {
        "name":           a.name,
        "content_type":   a.content_type,
        "size_bytes":     a.size_bytes,
        "sha256":         a.sha256,
        "local_path":     a.local_path,
        "extracted_text": a.extracted_text,
        "extract_error":  a.extract_error,
    }


def _meta_to_dict(meta: Any) -> dict[str, Any]:
    if is_dataclass(meta):
        return asdict(meta)
    return dict(meta)


def _first_vendor_token(vendor: Optional[str]) -> Optional[str]:
    """Strip suffixes that don't help an inbox $search.

    Microsoft Graph's full-text search treats every space-separated
    token as an AND. "Vodafone GmbH" matches fewer mails than
    "Vodafone" — and any vendor-specific mail will still mention its
    short name. Drop trailing legal suffixes; keep the rest.
    """
    if not vendor:
        return None
    raw = vendor.strip()
    if not raw:
        return None
    drop_suffixes = (
        " gmbh", " ag", " ug", " kg", " ohg", " e.k.", " e.k", " mbh",
        " ltd", " ltd.", " inc", " inc.", " s.a.", " s.a", " sas",
        " b.v.", " bv", " gmbh & co. kg", " gmbh & co kg",
    )
    low = raw.lower()
    for sfx in drop_suffixes:
        if low.endswith(sfx):
            raw = raw[: len(raw) - len(sfx)].strip()
            break
    # Use the first space-separated word — keeps the query specific.
    return raw.split()[0] if raw else None


def _build_subject(
    tx: Mapping[str, Any], mail: Mapping[str, Any], attachment_name: str,
) -> str:
    vendor = tx.get("counterparty_name") or "vendor"
    booking = tx.get("booking_date")
    booking_str = booking.isoformat() if hasattr(booking, "isoformat") else str(booking)
    amount = tx.get("amount")
    amount_str = f"{float(amount):.2f}" if amount is not None else "?"
    return f"[hermes-v2] {vendor} — €{amount_str} ({booking_str})"


def _build_body(
    tx: Mapping[str, Any],
    mail: Mapping[str, Any],
    attachment_name: str,
    *,
    reasoning: Optional[str],
) -> str:
    lines = [
        "Automatischer Beleg-Forward (hermes reconcile-v2).",
        "",
        f"Bank tx id      : {tx.get('id')}",
        f"Booking date    : {tx.get('booking_date')}",
        f"Amount          : {tx.get('amount')} {tx.get('currency') or ''}".strip(),
        f"Counterparty    : {tx.get('counterparty_name')}",
        f"Remittance      : {(tx.get('remittance_information') or '').strip()}",
        "",
        f"Source mail id  : {mail.get('outlook_message_id')}",
        f"Source subject  : {mail.get('subject')}",
        f"Source received : {mail.get('received_at')}",
        f"Attachment      : {attachment_name}",
    ]
    if reasoning:
        lines += ["", f"Agent reasoning : {reasoning}"]
    return "\n".join(lines)


def _default_reasoning(
    tx: Mapping[str, Any],
    mail: Mapping[str, Any],
    attachment: Mapping[str, Any],
) -> str:
    return (
        f"v2 send_beleg "
        f"tx_id={tx.get('id')} "
        f"mail_id={mail.get('outlook_message_id')} "
        f"attachment={attachment.get('name')} "
        f"sha256={attachment.get('sha256')}"
    )

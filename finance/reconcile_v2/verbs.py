"""The six verbs of the v2 reconcile toolbox (closes unimatrix27/ideas#31).

Strictly six; the SKILL.md tells the LLM not to invent more. Each
function is a single-purpose unit of work — never compound transactions
across tables.

    list_open_txs        — not ignored, no belege_sent row → returns rows
    get_tx_context       — tx details + likely-relevant mail attachments
    search_inbox         — vendor / amount / date_window → mail + attachments
    send_beleg           — send to DATEV + write/link belege_sent row
    mark_ignored         — set transactions.ignored=true
    finalize_run         — write agent_reconcile_runs row + notify

Anomalies are not the agent's concern; ``flag_anomaly`` was removed
in PR #7's follow-up. ``mark_ignored`` still writes an audit row to
``bank.agent_anomalies`` (severity=info) for human traceability, but
the agent has no verb to surface free-form anomalies. If a TX cannot
be resolved this run, leave it open — the next run picks it up.

If you find yourself adding a seventh, stop and rethink. The point of
the v2 toolbox is the narrow surface (#31).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


LOGGER = logging.getLogger("finance.reconcile_v2.verbs")

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

    mailboxes = _mailbox_targets(mailbox)
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
            mails = _search_all_mailboxes(
                inbox=inbox,
                mailboxes=mailboxes,
                vendor=_first_vendor_token(vendor),
                amount=float(tx["amount"]) if tx.get("amount") is not None else None,
                date_window=window,
                max_results=max_results,
            )
            if not mails:
                for ref in _invoice_reference_search_terms(tx):
                    mails = _search_all_mailboxes(
                        inbox=inbox,
                        mailboxes=mailboxes,
                        vendor=ref,
                        amount=float(tx["amount"]) if tx.get("amount") is not None else None,
                        date_window=window,
                        max_results=max_results,
                    )
                    if mails:
                        break
            likely_mails = [_serialize_mail(m) for m in mails]
        except ValueError as exc:
            # Zero-filter call would have been refused; surface the
            # situation but don't fail the whole verb.
            auto_search_note = f"auto-search skipped: {exc}"
        except Exception as exc:  # noqa: BLE001
            # Mirror search_inbox: log once, return empty + note.
            LOGGER.warning(
                "get_tx_context: auto-search failed for tx_id=%s (%s: %s)",
                tx_id, type(exc).__name__, exc,
            )
            auto_search_note = f"auto-search failed: {type(exc).__name__}: {exc}"

    return {
        "transaction":      tx,
        "belege_sent":      belege_sent,
        "anomalies":        anomalies,
        "likely_mails":     likely_mails,
        "auto_search_note": auto_search_note,
        "mailbox":          mailbox,
        "mailboxes_searched": mailboxes,
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
    mailboxes = _mailbox_targets(mailbox)
    try:
        mails = _search_all_mailboxes(
            inbox=inbox,
            mailboxes=mailboxes,
            vendor=vendor,
            amount=amount,
            date_window=window,
            message_id=message_id,
            max_results=max_results,
        )
    except ValueError:
        # Inbox client raises ValueError for invalid filter combos —
        # surface that as a ToolError so the agent sees a clean signal
        # rather than a generic Python exception.
        raise
    except Exception as exc:  # noqa: BLE001
        # Graph / network / auth failures: log once and return empty so
        # callers (and the agent's tool budget) aren't blown up by a
        # transient outage. Mirrors get_tx_context's auto-search policy.
        LOGGER.warning(
            "search_inbox: Graph search failed (%s: %s) — returning [] "
            "for mailbox=%s vendor=%r amount=%r date_window=%r message_id=%r",
            type(exc).__name__, exc, ",".join(mailboxes), vendor, amount, window, message_id,
        )
        return []
    return [_serialize_mail(m) for m in mails]


def _mailbox_targets(primary_mailbox: str) -> list[str]:
    """Return the Lineo receipt-search mailbox fanout.

    The v2 search flow must not only inspect the shared receipt inbox: in
    practice vendor PDFs (notably Finovia/DATEV-originated invoices) often
    land in Catrin's or Sebastian's mailbox first and are forwarded later.
    Search all three by default, while preserving any explicit primary
    mailbox as the first target and de-duplicating.
    """
    candidates = [
        primary_mailbox,
        os.environ.get("HERMES_RECONCILE_V2_MAILBOX"),
        DEFAULT_SOURCE_MAILBOX,
        os.environ.get("LINEO_SHARED_MAILBOX_RECHNUNG"),
        os.environ.get("LINEO_MAILBOX_CATRIN"),
        os.environ.get("LINEO_MAILBOX_SEBASTIAN"),
        "catrin.stuecker@lineo.finance",
        "sebastian.stuecker@lineo.finance",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        for part in str(candidate).split(","):
            mailbox = part.strip()
            key = mailbox.lower()
            if mailbox and key not in seen:
                out.append(mailbox)
                seen.add(key)
    return out


def _search_all_mailboxes(
    *,
    inbox: InboxClient,
    mailboxes: list[str],
    vendor: Optional[str],
    amount: Optional[float],
    date_window: Optional[tuple[date, date]],
    message_id: Optional[str] = None,
    max_results: int = 25,
) -> list[MailMessage]:
    """Run the same narrow Graph query against every configured mailbox.

    ``max_results`` is intentionally per mailbox, not global: if the shared
    receipt inbox contains noisy hits, Catrin/Sebastian must still be searched
    during the same verb call. Results are de-duplicated by Graph/internet id.
    """
    out: list[MailMessage] = []
    seen: set[tuple[str, str]] = set()
    for mailbox in mailboxes:
        try:
            hits = inbox.search(
                mailbox=mailbox,
                vendor=vendor,
                amount=amount,
                date_window=date_window,
                message_id=message_id,
                max_results=max_results,
            )
        except ValueError:
            # Filter-shape bugs should still be visible to the caller.
            raise
        except Exception as exc:  # noqa: BLE001
            # One personal mailbox may be temporarily unavailable or not yet
            # delegated. Keep searching the remaining mailboxes so a Catrin
            # access issue cannot hide a receipt in Rechnung/Sebastian.
            LOGGER.warning(
                "mailbox search failed for %s (%s: %s); continuing with remaining mailboxes",
                mailbox, type(exc).__name__, exc,
            )
            continue
        for msg in hits:
            key = (
                msg.outlook_message_id or "",
                msg.internet_message_id or "",
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(msg)
    return out


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

    Cross-tx dedup: before sending, also probe ``bank.belege_sent``
    for any row (regardless of ``bank_tx_id``) that matches the
    candidate PDF on ANY of, in priority order:

    1. ``outlook_message_id`` (same forwarded mail),
    2. ``internet_message_id`` (RFC-5322 Message-Id),
    3. ``attachment_filename`` in ``attachment_filenames`` AND
       equal ``bank_tx_amount`` (a cheap stand-in for hashing).

    If a dedup match is found, behaviour depends on the existing row's
    ``bank_tx_id``:

    * ``bank_tx_id IS NULL`` (e.g. a legacy ``outlook_auto_rule`` send
      that was never linked) → link it: ``UPDATE bank.belege_sent SET
      bank_tx_id = <tx_id> WHERE id = <existing_id> AND bank_tx_id IS
      NULL`` and return ``{sent: True, status: "linked_existing",
      belege_sent_id, ...}``. No second mail is sent; the agent treats
      this as a successful reconcile because the PDF is already in
      DATEV.
    * ``bank_tx_id == tx_id`` → idempotent hit. Returns the existing
      row verbatim.
    * ``bank_tx_id`` set to some OTHER tx → real cross-tx conflict.
      Refuse with ``{sent: False, status: "already_sent",
      existing_belege_sent_id, existing_bank_tx_id, sent_at, ...}``;
      the caller decides what to do.

    Partial-failure semantics mirror the v1 ``send_match``:

    * step (a/b) fails → ``{sent: False, step, error}``; no row written.
    * step (c) fails  → mail is out but row write failed; return the
      partial state with ``warning`` so the human can intervene. The
      run notes should record the situation in ``finalize_run``.
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

    # Idempotency probe by (bank_tx_id, attachment filename) — same TX,
    # same PDF, prior successful send: return that row verbatim.
    existing = adapter.find_belege_sent_for_tx(tx_id)
    for row in existing:
        fnames = row.get("attachment_filenames") or []
        if chosen_name in fnames:
            return {
                "sent":          True,
                "idempotent":    True,
                "belege_sent":   row,
            }

    # Cross-tx dedup probe — the same PDF was already sent. Three
    # cases: (a) the existing row has no bank_tx_id → link it; (b) it's
    # already linked to this tx → idempotent hit; (c) it's linked to
    # SOME OTHER tx → real conflict, refuse.
    tx_amount = float(tx["amount"]) if tx.get("amount") is not None else None
    dup = adapter.find_belege_sent_match(
        outlook_message_id=mail.get("outlook_message_id"),
        internet_message_id=mail.get("internet_message_id"),
        attachment_filename=chosen_name,
        bank_tx_amount=tx_amount,
    )
    if dup is not None:
        dup_tx = dup.get("bank_tx_id")
        matched_on = _match_key(dup, mail, chosen_name, tx_amount)
        if dup_tx is None:
            # Link the orphan row to this tx. The UPDATE is guarded by
            # ``bank_tx_id IS NULL`` so a racing writer can't clobber a
            # newly-attached link; on guard miss we fall through to the
            # conflict branch below by re-reading.
            linked = adapter.link_belege_sent_to_tx(
                belege_sent_id=int(dup["id"]), bank_tx_id=tx_id,
            )
            if linked is not None:
                return {
                    "sent":             True,
                    "status":           "linked_existing",
                    "belege_sent_id":   int(linked["id"]),
                    "belege_sent":      linked,
                    "matched_on":       matched_on,
                    "linked_from_null": True,
                }
            # Race: re-read the row and treat as conflict.
            dup_tx = adapter.find_belege_sent_match(
                outlook_message_id=mail.get("outlook_message_id"),
                internet_message_id=mail.get("internet_message_id"),
                attachment_filename=chosen_name,
                bank_tx_amount=tx_amount,
            )
            if dup_tx is None or dup_tx.get("bank_tx_id") == tx_id:
                # Lost the race to ourselves — idempotent.
                if dup_tx is not None:
                    return {
                        "sent":             True,
                        "status":           "linked_existing",
                        "belege_sent_id":   int(dup_tx["id"]),
                        "belege_sent":      dup_tx,
                        "matched_on":       matched_on,
                        "linked_from_null": False,
                    }
            else:
                dup = dup_tx
                dup_tx = dup.get("bank_tx_id")
        if dup_tx == tx_id:
            # Same PDF, same tx, prior send — idempotent.
            return {
                "sent":             True,
                "status":           "linked_existing",
                "belege_sent_id":   int(dup["id"]),
                "belege_sent":      dup,
                "matched_on":       matched_on,
                "linked_from_null": False,
            }
        # dup_tx is set to some OTHER tx → real conflict.
        return {
            "sent":                     False,
            "status":                   "already_sent",
            "existing_belege_sent_id":  dup.get("id"),
            "existing_bank_tx_id":      dup_tx,
            "sent_at":                  dup.get("sent_at"),
            "matched_on":               matched_on,
            "existing_row":             dup,
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
                "required — record this in finalize_run notes."
            ),
            "sent_metadata": _meta_to_dict(meta),
        }

    return {
        "sent":         True,
        "belege_sent":  row,
        "sent_metadata": _meta_to_dict(meta),
    }


# ──────────────────────────────────────────────────────────────────────
# 5. send_match compatibility verb
# ──────────────────────────────────────────────────────────────────────


def send_match(
    adapter: Adapter,
    *,
    match_id: int,
    sender: MailSender,
    attachment_name: Optional[str] = None,
    datev_recipient: str = DEFAULT_DATEV_RECIPIENT,
    source_mailbox: str = DEFAULT_SOURCE_MAILBOX,
    decided_by: str = "llm",
    reasoning: Optional[str] = None,
) -> dict[str, Any]:
    """Send an approved ``receipt_matches`` row via the v2 DATEV path.

    The authoritative side effect remains :func:`send_beleg`: it performs
    all duplicate probes before any mail is sent and writes/links
    ``bank.belege_sent``. This wrapper translates a receipt candidate into
    the mail shape expected by ``send_beleg`` and then records the audit
    link back on ``bank.receipt_matches``.
    """
    bundle = adapter.get_receipt_match_with_candidate(match_id)
    if bundle is None:
        raise NotFound(f"no receipt_match with id {match_id}")
    match = bundle["match"]
    candidate = bundle.get("candidate")
    if match.get("decision_status") == "sent" and match.get("legacy_belege_sent_id"):
        return {"sent": True, "idempotent": True, "match": match}
    if match.get("decision_status") != "approved":
        raise InvalidTransition(
            f"receipt_match {match_id} decision_status={match.get('decision_status')!r}; "
            "send_match only sends approved matches"
        )
    if match.get("legacy_belege_sent_id"):
        belege_sent_id = int(match["legacy_belege_sent_id"])
        audit_row = adapter.get_belege_sent_by_id(belege_sent_id)
        if audit_row is None:
            raise ToolError(
                f"receipt_match {match_id} points at missing belege_sent id {belege_sent_id}"
            )
        updated = adapter.update_receipt_match_status(
            match_id=match_id,
            decision_status="sent",
            decided_by=decided_by,
            legacy_belege_sent_id=belege_sent_id,
            legacy_meta_patch={
                "send_match": {
                    "status": "linked_existing_audit",
                    "belege_sent_id": belege_sent_id,
                    "source": "finance-reconcile-v2",
                }
            },
        )
        return {
            "sent": True,
            "status": "linked_existing_audit",
            "belege_sent_id": belege_sent_id,
            "belege_sent": audit_row,
            "match": updated,
        }
    if candidate is None:
        raise ToolError(f"receipt_match {match_id} has no receipt_candidate to send")

    mail = _candidate_to_mail(candidate)
    result = send_beleg(
        adapter,
        tx_id=int(match["bank_tx_id"]),
        mail=mail,
        sender=sender,
        attachment_name=attachment_name or candidate.get("attachment_name"),
        datev_recipient=datev_recipient,
        source_mailbox=source_mailbox or candidate.get("mailbox") or DEFAULT_SOURCE_MAILBOX,
        decided_by=decided_by,
        reasoning=reasoning or _send_match_reasoning(match, candidate),
    )

    belege_sent_id: Optional[int] = None
    status = result.get("status")
    if result.get("sent"):
        row = result.get("belege_sent") or {}
        belege_sent_id = result.get("belege_sent_id") or row.get("id")
    elif status == "already_sent" and result.get("existing_belege_sent_id"):
        # Duplicate guard did its job: the exact same mail/PDF is already in
        # DATEV. Repair the receipt_match audit pointer without sending again.
        belege_sent_id = int(result["existing_belege_sent_id"])
        status = "linked_already_sent"
    else:
        return result

    updated = adapter.update_receipt_match_status(
        match_id=match_id,
        decision_status="sent",
        decided_by=decided_by,
        legacy_belege_sent_id=int(belege_sent_id),
        legacy_meta_patch={
            "send_match": {
                "status": status or "sent",
                "belege_sent_id": int(belege_sent_id),
                "matched_on": result.get("matched_on"),
                "source": "finance-reconcile-v2",
            }
        },
    )
    out = dict(result)
    out["sent"] = True
    if status:
        out["status"] = status
    out["match"] = updated
    out["belege_sent_id"] = int(belege_sent_id)
    return out


def approve_match(
    adapter: Adapter,
    *,
    match_id: int,
    reason: Optional[str] = None,
    decided_by: str = "llm",
) -> dict[str, Any]:
    """Compatibility verb: mark a receipt_match as approved.

    This intentionally does not send anything. The separate ``send_match``
    step performs duplicate checks before DATEV side effects.
    """
    bundle = adapter.get_receipt_match_with_candidate(match_id)
    if bundle is None:
        raise NotFound(f"no receipt_match with id {match_id}")
    match = bundle["match"]
    if match.get("decision_status") == "sent":
        raise InvalidTransition(f"receipt_match {match_id} is already sent")
    if match.get("decision_status") == "approved":
        return {"match": match, "idempotent": True}
    meta = {"approved_by": decided_by, "source": "finance-reconcile-v2"}
    if reason:
        meta["approval_reason"] = reason.strip()
    updated = adapter.update_receipt_match_status(
        match_id=match_id,
        decision_status="approved",
        decided_by=decided_by,
        legacy_meta_patch=meta,
    )
    return {"match": updated, "idempotent": False}


# ──────────────────────────────────────────────────────────────────────
# 6. mark_ignored / manual / anomaly
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


def mark_manual_needed(
    adapter: Adapter,
    *,
    tx_id: int,
    reason: str,
    decided_by: str = "llm",
) -> dict[str, Any]:
    """Record that a transaction needs human/portal handling.

    Prefer updating the latest non-sent receipt_match for the tx. If no
    row exists, create a nullable-candidate ``portal_only`` match so the
    status view can stop treating the tx as a silently-open item.
    """
    reason = _validate_reason(reason)
    tx = adapter.get_transaction(tx_id)
    if tx is None:
        raise NotFound(f"no transaction with id {tx_id}")
    existing = adapter.find_latest_receipt_match_for_tx(tx_id)
    meta = {"manual_needed_reason": reason, "source": "finance-reconcile-v2"}
    if existing and existing.get("decision_status") != "sent":
        match = adapter.update_receipt_match_status(
            match_id=int(existing["id"]),
            decision_status="manual_needed",
            decided_by=decided_by,
            legacy_meta_patch=meta,
        )
    else:
        match = adapter.create_receipt_match(
            bank_tx_id=tx_id,
            decision_status="manual_needed",
            decided_by=decided_by,
            match_type="portal_only",
            reason_codes=["manual_needed"],
            legacy_meta=meta,
        )
    return {"transaction": tx, "match": match}


def flag_anomaly(
    adapter: Adapter,
    *,
    tx_id: Optional[int] = None,
    reason: str,
    severity: str = "warn",
    decided_by: str = "llm",
) -> dict[str, Any]:
    """Compatibility verb for v1's anomaly escape hatch."""
    reason = _validate_reason(reason)
    if severity not in {"info", "warn", "block"}:
        raise ToolError("severity must be one of: info, warn, block")
    if tx_id is not None and adapter.get_transaction(tx_id) is None:
        raise NotFound(f"no transaction with id {tx_id}")
    anomaly = adapter.insert_anomaly(
        bank_tx_id=tx_id,
        reason=reason,
        severity=severity,
        raised_by=decided_by,
    )
    return {"anomaly": anomaly}


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


def _match_key(
    dup: Mapping[str, Any],
    mail: Mapping[str, Any],
    chosen_name: str,
    tx_amount: Optional[float],
) -> str:
    """Which dedup key caused ``send_beleg`` to refuse.

    Reported back to the caller verbatim so an LLM can decide whether
    "same forwarded mail" vs "same PDF on a different mail" matters
    for its downstream action.
    """
    o_mid = mail.get("outlook_message_id")
    if o_mid and dup.get("outlook_message_id") == o_mid:
        return "outlook_message_id"
    i_mid = mail.get("internet_message_id")
    if i_mid and dup.get("internet_message_id") == i_mid:
        return "internet_message_id"
    fnames = dup.get("attachment_filenames") or []
    if chosen_name in fnames and tx_amount is not None:
        dup_amt = dup.get("bank_tx_amount")
        if dup_amt is not None:
            try:
                if abs(float(dup_amt) - float(tx_amount)) < 0.005:
                    return "attachment_filename+bank_tx_amount"
            except (TypeError, ValueError):
                pass
    return "unknown"


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


def _candidate_to_mail(candidate: Mapping[str, Any]) -> dict[str, Any]:
    attachment_name = candidate.get("attachment_name")
    local_path = candidate.get("local_blob_path")
    if not attachment_name:
        raise ToolError(f"receipt_candidate {candidate.get('id')} has no attachment_name")
    if not local_path:
        raise ToolError(f"receipt_candidate {candidate.get('id')} has no local_blob_path")
    return {
        "outlook_message_id": candidate.get("outlook_message_id"),
        "internet_message_id": candidate.get("internet_message_id"),
        "mailbox": candidate.get("mailbox") or DEFAULT_SOURCE_MAILBOX,
        "from_address": candidate.get("from_email"),
        "subject": candidate.get("subject"),
        "received_at": candidate.get("received_at") or candidate.get("sent_at"),
        "body_text": candidate.get("extracted_text"),
        "has_attachments": True,
        "attachments": [{
            "name": attachment_name,
            "content_type": "application/pdf",
            "size_bytes": None,
            "sha256": candidate.get("attachment_sha256"),
            "local_path": local_path,
            "extracted_text": candidate.get("extracted_text"),
            "extract_error": candidate.get("parse_error"),
        }],
    }


def _send_match_reasoning(
    match: Mapping[str, Any], candidate: Mapping[str, Any],
) -> str:
    return (
        "v2 send_match "
        f"match_id={match.get('id')} "
        f"tx_id={match.get('bank_tx_id')} "
        f"candidate_id={candidate.get('id')} "
        f"confidence={match.get('confidence')}"
    )


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


def _invoice_reference_search_terms(tx: Mapping[str, Any]) -> list[str]:
    """Extract invoice-reference search terms from bank remittance text.

    Finovia/DATEV direct-debit remittance contains strings like
    ``ReNr: 1285/30.04.26`` while the invoice mail subject/body uses
    ``2026/1285``. Search that normalized reference if the vendor+amount
    query comes back empty.
    """
    remittance = str(tx.get("remittance_information") or "")
    terms: list[str] = []
    for match in re.finditer(
        r"(?i)\bReNr\s*:\s*(\d{2,})\s*/\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})",
        remittance,
    ):
        number, _day, _month, year = match.groups()
        if len(year) == 2:
            year = f"20{year}"
        terms.append(f"{year}/{number}")
        terms.append(number)
    return list(dict.fromkeys(terms))


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

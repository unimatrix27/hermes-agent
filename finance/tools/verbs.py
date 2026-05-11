"""Toolbox verbs for the Mode-B finance reconciliation agent (issue #23).

Each function is single-purpose, deterministic, idempotent where possible,
and writes at most one row per call (no compound transactions). Same code
path is used by ``finance/cli.py`` and by the agent's direct
``from finance.tools import ...`` imports.

Hard invariants enforced here (per #27 + #23):

* ``mark_ignored`` cannot flip ``transactions.ignored=true → false``.
* ``send_match`` is idempotent on natural key
  ``(attachment_sha256, bank_tx_id)`` and is a no-op when an entry
  already exists in ``bank.belege_sent`` for that key.
* ``bank.belege_sent`` is treated as append-only — no UPDATE / DELETE
  code path here.
* Read verbs are pure: no insert / update / delete called from them.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from finance.tools.adapter import (
    InvalidTransition,
    NotFound,
    ToolAdapter,
    ToolError,
)
from finance.tools.graph import GraphMailSender, SendOutcome
from finance.tools.notifier import Notifier, StdoutNotifier


DEFAULT_DATEV_RECIPIENT = "rechnung@lineo.finance"  # placeholder; CLI/agent overrides
DEFAULT_SOURCE_MAILBOX  = "rechnung@lineo.finance"


# ──────────────────────────────────────────────────────────────────────
# Read verbs
# ──────────────────────────────────────────────────────────────────────


def list_open_transactions(
    adapter: ToolAdapter,
    *,
    month: Optional[str] = None,
    vendor: Optional[str] = None,
    status: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Six-bucket status list from ``bank.receipt_status_v``.

    `status` is one of {done, available_to_send, manual_needed, ambiguous,
    missing, ignored}. JSON-shaped dict per row. The agent reads
    `proposed_count` to decide whether to call `get_proposals(tx_id=...)`
    or treat the row as needing a hunter dispatch.
    """
    return adapter.list_status_view(
        month=month, vendor=vendor, status=status,
        limit=limit, offset=offset,
    )


def get_tx_context(
    adapter: ToolAdapter,
    tx_id: int,
    *,
    excerpt_chars: int = 2000,
) -> dict[str, Any]:
    """Everything the agent needs to judge one transaction.

    Includes:
    * the ``bank.transactions`` row
    * every ``bank.receipt_matches`` row pointing at it (with
      ``reason_codes`` + ``legacy_meta`` agent_notes trail)
    * the linked ``bank.receipt_candidates`` for each match, with the
      first ``excerpt_chars`` of ``extracted_text``
    * any ``bank.agent_runs`` history rows (legacy outlook_auto_rule
      pipeline — read-only)
    """
    tx = adapter.get_transaction(tx_id)
    if tx is None:
        raise NotFound(f"no transaction with id {tx_id}")
    matches = adapter.get_matches_for_tx(tx_id)
    candidates_by_id: dict[int, dict[str, Any]] = {}
    for m in matches:
        cid = m.get("receipt_candidate_id")
        if cid is None or cid in candidates_by_id:
            continue
        c = adapter.get_candidate(cid)
        if c is None:
            continue
        if c.get("extracted_text") and isinstance(c["extracted_text"], str):
            c["extracted_text_excerpt"] = c["extracted_text"][:excerpt_chars]
            if len(c["extracted_text"]) > excerpt_chars:
                c["extracted_text_truncated"] = True
            # Drop the full text to keep the JSON small on the wire.
            c.pop("extracted_text", None)
        candidates_by_id[cid] = c
    return {
        "transaction":       tx,
        "matches":           matches,
        "candidates":        list(candidates_by_id.values()),
        "agent_runs_legacy": adapter.get_agent_runs_for_tx(tx_id),
    }


def get_proposals(
    adapter: ToolAdapter,
    *,
    month: Optional[str] = None,
    vendor: Optional[str] = None,
    min_confidence: Optional[str] = None,
) -> list[dict[str, Any]]:
    """All ``decision_status='proposed'`` matches the agent should triage."""
    return adapter.list_proposed_matches(
        month=month, vendor=vendor, min_confidence=min_confidence,
    )


def get_run_history(
    adapter: ToolAdapter,
    *,
    month: Optional[str] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Past agent runs from ``bank.agent_reconcile_runs``."""
    return adapter.list_reconcile_runs(month=month, limit=limit)


def read_anomalies(
    adapter: ToolAdapter,
    *,
    status: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Open + recently-resolved anomaly rows from ``bank.agent_anomalies``."""
    return adapter.list_anomalies(status=status, since=since, limit=limit)


# ──────────────────────────────────────────────────────────────────────
# Job-runner verbs (thin wrappers around #21 / #22 entrypoints)
# ──────────────────────────────────────────────────────────────────────


def run_indexer(
    *,
    mailbox: Optional[str] = None,
    since: Optional[datetime] = None,
    indexer_run: Optional[Callable[..., Any]] = None,
    indexer_adapter: Any = None,
    fetcher: Any = None,
    parser: Any = None,
    config_factory: Optional[Callable[[], Any]] = None,
) -> dict[str, Any]:
    """Wrap ``finance.indexer.run()``.

    The CLI builds the prod adapter/fetcher and passes them through. Tests
    inject a fake ``indexer_run`` callable. Returns the aggregate summary
    in the shape ``{scanned, new, dedup_skipped, portal_required,
    parse_failed}``.
    """
    if indexer_run is None:
        from finance.indexer import run as indexer_run  # type: ignore[no-redef]
    if config_factory is None:
        from finance.indexer import IndexerConfig

        def config_factory() -> Any:  # type: ignore[no-redef]
            cfg = IndexerConfig()
            if mailbox:
                cfg.mailboxes = [mailbox]
            if since:
                cfg.since = since
            return cfg
    config = config_factory()
    kwargs: dict[str, Any] = {"config": config}
    if indexer_adapter is not None:
        kwargs["adapter"] = indexer_adapter
    if fetcher is not None:
        kwargs["fetcher"] = fetcher
    if parser is not None:
        kwargs["parser"] = parser
    agg = indexer_run(**kwargs)
    if hasattr(agg, "to_dict"):
        return agg.to_dict()
    if isinstance(agg, dict):
        return agg
    return {"result": repr(agg)}


def run_matcher(
    matcher_adapter: Any,
    *,
    month: Optional[str] = None,
    vendor: Optional[str] = None,
    matcher_run: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Wrap ``finance.matcher.run_matcher``.

    Counts returned per #23 spec: ``{proposed_new, proposed_updated,
    skipped_existing, txs_seen, txs_with_proposals}``.

    ``month`` / ``vendor`` filter at the adapter boundary: we wrap the
    provided adapter in a ``_FilteringMatcherAdapter`` that constrains
    ``iter_open_transactions``. The underlying matcher logic is untouched.
    """
    if matcher_run is None:
        from finance.matcher import run_matcher as matcher_run  # type: ignore[no-redef]
    wrapped = matcher_adapter
    if month or vendor:
        wrapped = _FilteringMatcherAdapter(matcher_adapter, month=month, vendor=vendor)
    summary = matcher_run(wrapped)
    # MatcherRunSummary -> dict in #22's shape, renamed for #23.
    return {
        "proposed_new":         getattr(summary, "inserted", 0),
        "proposed_updated":     getattr(summary, "updated", 0),
        "skipped_existing":     getattr(summary, "skipped_existing", 0),
        "txs_seen":             getattr(summary, "txs_seen", 0),
        "txs_with_proposals":   getattr(summary, "txs_with_proposals", 0),
    }


class _FilteringMatcherAdapter:
    """Pass-through adapter that filters iter_open_transactions by month/vendor.

    Everything else (iter_candidates, find_existing_match, insert_match,
    update_match_reason_codes) delegates verbatim. The matcher's
    deterministic logic is unchanged — we just narrow its input set.
    """

    def __init__(self, inner: Any, *, month: Optional[str], vendor: Optional[str]) -> None:
        self._inner = inner
        self._month = month
        self._vendor_substr = (vendor or "").lower() or None

    def iter_open_transactions(self):
        for tx in self._inner.iter_open_transactions():
            if self._month and tx.booking_date.strftime("%Y-%m") != self._month:
                continue
            if self._vendor_substr and self._vendor_substr not in (tx.counterparty_name or "").lower():
                continue
            yield tx

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ──────────────────────────────────────────────────────────────────────
# Write verbs
# ──────────────────────────────────────────────────────────────────────


def _validate_reason(reason: str) -> str:
    if not reason or not reason.strip():
        raise ToolError("reason must be a non-empty string")
    return reason.strip()


def approve_match(
    adapter: ToolAdapter,
    match_id: int,
    *,
    reason: str,
    decided_by: str = "llm",
) -> dict[str, Any]:
    """Set ``decision_status='approved'`` on one match row.

    Idempotent on (match_id, decision_status='approved'): a second call
    that finds the row already approved appends another agent_note but
    does not write a duplicate row.
    """
    reason = _validate_reason(reason)
    # Allow approving from 'proposed' or re-approving an already-'approved'
    # row (idempotency-friendly — appends another note). Forbid the
    # send/rejected/ignored terminal states.
    return adapter.update_match_decision(
        match_id=match_id,
        decision_status="approved",
        decided_by=decided_by,
        agent_note=reason,
        expect_current_status=("proposed", "approved"),
    )


def reject_match(
    adapter: ToolAdapter,
    match_id: int,
    *,
    reason: str,
    decided_by: str = "llm",
) -> dict[str, Any]:
    """Set ``decision_status='rejected'`` on one match row."""
    reason = _validate_reason(reason)
    return adapter.update_match_decision(
        match_id=match_id,
        decision_status="rejected",
        decided_by=decided_by,
        agent_note=reason,
        expect_current_status=("proposed", "approved", "rejected"),
    )


def mark_manual_needed(
    adapter: ToolAdapter,
    tx_id: int,
    *,
    reason: str,
    decided_by: str = "llm",
) -> dict[str, Any]:
    """Mark a transaction as needing a human.

    Two paths: if an existing ``manual_needed`` row points at this tx,
    append ``reason`` to its agent_notes trail. Otherwise insert a new
    synthetic ``match_type='manual_review_legacy'`` row carrying the
    reason — same shape as #20's manual_review backfill rows.
    """
    reason = _validate_reason(reason)
    tx = adapter.get_transaction(tx_id)
    if tx is None:
        raise NotFound(f"no transaction with id {tx_id}")
    existing = adapter.find_manual_needed_match(tx_id)
    if existing is not None:
        return adapter.update_match_decision(
            match_id=existing["id"],
            decision_status="manual_needed",
            decided_by=decided_by,
            agent_note=reason,
            expect_current_status=("manual_needed",),
        )
    return adapter.insert_match(
        bank_tx_id=tx_id,
        receipt_candidate_id=None,
        match_type="manual_review_legacy",
        confidence=None,
        decision_status="manual_needed",
        decided_by=decided_by,
        reason_codes=[f"manual_needed_by:{decided_by}"],
        legacy_meta={
            "origin":      "tool_mark_manual_needed",
            "tx_id":       tx_id,
            "reason":      reason,
            "agent_notes": [{
                "ts":              datetime.now(timezone.utc).isoformat(),
                "decided_by":      decided_by,
                "decision_status": "manual_needed",
                "reason":          reason,
            }],
        },
    )


def mark_ignored(
    adapter: ToolAdapter,
    tx_id: int,
    *,
    reason: str,
    decided_by: str = "llm",
) -> dict[str, Any]:
    """Set ``transactions.ignored = true`` for one tx and audit it.

    Per #27 invariant: hard error if the tx is already ``ignored=true``
    — this verb cannot flip ``true → false`` under any circumstance, and
    a redundant ``true → true`` call is treated as the agent
    misunderstanding state and is rejected (no silent no-op) so the
    audit trail stays meaningful.
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
    audit_match = adapter.insert_match(
        bank_tx_id=tx_id,
        receipt_candidate_id=None,
        match_type="manual_review_legacy",
        confidence=None,
        decision_status="ignored",
        decided_by=decided_by,
        reason_codes=[f"ignored_by:{decided_by}"],
        legacy_meta={
            "origin":      "tool_mark_ignored",
            "tx_id":       tx_id,
            "reason":      reason,
            "agent_notes": [{
                "ts":              datetime.now(timezone.utc).isoformat(),
                "decided_by":      decided_by,
                "decision_status": "ignored",
                "reason":          reason,
            }],
        },
    )
    return {"transaction": tx_after, "audit_match": audit_match}


# ──────────────────────────────────────────────────────────────────────
# send_match (four-step pipeline)
# ──────────────────────────────────────────────────────────────────────


def send_match(
    adapter: ToolAdapter,
    match_id: int,
    *,
    graph_sender: GraphMailSender,
    decided_by: str = "llm",
    datev_recipient: str = DEFAULT_DATEV_RECIPIENT,
    source_mailbox: str = DEFAULT_SOURCE_MAILBOX,
    body_template: Optional[Callable[[Mapping[str, Any], Mapping[str, Any]], str]] = None,
) -> dict[str, Any]:
    """Forward an approved match's PDF to DATEV via Graph, then audit.

    Four-step pipeline per #23:

      a. POST sendMail(saveToSentItems=true) with the candidate's PDF
         as a fileAttachment.
      b. Poll Sent Items for the saved copy; capture metadata.
      c. INSERT bank.belege_sent (raw parameterized SQL).
      d. UPDATE bank.receipt_matches.decision_status='sent'.

    Idempotency: keyed on ``(candidate.attachment_sha256,
    match.bank_tx_id)``. A second call that finds an existing
    ``bank.belege_sent`` row for that key returns it verbatim — no
    re-send, no second row.

    Partial-failure semantics:
      * step a/b fail  → no belege_sent row, decision_status untouched,
                         returns ``{"sent": False, "step": ..., "error": ...}``
                         so the agent can surface it via flag_anomaly.
      * step c succeeds + step d fails → leave the belege_sent row (the
                         mail is real), return ``{"sent": True,
                         "match_status_updated": False, "warning": ...}``
                         so the agent flags the partial state.
    """
    match = adapter.get_match(match_id)
    if match is None:
        raise NotFound(f"no match with id {match_id}")
    if match["decision_status"] != "approved":
        raise InvalidTransition(
            f"match {match_id} is {match['decision_status']!r}, "
            "send_match only operates on 'approved'"
        )
    candidate_id = match.get("receipt_candidate_id")
    if candidate_id is None:
        raise ToolError(
            f"match {match_id} has no receipt_candidate_id; "
            "synthetic / portal-only matches cannot be sent automatically"
        )
    candidate = adapter.get_candidate(candidate_id)
    if candidate is None:
        raise NotFound(f"match {match_id} references missing candidate {candidate_id}")
    tx = adapter.get_transaction(match["bank_tx_id"])
    if tx is None:
        raise NotFound(f"match {match_id} references missing tx {match['bank_tx_id']}")

    attachment_sha = candidate.get("attachment_sha256")
    bank_tx_id = match["bank_tx_id"]

    # Step 0: idempotency check.
    existing_bs = adapter.find_belege_sent_by_natural_key(
        attachment_sha256=attachment_sha, bank_tx_id=bank_tx_id,
    )
    if existing_bs is not None:
        # Mirror the current match state so the agent can spot partial
        # rows that never got step (d).
        return {
            "sent":                  True,
            "idempotent":            True,
            "belege_sent":           existing_bs,
            "match_status":          match["decision_status"],
            "match_status_updated":  match["decision_status"] == "sent",
            "warning": (
                "decision_status is not 'sent' — earlier send completed "
                "step (c) but not (d); agent should re-update or flag"
            ) if match["decision_status"] != "sent" else None,
        }

    blob_path_str = candidate.get("local_blob_path")
    if not blob_path_str:
        raise ToolError(
            f"candidate {candidate_id} has no local_blob_path; cannot send"
        )
    blob_path = Path(blob_path_str)
    if not blob_path.exists():
        raise ToolError(f"candidate {candidate_id} local_blob_path {blob_path} not found on disk")

    attachment_name = (
        candidate.get("attachment_name")
        or f"receipt_{candidate_id}.pdf"
    )
    subject = _build_subject(tx, candidate)
    body_text = (body_template or _default_body)(tx, candidate)

    # Step (a) + (b): Graph send + sent-items lookup.
    outcome: SendOutcome = graph_sender.send(
        from_mailbox=source_mailbox,
        to_recipient=datev_recipient,
        subject=subject,
        body_text=body_text,
        attachment_path=blob_path,
        attachment_name=attachment_name,
    )
    if not outcome.ok:
        return {
            "sent":  False,
            "step":  outcome.step,
            "error": outcome.error,
        }
    assert outcome.metadata is not None  # ok=True invariant

    # Step (c): bank.belege_sent insert. If this fails we surface it but
    # do not retry — the mail is out; a duplicate retry would write a
    # second row.
    try:
        belege_row = adapter.insert_belege_sent(
            outlook_message_id=outcome.metadata.outlook_message_id,
            internet_message_id=outcome.metadata.internet_message_id,
            source_mailbox=source_mailbox,
            sent_at=outcome.metadata.sent_at,
            recipient=datev_recipient,
            subject=outcome.metadata.subject or subject,
            attachment_filenames=outcome.metadata.attachment_filenames or [attachment_name],
            # via must match the existing bank.belege_sent_via_check
            # constraint (outlook_auto_rule | manual_inbox_match |
            # agent_match | manual). We're the new agent path.
            via="agent_match",
            bank_tx_id=bank_tx_id,
            bank_tx_amount=float(tx["amount"]) if tx.get("amount") is not None else None,
            bank_tx_booking_date=tx.get("booking_date"),
            confidence=match.get("confidence"),
            reasoning=_reasoning_for_audit(match, candidate, attachment_sha),
            attachment_sha256=attachment_sha,
        )
    except Exception as exc:  # noqa: BLE001 — surface for the agent
        return {
            "sent":  True,
            "step":  "belege_sent_insert",
            "error": f"{type(exc).__name__}: {exc}",
            "warning": (
                "the mail went out (outlook_message_id captured) but the "
                "belege_sent audit row was NOT written. Manual intervention required."
            ),
            "sent_metadata": asdict(outcome.metadata),
            "match_status_updated": False,
        }

    # Step (d): receipt_matches.decision_status='sent'.
    match_updated = True
    update_warning: Optional[str] = None
    try:
        adapter.update_match_decision(
            match_id=match_id,
            decision_status="sent",
            decided_by=decided_by,
            agent_note=(
                f"sent via send_match; belege_sent_id={belege_row['id']} "
                f"outlook_message_id={outcome.metadata.outlook_message_id}"
            ),
            expect_current_status=("approved",),
        )
    except Exception as exc:  # noqa: BLE001
        match_updated = False
        update_warning = (
            f"belege_sent row {belege_row['id']} written, but "
            f"decision_status update failed: {type(exc).__name__}: {exc}"
        )

    return {
        "sent":                 True,
        "belege_sent":          belege_row,
        "match_id":             match_id,
        "match_status_updated": match_updated,
        "warning":              update_warning,
        "sent_metadata":        asdict(outcome.metadata),
    }


def _build_subject(tx: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    extracted = candidate.get("extracted_json") or {}
    invoice_no = extracted.get("invoice_number") or "n/a"
    vendor = extracted.get("vendor") or "vendor"
    booking = tx.get("booking_date")
    booking_str = booking.isoformat() if hasattr(booking, "isoformat") else str(booking)
    amount = tx.get("amount")
    amount_str = f"{float(amount):.2f}" if amount is not None else "?"
    return f"[hermes] {vendor} {invoice_no} — €{amount_str} ({booking_str})"


def _default_body(tx: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    extracted = candidate.get("extracted_json") or {}
    lines = [
        "Automatischer Beleg-Forward (hermes Mode-B Reconcile).",
        "",
        f"Bank tx id      : {tx.get('id')}",
        f"Booking date    : {tx.get('booking_date')}",
        f"Amount          : {tx.get('amount')} {tx.get('currency') or ''}",
        f"Counterparty    : {tx.get('counterparty_name')}",
        f"Remittance      : {(tx.get('remittance_information') or '').strip()}",
        "",
        f"Candidate id    : {candidate.get('id')}",
        f"Attachment SHA  : {candidate.get('attachment_sha256')}",
        f"Vendor          : {extracted.get('vendor')}",
        f"Invoice number  : {extracted.get('invoice_number')}",
        f"Invoice date    : {extracted.get('invoice_date')}",
        f"Gross amount    : {extracted.get('gross_amount')}",
    ]
    return "\n".join(lines)


def _reasoning_for_audit(
    match: Mapping[str, Any],
    candidate: Mapping[str, Any],
    attachment_sha: Optional[str],
) -> str:
    parts = [
        f"match_id={match['id']}",
        f"candidate_id={match.get('receipt_candidate_id')}",
        f"match_type={match.get('match_type')}",
        f"confidence={match.get('confidence')}",
        f"sha256={attachment_sha}",
    ]
    return " ".join(parts)


# ──────────────────────────────────────────────────────────────────────
# flag_anomaly + search_for_missing_receipt + finalize_run
# ──────────────────────────────────────────────────────────────────────


def flag_anomaly(
    adapter: ToolAdapter,
    *,
    tx_id: Optional[int],
    reason: str,
    severity: str,
    raised_by: str = "llm",
    run_id: Optional[int] = None,
    legacy_meta: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Insert one row into ``bank.agent_anomalies``.

    Per #27: this verb does NOT mutate transaction or match state, and
    is the agent's only escalation path. Severity is constrained to
    {info, warn, block}.
    """
    reason = _validate_reason(reason)
    if severity not in ("info", "warn", "block"):
        raise ToolError(f"severity must be info|warn|block, got {severity!r}")
    if tx_id is not None:
        # Defensive: the table allows NULL bank_tx_id (system-level
        # anomalies) but verifying when the caller supplied a value
        # keeps the audit trail meaningful.
        tx = adapter.get_transaction(tx_id)
        if tx is None:
            raise NotFound(f"no transaction with id {tx_id}")
    return adapter.insert_anomaly(
        bank_tx_id=tx_id,
        reason=reason,
        severity=severity,
        raised_by=raised_by,
        run_id=run_id,
        legacy_meta=legacy_meta,
    )


def search_for_missing_receipt(
    tx_id: int,
    *,
    skill_registry: Optional[Any] = None,
) -> dict[str, Any]:
    """Dispatch the hunter subagent (skill from #28).

    #28 ships the ``find-missing-receipt`` skill plus the hunter sub-tools
    (search_inboxes / search_attachments / fetch_attachment /
    propose_match). #23 ships ONLY this thin dispatch wrapper. If no
    hunter skill is registered yet, return the graceful-degradation shape
    documented in #23 with exit code 0 — never raise. Tests assert this.

    ``skill_registry`` is optional: tests inject a fake; in production
    a future implementation will look up the registered hunter via
    ``hermes_cli.skills`` or similar. Until #28 lands, the registry is
    always None.
    """
    if skill_registry is None:
        return {
            "tx_id":              tx_id,
            "candidates_proposed": [],
            "notes":              "hunter skill not registered yet (closes #28)",
            "skill_present":      False,
        }
    dispatch = getattr(skill_registry, "dispatch_find_missing_receipt", None)
    if dispatch is None:
        return {
            "tx_id":              tx_id,
            "candidates_proposed": [],
            "notes":              "skill_registry present but no find_missing_receipt skill",
            "skill_present":      False,
        }
    result = dispatch(tx_id=tx_id)
    if isinstance(result, dict):
        result.setdefault("tx_id", tx_id)
        result.setdefault("skill_present", True)
        return result
    return {
        "tx_id":              tx_id,
        "candidates_proposed": [],
        "notes":              f"unexpected result type {type(result).__name__}",
        "skill_present":      True,
    }


def finalize_run(
    adapter: ToolAdapter,
    *,
    summary_md: str,
    proposed_changes: Optional[Mapping[str, Any]] = None,
    tool_call_summary: Optional[Mapping[str, Any]] = None,
    invoked_by: str = "llm",
    notes: Optional[str] = None,
    notifier: Optional[Notifier] = None,
    notification_title: Optional[str] = "hermes reconcile run",
) -> dict[str, Any]:
    """Persist the run report and dispatch the summary via the notifier.

    Returns the inserted ``bank.agent_reconcile_runs`` row plus a flag
    indicating whether the notifier received the summary. The default
    notifier (``StdoutNotifier``) prints to stdout; cron's
    ``deliver: telegram`` then forwards that to Telegram. Tests supply
    ``RecordingNotifier`` to assert delivery.

    ``proposed_changes`` carries the optional threshold-gated self-audit
    (#27 self-audit); never auto-applied — only queued for human review.
    """
    if not summary_md or not summary_md.strip():
        raise ToolError("summary_md must be a non-empty string")
    row = adapter.insert_reconcile_run(
        summary_md=summary_md,
        proposed_changes=proposed_changes,
        tool_call_summary=tool_call_summary,
        invoked_by=invoked_by,
        notes=notes,
    )
    notifier = notifier or StdoutNotifier()
    notifier.notify(summary_md, title=notification_title)
    return {
        "reconcile_run":         row,
        "notification_dispatched": True,
    }

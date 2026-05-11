"""`finance-reconcile` CLI — every verb in finance.tools as a subcommand.

Read verbs emit JSON on stdout. Write / side-effect verbs emit a status
line ("ok: <what changed>" or "error: <why>") and a non-zero exit code on
failure. Every subcommand also exposes the same behavior as a Python
import from ``finance.tools`` — the agent (#25) uses the same code path.

Usage:

    finance-reconcile list_open_transactions --month 2026-04 | jq .
    finance-reconcile approve_match 123 --reason "vendor + amount + date"
    finance-reconcile send_match 123
    finance-reconcile finalize_run --summary "..." --notes "..."

Environment:

    SUPABASE_DB_URL                 — required for any verb that hits Postgres
    LINEO_MS_TENANT_ID / CLIENT_ID  — required for send_match (Graph)
    HERMES_DATEV_RECIPIENT          — overrides the default DATEV mailbox
    HERMES_FROM_MAILBOX             — overrides the default from-mailbox
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from finance.tools.adapter import (
    InvalidTransition,
    NotFound,
    PostgresToolAdapter,
    ToolError,
)
from finance.tools import verbs


# ──────────────────────────────────────────────────────────────────────
# JSON encoding for psycopg2 rows (date / datetime / Decimal)
# ──────────────────────────────────────────────────────────────────────


def _json_default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, Path):
        return str(o)
    if is_dataclass(o):
        return asdict(o)
    raise TypeError(f"unserializable: {type(o).__name__}")


def _emit_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, default=_json_default, indent=2, sort_keys=False)
    sys.stdout.write("\n")


# ──────────────────────────────────────────────────────────────────────
# Database connection helper
# ──────────────────────────────────────────────────────────────────────


def _connect() -> Any:
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        print("error: SUPABASE_DB_URL not set", file=sys.stderr)
        sys.exit(2)
    try:
        import psycopg2  # noqa: WPS433
    except ImportError:
        print("error: psycopg2 not installed", file=sys.stderr)
        sys.exit(2)
    return psycopg2.connect(url)


def _adapter() -> PostgresToolAdapter:
    return PostgresToolAdapter(_connect())


# ──────────────────────────────────────────────────────────────────────
# Argument parsing
# ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="finance-reconcile",
        description="Deterministic toolbox for the Mode-B reconcile agent.",
    )
    sp = p.add_subparsers(dest="verb", required=True)

    # ── Read verbs ──
    list_p = sp.add_parser("list_open_transactions",
                           help="six-bucket status list (JSON)")
    list_p.add_argument("--month")
    list_p.add_argument("--vendor")
    list_p.add_argument("--status")
    list_p.add_argument("--limit", type=int)
    list_p.add_argument("--offset", type=int, default=0)

    ctx_p = sp.add_parser("get_tx_context",
                          help="full context for one tx (JSON)")
    ctx_p.add_argument("tx_id", type=int)
    ctx_p.add_argument("--excerpt-chars", type=int, default=2000)

    prop_p = sp.add_parser("get_proposals",
                           help="all proposed matches awaiting decision (JSON)")
    prop_p.add_argument("--month")
    prop_p.add_argument("--vendor")
    prop_p.add_argument("--min-confidence", choices=("low", "medium", "high", "very_high"))

    hist_p = sp.add_parser("get_run_history",
                           help="past agent reconcile runs (JSON)")
    hist_p.add_argument("--month")
    hist_p.add_argument("--limit", type=int, default=20)

    an_p = sp.add_parser("read_anomalies",
                         help="agent_anomalies rows (JSON)")
    an_p.add_argument("--status", choices=("open", "acknowledged", "resolved"))
    an_p.add_argument("--since", help="ISO datetime (e.g. 2026-04-01)")
    an_p.add_argument("--limit", type=int, default=100)

    # ── Job-runner verbs ──
    idx_p = sp.add_parser("run_indexer", help="run the Graph indexer (#21)")
    idx_p.add_argument("--mailbox")
    idx_p.add_argument("--since", help="ISO date / datetime")

    match_p = sp.add_parser("run_matcher", help="run the deterministic matcher (#22)")
    match_p.add_argument("--month")
    match_p.add_argument("--vendor")

    # ── Write verbs ──
    appr_p = sp.add_parser("approve_match",
                           help="set decision_status='approved' on a match row")
    appr_p.add_argument("match_id", type=int)
    appr_p.add_argument("--reason", required=True)
    appr_p.add_argument("--decided-by", default="user")  # CLI defaults to 'user'

    rej_p = sp.add_parser("reject_match",
                          help="set decision_status='rejected' on a match row")
    rej_p.add_argument("match_id", type=int)
    rej_p.add_argument("--reason", required=True)
    rej_p.add_argument("--decided-by", default="user")

    mn_p = sp.add_parser("mark_manual_needed",
                         help="mark a tx as needing human review")
    mn_p.add_argument("tx_id", type=int)
    mn_p.add_argument("--reason", required=True)
    mn_p.add_argument("--decided-by", default="user")

    mi_p = sp.add_parser("mark_ignored",
                         help="set transactions.ignored=true (one-way)")
    mi_p.add_argument("tx_id", type=int)
    mi_p.add_argument("--reason", required=True)
    mi_p.add_argument("--decided-by", default="user")

    # ── Side-effect verbs ──
    sm_p = sp.add_parser("send_match",
                         help="forward the approved match's PDF to DATEV via Graph")
    sm_p.add_argument("match_id", type=int)
    sm_p.add_argument("--decided-by", default="user")
    sm_p.add_argument("--datev-recipient",
                      default=os.environ.get("HERMES_DATEV_RECIPIENT",
                                             verbs.DEFAULT_DATEV_RECIPIENT))
    sm_p.add_argument("--from-mailbox",
                      default=os.environ.get("HERMES_FROM_MAILBOX",
                                             verbs.DEFAULT_SOURCE_MAILBOX))

    fa_p = sp.add_parser("flag_anomaly",
                         help="insert one row into bank.agent_anomalies")
    fa_p.add_argument("--tx-id", type=int)
    fa_p.add_argument("--reason", required=True)
    fa_p.add_argument("--severity", required=True, choices=("info", "warn", "block"))
    fa_p.add_argument("--raised-by", default="user")
    fa_p.add_argument("--run-id", type=int)

    sf_p = sp.add_parser("search_for_missing_receipt",
                         help="dispatch hunter subagent (#28); graceful when unavailable")
    sf_p.add_argument("tx_id", type=int)

    fr_p = sp.add_parser("finalize_run",
                         help="write reconcile_runs row + dispatch summary via notifier")
    fr_p.add_argument("--summary", required=True,
                      help="markdown summary string (use --summary-file for big payloads)")
    fr_p.add_argument("--summary-file", help="read summary_md from this file instead")
    fr_p.add_argument("--proposed-changes",
                      help="optional JSON describing self-audit proposals")
    fr_p.add_argument("--notes")
    fr_p.add_argument("--invoked-by", default="user")

    return p


# ──────────────────────────────────────────────────────────────────────
# Verb handlers
# ──────────────────────────────────────────────────────────────────────


def _parse_since(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        # Accept either YYYY-MM-DD or ISO datetime.
        if "T" in value:
            return datetime.fromisoformat(value)
        return datetime.combine(date.fromisoformat(value), datetime.min.time()).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ToolError(f"invalid --since {value!r}: {exc}") from exc


def _run(args: argparse.Namespace) -> int:
    verb = args.verb
    # ── Read verbs ──
    if verb == "list_open_transactions":
        adapter = _adapter()
        rows = verbs.list_open_transactions(
            adapter,
            month=args.month, vendor=args.vendor, status=args.status,
            limit=args.limit, offset=args.offset,
        )
        _emit_json(rows)
        return 0
    if verb == "get_tx_context":
        adapter = _adapter()
        ctx = verbs.get_tx_context(adapter, args.tx_id, excerpt_chars=args.excerpt_chars)
        _emit_json(ctx)
        return 0
    if verb == "get_proposals":
        adapter = _adapter()
        rows = verbs.get_proposals(
            adapter, month=args.month, vendor=args.vendor, min_confidence=args.min_confidence,
        )
        _emit_json(rows)
        return 0
    if verb == "get_run_history":
        adapter = _adapter()
        rows = verbs.get_run_history(adapter, month=args.month, limit=args.limit)
        _emit_json(rows)
        return 0
    if verb == "read_anomalies":
        adapter = _adapter()
        since = _parse_since(args.since)
        rows = verbs.read_anomalies(adapter, status=args.status, since=since, limit=args.limit)
        _emit_json(rows)
        return 0

    # ── Job-runner verbs ──
    if verb == "run_indexer":
        from finance.indexer import (
            DEFAULT_TOKEN_FILE,
            PostgresIndexerAdapter,
            _build_graph_fetcher,
        )
        conn = _connect()
        idx_adapter = PostgresIndexerAdapter(conn)
        fetcher = _build_graph_fetcher(DEFAULT_TOKEN_FILE)
        result = verbs.run_indexer(
            mailbox=args.mailbox,
            since=_parse_since(args.since),
            indexer_adapter=idx_adapter,
            fetcher=fetcher,
        )
        _emit_json(result)
        return 0
    if verb == "run_matcher":
        from finance.matcher import PostgresMatcherAdapter
        conn = _connect()
        m_adapter = PostgresMatcherAdapter(conn)
        result = verbs.run_matcher(m_adapter, month=args.month, vendor=args.vendor)
        _emit_json(result)
        return 0

    # ── Write verbs ──
    if verb == "approve_match":
        adapter = _adapter()
        row = verbs.approve_match(adapter, args.match_id, reason=args.reason, decided_by=args.decided_by)
        print(f"ok: match {row['id']} → approved (decided_by={row['decided_by']})")
        return 0
    if verb == "reject_match":
        adapter = _adapter()
        row = verbs.reject_match(adapter, args.match_id, reason=args.reason, decided_by=args.decided_by)
        print(f"ok: match {row['id']} → rejected (decided_by={row['decided_by']})")
        return 0
    if verb == "mark_manual_needed":
        adapter = _adapter()
        row = verbs.mark_manual_needed(adapter, args.tx_id, reason=args.reason, decided_by=args.decided_by)
        print(f"ok: tx {args.tx_id} → manual_needed (match_id={row['id']})")
        return 0
    if verb == "mark_ignored":
        adapter = _adapter()
        result = verbs.mark_ignored(adapter, args.tx_id, reason=args.reason, decided_by=args.decided_by)
        print(
            f"ok: tx {args.tx_id} → ignored=true; "
            f"audit_match_id={result['audit_match']['id']}"
        )
        return 0

    # ── Side-effect verbs ──
    if verb == "send_match":
        from finance.indexer import DEFAULT_TOKEN_FILE, DelegatedTokenProvider
        from finance.tools.graph import LiveGraphMailSender
        adapter = _adapter()
        # Override the indexer's default scope (Mail.Read*) with Mail.Send;
        # the on-host refresh token has both scopes consented but the
        # refresh-grant returns a token narrowed to what we request.
        token_provider = DelegatedTokenProvider(
            token_file=DEFAULT_TOKEN_FILE,
            scope="User.Read Mail.Send Mail.Send.Shared Mail.Read offline_access",
        )
        sender = LiveGraphMailSender(token_provider=token_provider)
        result = verbs.send_match(
            adapter, args.match_id,
            graph_sender=sender,
            decided_by=args.decided_by,
            datev_recipient=args.datev_recipient,
            source_mailbox=args.from_mailbox,
        )
        if not result.get("sent"):
            print(f"error: send_match failed at step={result.get('step')}: {result.get('error')}",
                  file=sys.stderr)
            return 3
        belege = result.get("belege_sent") or {}
        bs_id = belege.get("id")
        warning = result.get("warning")
        if result.get("idempotent"):
            print(f"ok: send_match idempotent — belege_sent id={bs_id} already exists")
        elif not result.get("match_status_updated"):
            print(f"ok-partial: belege_sent id={bs_id} written; match status NOT updated; "
                  f"warning={warning}", file=sys.stderr)
            return 4
        else:
            print(f"ok: send_match completed — belege_sent id={bs_id}; match {args.match_id} → sent")
        # Always emit the structured result on stdout *after* the status line so JSON consumers can capture it.
        _emit_json(result)
        return 0

    if verb == "flag_anomaly":
        adapter = _adapter()
        row = verbs.flag_anomaly(
            adapter, tx_id=args.tx_id, reason=args.reason, severity=args.severity,
            raised_by=args.raised_by, run_id=args.run_id,
        )
        print(f"ok: anomaly id={row['id']} severity={row['severity']} status=open")
        return 0

    if verb == "search_for_missing_receipt":
        # Per #23: ships as thin dispatch wrapper. Until #28 lands, no skill
        # registry is wired — graceful degradation path. JSON on stdout so
        # the agent can read it like any other tool result.
        result = verbs.search_for_missing_receipt(args.tx_id)
        _emit_json(result)
        return 0

    if verb == "finalize_run":
        adapter = _adapter()
        if args.summary_file:
            summary_md = Path(args.summary_file).read_text(encoding="utf-8")
        else:
            summary_md = args.summary
        proposed: Optional[dict[str, Any]] = None
        if args.proposed_changes:
            try:
                proposed = json.loads(args.proposed_changes)
            except json.JSONDecodeError as exc:
                print(f"error: --proposed-changes is not valid JSON: {exc}", file=sys.stderr)
                return 2
        # Notifier is StdoutNotifier by default; cron's deliver:telegram picks
        # up stdout. Tests use RecordingNotifier directly via the Python API.
        result = verbs.finalize_run(
            adapter,
            summary_md=summary_md,
            proposed_changes=proposed,
            notes=args.notes,
            invoked_by=args.invoked_by,
        )
        run_row = result["reconcile_run"]
        print(f"ok: reconcile_run id={run_row['id']} written; "
              f"notification_dispatched={result['notification_dispatched']}",
              file=sys.stderr)
        return 0

    print(f"error: unknown verb {verb!r}", file=sys.stderr)
    return 2


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except NotFound as exc:
        print(f"error: not_found: {exc}", file=sys.stderr)
        return 5
    except InvalidTransition as exc:
        print(f"error: invalid_transition: {exc}", file=sys.stderr)
        return 6
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

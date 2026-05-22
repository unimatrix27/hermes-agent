"""``finance-reconcile-v2`` CLI — every v2 verb as a subcommand.

Read verbs emit JSON on stdout. Write / side-effect verbs emit a status
line on stderr and a JSON payload on stdout (so consumers can capture
either depending on their needs).

Usage:

    finance-reconcile-v2 list_open_txs --month 2026-04 | jq .
    finance-reconcile-v2 get_tx_context 123
    finance-reconcile-v2 search_inbox --vendor Vodafone --amount 39.99 \\
        --date-from 2026-04-01 --date-to 2026-04-30
    finance-reconcile-v2 send_beleg --tx-id 123 --mail-file mail.json \\
        --attachment-name receipt.pdf
    finance-reconcile-v2 mark_ignored 123 --reason "Lohn — not a business expense"
    finance-reconcile-v2 finalize_run --summary "..." --notes-json '{"month":"2026-04"}'

Environment:

    SUPABASE_DB_URL                — required for any Postgres write
    LINEO_MS_TENANT_ID / CLIENT_ID — required for search_inbox / send_beleg
    HERMES_RECONCILE_V2_RULES_PATH — overrides the ignore_rules.md path
    HERMES_RECONCILE_V2_MAILBOX    — overrides the source mailbox
    HERMES_RECONCILE_V2_RECIPIENT  — overrides the DATEV recipient
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

from finance.reconcile_v2 import verbs
from finance.reconcile_v2.adapter import (
    InvalidTransition,
    NotFound,
    PostgresAdapter,
    ToolError,
)
from finance.reconcile_v2.graph import (
    DEFAULT_DATEV_RECIPIENT,
    DEFAULT_SOURCE_MAILBOX,
    LiveInboxClient,
    LiveMailSender,
)


# ──────────────────────────────────────────────────────────────────────
# JSON encoding
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


def _emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, default=_json_default, indent=2, sort_keys=False)
    sys.stdout.write("\n")


# ──────────────────────────────────────────────────────────────────────
# Connection + clients
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


def _mailbox(args: argparse.Namespace) -> str:
    return (
        getattr(args, "mailbox", None)
        or os.environ.get("HERMES_RECONCILE_V2_MAILBOX")
        or DEFAULT_SOURCE_MAILBOX
    )


def _recipient(args: argparse.Namespace) -> str:
    return (
        getattr(args, "datev_recipient", None)
        or os.environ.get("HERMES_RECONCILE_V2_RECIPIENT")
        or DEFAULT_DATEV_RECIPIENT
    )


def _ignore_rules_path(args: argparse.Namespace) -> Optional[Path]:
    raw = (
        getattr(args, "ignore_rules", None)
        or os.environ.get("HERMES_RECONCILE_V2_RULES_PATH")
    )
    return Path(raw) if raw else None


def _normalize_run_history_row(row: dict[str, Any]) -> dict[str, Any]:
    """Expose v1-style ``tool_call_summary`` even for v2 rows.

    Older reconcile instructions expect get_run_history rows to carry a
    parsed JSON object under ``tool_call_summary``. v2 stores structured
    notes either in that jsonb column (Postgres) or ``notes`` (in-memory
    tests / older rows), so normalize both shapes for the CLI boundary.
    """
    out = dict(row)
    raw = out.get("tool_call_summary") or out.get("notes")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"notes": raw}
    elif isinstance(raw, dict):
        parsed = raw
    else:
        parsed = {}
    out["tool_call_summary"] = parsed
    return out


# ──────────────────────────────────────────────────────────────────────
# Argument parsing
# ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="finance-reconcile-v2",
        description="Simplified reconcile toolbox — 6 verbs against the "
                    "3-state TX model (#31).",
    )
    sp = p.add_subparsers(dest="verb", required=True)

    list_p = sp.add_parser("list_open_txs", aliases=["list_open_transactions"],
                           help="TX not ignored and not in belege_sent")
    list_p.add_argument("--month")
    list_p.add_argument("--limit", type=int)
    list_p.add_argument("--offset", type=int, default=0)
    list_p.add_argument("--ignore-rules", dest="ignore_rules")

    ctx_p = sp.add_parser("get_tx_context",
                          help="TX details + likely-relevant mails (auto-search)")
    ctx_p.add_argument("tx_id", type=int, nargs="?")
    ctx_p.add_argument("--tx-id", dest="tx_id_flag", type=int,
                       help="v1-compatible spelling for the transaction id")
    ctx_p.add_argument("--mailbox")
    ctx_p.add_argument("--date-window-days", type=int, default=30)
    ctx_p.add_argument("--max-results", type=int, default=10)
    ctx_p.add_argument("--no-inbox", action="store_true",
                       help="skip the inbox auto-search; DB context only")

    s_p = sp.add_parser("search_inbox",
                        help="vendor / amount / date_window → mails + attachments")
    s_p.add_argument("--mailbox")
    s_p.add_argument("--vendor")
    s_p.add_argument("--amount", type=float)
    s_p.add_argument("--date-from", dest="date_from",
                     type=lambda v: date.fromisoformat(v))
    s_p.add_argument("--date-to", dest="date_to",
                     type=lambda v: date.fromisoformat(v))
    s_p.add_argument("--message-id", dest="message_id")
    s_p.add_argument("--max-results", type=int, default=25)

    sp.add_parser("run_indexer", help="v1 compatibility no-op; v2 indexer is external/idempotent")

    rm_p = sp.add_parser("run_matcher", help="v1 compatibility no-op; matcher is run externally")
    rm_p.add_argument("--month")

    am_p = sp.add_parser("approve_match", help="mark a receipt_match approved")
    am_p.add_argument("--match-id", dest="match_id", type=int, required=True)
    am_p.add_argument("--reason")
    am_p.add_argument("--decided-by", dest="decided_by", default="llm")

    sb_p = sp.add_parser("send_beleg",
                         help="forward an attachment to DATEV + write belege_sent")
    sb_p.add_argument("--tx-id", dest="tx_id", type=int, required=True)
    grp = sb_p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--mail-file", dest="mail_file",
                     help="path to JSON mail dict (as produced by search_inbox)")
    grp.add_argument("--mail-json", dest="mail_json",
                     help="JSON mail dict inline")
    sb_p.add_argument("--attachment-name", dest="attachment_name")
    sb_p.add_argument("--mailbox")
    sb_p.add_argument("--datev-recipient", dest="datev_recipient")
    sb_p.add_argument("--reasoning")
    sb_p.add_argument("--decided-by", dest="decided_by", default="llm")

    sm_p = sp.add_parser("send_match",
                         help="send an approved receipt_match via DATEV + mark sent")
    sm_p.add_argument("--match-id", dest="match_id", type=int, required=True)
    sm_p.add_argument("--attachment-name", dest="attachment_name")
    sm_p.add_argument("--mailbox")
    sm_p.add_argument("--datev-recipient", dest="datev_recipient")
    sm_p.add_argument("--from-mailbox", dest="mailbox")
    sm_p.add_argument("--reasoning")
    sm_p.add_argument("--decided-by", dest="decided_by", default="llm")

    mi_p = sp.add_parser("mark_ignored",
                         help="set transactions.ignored=true (one-way)")
    mi_p.add_argument("tx_id", type=int)
    mi_p.add_argument("--reason", required=True)
    mi_p.add_argument("--decided-by", dest="decided_by", default="llm")

    mm_p = sp.add_parser("mark_manual_needed",
                         help="mark/create receipt_match as manual_needed")
    mm_p.add_argument("--tx-id", dest="tx_id", type=int, required=True)
    mm_p.add_argument("--reason", required=True)
    mm_p.add_argument("--decided-by", dest="decided_by", default="llm")

    fa_p = sp.add_parser("flag_anomaly",
                         help="insert an agent anomaly for human review")
    fa_p.add_argument("--tx-id", dest="tx_id", type=int)
    fa_p.add_argument("--reason", required=True)
    fa_p.add_argument("--severity", choices=["info", "warn", "block"], default="warn")
    fa_p.add_argument("--decided-by", dest="decided_by", default="llm")

    ra_p = sp.add_parser("read_anomalies",
                         help="v1 compatibility read: list agent anomalies")
    ra_p.add_argument("--tx-id", dest="tx_id", type=int)
    ra_p.add_argument("--status", default="open")
    ra_p.add_argument("--limit", type=int, default=100)

    rh_p = sp.add_parser("get_run_history",
                         help="v1 compatibility read: list reconcile runs")
    rh_p.add_argument("--month")
    rh_p.add_argument("--limit", type=int, default=12)

    fr_p = sp.add_parser("finalize_run",
                         help="write reconcile_run row + notify")
    fr_p.add_argument("--summary", "--summary-md", dest="summary", required=True)
    fr_p.add_argument("--notes-json", "--tool-call-summary", dest="notes_json",
                      help="JSON dict carried as tool_call_summary")
    fr_p.add_argument("--proposed-changes", dest="proposed_changes",
                      help="accepted for v1 compatibility and stored inside notes JSON")
    fr_p.add_argument("--invoked-by", dest="invoked_by", default="user")

    return p


# ──────────────────────────────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────────────────────────────


def _run(args: argparse.Namespace) -> int:
    v = args.verb

    if v in {"list_open_txs", "list_open_transactions"}:
        adapter = PostgresAdapter(_connect())
        result = verbs.list_open_txs(
            adapter,
            month=args.month, limit=args.limit, offset=args.offset,
            ignore_rules_path=_ignore_rules_path(args),
        )
        _emit(result)
        return 0

    if v == "get_tx_context":
        tx_id = args.tx_id_flag if args.tx_id_flag is not None else args.tx_id
        if tx_id is None:
            raise ToolError("get_tx_context requires TX id as positional argument or --tx-id")
        adapter = PostgresAdapter(_connect())
        inbox = None if args.no_inbox else LiveInboxClient()
        ctx = verbs.get_tx_context(
            adapter, tx_id,
            inbox=inbox, mailbox=_mailbox(args),
            date_window_days=args.date_window_days,
            max_results=args.max_results,
        )
        _emit(ctx)
        return 0

    if v == "search_inbox":
        inbox = LiveInboxClient()
        result = verbs.search_inbox(
            inbox=inbox, mailbox=_mailbox(args),
            vendor=args.vendor, amount=args.amount,
            date_from=args.date_from, date_to=args.date_to,
            message_id=args.message_id, max_results=args.max_results,
        )
        _emit(result)
        return 0

    if v == "run_indexer":
        result = {
            "ok": True,
            "verb": "run_indexer",
            "status": "noop_v2_external_indexer",
            "message": "v2 indexes receipt_candidates outside this compatibility CLI; no DB writes performed.",
        }
        print("ok: run_indexer compatibility no-op; no DB writes performed", file=sys.stderr)
        _emit(result)
        return 0

    if v == "run_matcher":
        result = {
            "ok": True,
            "verb": "run_matcher",
            "month": args.month,
            "status": "noop_v2_external_matcher",
            "message": "v2 matcher/backfill is run outside this compatibility CLI; no DB writes performed.",
        }
        print("ok: run_matcher compatibility no-op; no DB writes performed", file=sys.stderr)
        _emit(result)
        return 0

    if v == "approve_match":
        adapter = PostgresAdapter(_connect())
        result = verbs.approve_match(
            adapter,
            match_id=args.match_id,
            reason=args.reason,
            decided_by=args.decided_by,
        )
        status = "idempotent" if result.get("idempotent") else "approved"
        print(f"ok: receipt_match {args.match_id} → {status}", file=sys.stderr)
        _emit(result)
        return 0

    if v == "send_beleg":
        adapter = PostgresAdapter(_connect())
        if args.mail_file:
            mail = json.loads(Path(args.mail_file).read_text(encoding="utf-8"))
        else:
            mail = json.loads(args.mail_json)
        sender = LiveMailSender()
        result = verbs.send_beleg(
            adapter,
            tx_id=args.tx_id, mail=mail, sender=sender,
            attachment_name=args.attachment_name,
            datev_recipient=_recipient(args),
            source_mailbox=_mailbox(args),
            decided_by=args.decided_by,
            reasoning=args.reasoning,
        )
        if not result.get("sent"):
            print(
                f"error: send_beleg failed at step={result.get('step')}: "
                f"{result.get('error')}",
                file=sys.stderr,
            )
            _emit(result)
            return 3
        bs = result.get("belege_sent") or {}
        status = result.get("status")
        if result.get("idempotent"):
            print(f"ok: send_beleg idempotent — belege_sent id={bs.get('id')} "
                  f"already exists", file=sys.stderr)
        elif status == "linked_existing":
            print(
                f"ok: send_beleg linked existing belege_sent id="
                f"{result.get('belege_sent_id')} (matched_on="
                f"{result.get('matched_on')}); no second mail sent",
                file=sys.stderr,
            )
        elif result.get("warning"):
            print(f"ok-partial: belege_sent NOT written; warning={result.get('warning')}",
                  file=sys.stderr)
            _emit(result)
            return 4
        else:
            print(f"ok: send_beleg completed — belege_sent id={bs.get('id')}",
                  file=sys.stderr)
        _emit(result)
        return 0

    if v == "send_match":
        adapter = PostgresAdapter(_connect())
        sender = LiveMailSender()
        result = verbs.send_match(
            adapter,
            match_id=args.match_id,
            sender=sender,
            attachment_name=args.attachment_name,
            datev_recipient=_recipient(args),
            source_mailbox=_mailbox(args),
            decided_by=args.decided_by,
            reasoning=args.reasoning,
        )
        if not result.get("sent"):
            print(
                f"error: send_match failed: {result.get('status') or result.get('error')}",
                file=sys.stderr,
            )
            _emit(result)
            return 3
        match = result.get("match") or {}
        status = result.get("status") or ("idempotent" if result.get("idempotent") else "sent")
        print(
            f"ok: send_match {status}; match_id={match.get('id')} "
            f"belege_sent_id={result.get('belege_sent_id') or match.get('legacy_belege_sent_id')}",
            file=sys.stderr,
        )
        _emit(result)
        return 0

    if v == "mark_ignored":
        adapter = PostgresAdapter(_connect())
        result = verbs.mark_ignored(
            adapter, tx_id=args.tx_id,
            reason=args.reason, decided_by=args.decided_by,
        )
        print(
            f"ok: tx {args.tx_id} → ignored=true; "
            f"audit_anomaly_id={result['audit_anomaly']['id']}",
            file=sys.stderr,
        )
        _emit(result)
        return 0

    if v == "mark_manual_needed":
        adapter = PostgresAdapter(_connect())
        result = verbs.mark_manual_needed(
            adapter,
            tx_id=args.tx_id,
            reason=args.reason,
            decided_by=args.decided_by,
        )
        print(
            f"ok: tx {args.tx_id} → manual_needed; match_id={result['match']['id']}",
            file=sys.stderr,
        )
        _emit(result)
        return 0

    if v == "flag_anomaly":
        adapter = PostgresAdapter(_connect())
        result = verbs.flag_anomaly(
            adapter,
            tx_id=args.tx_id,
            reason=args.reason,
            severity=args.severity,
            decided_by=args.decided_by,
        )
        print(
            f"ok: anomaly id={result['anomaly']['id']} severity={result['anomaly']['severity']}",
            file=sys.stderr,
        )
        _emit(result)
        return 0

    if v == "read_anomalies":
        adapter = PostgresAdapter(_connect())
        result = {
            "anomalies": adapter.list_anomalies(
                tx_id=args.tx_id,
                status=args.status,
                limit=args.limit,
            )
        }
        _emit(result)
        return 0

    if v == "get_run_history":
        adapter = PostgresAdapter(_connect())
        runs = [
            _normalize_run_history_row(r)
            for r in adapter.list_reconcile_runs(month=args.month, limit=args.limit)
        ]
        _emit({"month": args.month, "runs": runs})
        return 0

    if v == "finalize_run":
        adapter = PostgresAdapter(_connect())
        notes = None
        if args.notes_json:
            try:
                notes = json.loads(args.notes_json)
            except json.JSONDecodeError as exc:
                print(f"error: --notes-json/--tool-call-summary is not valid JSON: {exc}",
                      file=sys.stderr)
                return 2
        if args.proposed_changes:
            notes = dict(notes or {})
            notes["proposed_changes"] = args.proposed_changes
        result = verbs.finalize_run(
            adapter,
            summary=args.summary, notes=notes, invoked_by=args.invoked_by,
        )
        run = result["reconcile_run"]
        print(f"ok: reconcile_run id={run['id']} written; "
              f"notification_dispatched={result['notification_dispatched']}",
              file=sys.stderr)
        _emit(result)
        return 0

    print(f"error: unknown verb {v!r}", file=sys.stderr)
    return 2


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

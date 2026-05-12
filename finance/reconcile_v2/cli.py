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
    finance-reconcile-v2 flag_anomaly --reason "..." --severity warn --tx-id 123
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


# ──────────────────────────────────────────────────────────────────────
# Argument parsing
# ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="finance-reconcile-v2",
        description="Simplified reconcile toolbox — 7 verbs against the "
                    "3-state TX model (#31).",
    )
    sp = p.add_subparsers(dest="verb", required=True)

    list_p = sp.add_parser("list_open_txs",
                           help="TX not ignored and not in belege_sent")
    list_p.add_argument("--month")
    list_p.add_argument("--limit", type=int)
    list_p.add_argument("--offset", type=int, default=0)
    list_p.add_argument("--ignore-rules", dest="ignore_rules")

    ctx_p = sp.add_parser("get_tx_context",
                          help="TX details + likely-relevant mails (auto-search)")
    ctx_p.add_argument("tx_id", type=int)
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

    mi_p = sp.add_parser("mark_ignored",
                         help="set transactions.ignored=true (one-way)")
    mi_p.add_argument("tx_id", type=int)
    mi_p.add_argument("--reason", required=True)
    mi_p.add_argument("--decided-by", dest="decided_by", default="llm")

    fa_p = sp.add_parser("flag_anomaly",
                         help="insert one bank.agent_anomalies row")
    fa_p.add_argument("--reason", required=True)
    fa_p.add_argument("--severity", default="warn",
                      choices=("info", "warn", "block"))
    fa_p.add_argument("--tx-id", dest="tx_id", type=int)
    fa_p.add_argument("--raised-by", dest="raised_by", default="llm")
    fa_p.add_argument("--run-id", dest="run_id", type=int)

    fr_p = sp.add_parser("finalize_run",
                         help="write reconcile_run row + notify")
    fr_p.add_argument("--summary", required=True)
    fr_p.add_argument("--notes-json", dest="notes_json",
                      help="JSON dict carried as tool_call_summary")
    fr_p.add_argument("--invoked-by", dest="invoked_by", default="user")

    return p


# ──────────────────────────────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────────────────────────────


def _run(args: argparse.Namespace) -> int:
    v = args.verb

    if v == "list_open_txs":
        adapter = PostgresAdapter(_connect())
        result = verbs.list_open_txs(
            adapter,
            month=args.month, limit=args.limit, offset=args.offset,
            ignore_rules_path=_ignore_rules_path(args),
        )
        _emit(result)
        return 0

    if v == "get_tx_context":
        adapter = PostgresAdapter(_connect())
        inbox = None if args.no_inbox else LiveInboxClient()
        ctx = verbs.get_tx_context(
            adapter, args.tx_id,
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
        if result.get("idempotent"):
            print(f"ok: send_beleg idempotent — belege_sent id={bs.get('id')} "
                  f"already exists", file=sys.stderr)
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

    if v == "flag_anomaly":
        adapter = PostgresAdapter(_connect())
        row = verbs.flag_anomaly(
            adapter,
            reason=args.reason, severity=args.severity, tx_id=args.tx_id,
            raised_by=args.raised_by, run_id=args.run_id,
        )
        print(f"ok: anomaly id={row['id']} severity={row['severity']} "
              f"status=open", file=sys.stderr)
        _emit(row)
        return 0

    if v == "finalize_run":
        adapter = PostgresAdapter(_connect())
        notes = None
        if args.notes_json:
            try:
                notes = json.loads(args.notes_json)
            except json.JSONDecodeError as exc:
                print(f"error: --notes-json is not valid JSON: {exc}",
                      file=sys.stderr)
                return 2
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

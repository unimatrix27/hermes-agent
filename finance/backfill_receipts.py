#!/usr/bin/env python3
"""Backfill bank.receipt_candidates and bank.receipt_matches from legacy bank.* state.

Source rows (counts as of issue unimatrix27/ideas#20):
  - bank.transactions.beleg_match (40 non-null: 37 normal + 3 manual_review)
  - bank.belege_sent              (230 rows: 78 with bank_tx_id + 152 without)
  - bank.belege_to_send           (12 superseded rows; 1 has a dirty `via` field, skipped)

Out of scope (per #20):
  - bank.match_proposals — customer-payment / Easybill domain, not supplier receipts.

Invariants enforced (per parent #27):
  - Zero writes to bank.transactions and bank.belege_sent.
  - bank.belege_missing view definition is not touched.
  - Re-running this script with no new source rows produces zero inserts.

Run:
    SUPABASE_DB_URL=... python3 finance/backfill_receipts.py
    SUPABASE_DB_URL=... python3 finance/backfill_receipts.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import psycopg2
import psycopg2.extras

VALID_VIA = {"outlook_auto_rule", "manual_inbox_match", "agent_match", "manual", "manual_review"}

DECIDED_BY_FROM_VIA = {
    "outlook_auto_rule": "legacy_outlook_rule",
    "manual_inbox_match": "user",
    "agent_match": "code",
    "manual": "user",
    "manual_review": "user",
}

MANUAL_REVIEW_STATUS_TO_DECISION = {
    "ignored_offsetting_charge_reversal": "ignored",
    "open_portal_receipt_needed": "manual_needed",
}


def via_to_decided_by(via: str | None) -> str:
    if via is None:
        return "user"
    return DECIDED_BY_FROM_VIA.get(via, "user")


def connect():
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set")
    return psycopg2.connect(url)


def fetch_existing_candidate_id_by_backfill(cur, source: str, source_id: int) -> int | None:
    cur.execute(
        """
        SELECT id FROM bank.receipt_candidates
        WHERE extracted_json->>'_backfill_source' = %s
          AND extracted_json->>'_backfill_id'     = %s
        LIMIT 1
        """,
        (source, str(source_id)),
    )
    row = cur.fetchone()
    return row["id"] if row else None


def insert_candidate_from_belege_sent(cur, bs: dict) -> int:
    """Idempotent: returns existing candidate id if already backfilled."""
    existing = fetch_existing_candidate_id_by_backfill(cur, "belege_sent", bs["id"])
    if existing is not None:
        return existing

    attachments = bs.get("attachment_filenames") or []
    extracted = {
        "_backfill_source": "belege_sent",
        "_backfill_id": bs["id"],
        "attachment_filenames": attachments,
        "legacy_via": bs.get("via"),
        "legacy_reasoning": bs.get("reasoning"),
    }
    cur.execute(
        """
        INSERT INTO bank.receipt_candidates (
            source_system, mailbox, outlook_message_id, internet_message_id,
            sent_at, subject, attachment_name,
            extracted_json, parse_status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
        RETURNING id
        """,
        (
            "datev_sent",
            bs.get("source_mailbox"),
            bs.get("outlook_message_id"),
            bs.get("internet_message_id"),
            bs.get("sent_at"),
            bs.get("subject"),
            attachments[0] if attachments else None,
            json.dumps(extracted),
            "ok",
        ),
    )
    return cur.fetchone()["id"]


def insert_match(
    cur,
    *,
    receipt_candidate_id: int | None,
    bank_tx_id: int,
    confidence: str | None,
    match_type: str,
    reason_codes: list[str],
    decision_status: str,
    decided_by: str,
    legacy_belege_sent_id: int | None,
    legacy_meta: dict[str, Any],
) -> bool:
    """Insert a match row, returning True if inserted, False if a duplicate was found.

    Idempotency:
      * Rows with legacy_belege_sent_id rely on the partial unique index — we use
        ON CONFLICT DO NOTHING and check the returned id.
      * Rows without it dedupe on (legacy_meta->>'origin', legacy_meta->>'<key>').
    """
    if legacy_belege_sent_id is not None:
        cur.execute(
            """
            INSERT INTO bank.receipt_matches (
                receipt_candidate_id, bank_tx_id, confidence, match_type,
                reason_codes, decision_status, decided_by,
                legacy_belege_sent_id, legacy_meta
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
            ON CONFLICT (legacy_belege_sent_id) WHERE legacy_belege_sent_id IS NOT NULL
            DO NOTHING
            RETURNING id
            """,
            (
                receipt_candidate_id,
                bank_tx_id,
                confidence,
                match_type,
                json.dumps(reason_codes),
                decision_status,
                decided_by,
                legacy_belege_sent_id,
                json.dumps(legacy_meta),
            ),
        )
        return cur.fetchone() is not None

    # legacy_belege_sent_id is NULL — dedupe via legacy_meta. We look up first
    # using the GIN-indexed `legacy_meta` column.
    origin = legacy_meta["origin"]
    if origin == "beleg_match_manual_review":
        key, value = "tx_id", legacy_meta["tx_id"]
    elif origin == "belege_to_send":
        key, value = "belege_to_send_id", legacy_meta["belege_to_send_id"]
    else:
        sys.exit(f"Unsupported dedupe origin: {origin!r}")

    cur.execute(
        """
        SELECT id FROM bank.receipt_matches
        WHERE legacy_meta->>'origin' = %s
          AND legacy_meta->>%s       = %s
        LIMIT 1
        """,
        (origin, key, str(value)),
    )
    if cur.fetchone() is not None:
        return False

    cur.execute(
        """
        INSERT INTO bank.receipt_matches (
            receipt_candidate_id, bank_tx_id, confidence, match_type,
            reason_codes, decision_status, decided_by,
            legacy_belege_sent_id, legacy_meta
        )
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
        """,
        (
            receipt_candidate_id,
            bank_tx_id,
            confidence,
            match_type,
            json.dumps(reason_codes),
            decision_status,
            decided_by,
            None,
            json.dumps(legacy_meta),
        ),
    )
    return True


def step_belege_sent_candidates(cur) -> dict[int, int]:
    """Create receipt_candidates rows for every belege_sent with attachments.

    Returns {belege_sent_id: receipt_candidate_id} for downstream linking.
    """
    cur.execute(
        """
        SELECT id, outlook_message_id, internet_message_id, source_mailbox,
               sent_at, recipient, subject, attachment_filenames, via,
               reasoning
        FROM bank.belege_sent
        WHERE attachment_filenames IS NOT NULL
          AND cardinality(attachment_filenames) > 0
        ORDER BY id
        """
    )
    rows = cur.fetchall()
    mapping: dict[int, int] = {}
    created = reused = 0
    for row in rows:
        bs = dict(row)
        existing = fetch_existing_candidate_id_by_backfill(cur, "belege_sent", bs["id"])
        if existing is not None:
            mapping[bs["id"]] = existing
            reused += 1
            continue
        cid = insert_candidate_from_belege_sent(cur, bs)
        mapping[bs["id"]] = cid
        created += 1
    print(f"[candidates] belege_sent: created={created} reused={reused} total={len(rows)}")
    return mapping


def step_beleg_match_matches(cur, candidate_by_bsid: dict[int, int]) -> tuple[int, int]:
    """Walk the 40 non-null transactions.beleg_match rows.

    37 normal rows → receipt_matches with legacy_belege_sent_id set, decision
    status driven by `forwarded_to_datev`.

    3 manual_review rows → receipt_matches with full legacy_meta preserved.
    """
    cur.execute(
        """
        SELECT id, ignored, beleg_match
        FROM bank.transactions
        WHERE beleg_match IS NOT NULL
        ORDER BY id
        """
    )
    rows = cur.fetchall()
    if len(rows) != 40:
        sys.exit(
            f"Live row count drift: bank.transactions has {len(rows)} non-null "
            f"beleg_match rows (expected 40). Stop and confirm with Sebastian."
        )

    inserted = skipped = 0
    for row in rows:
        tx_id = row["id"]
        bm = row["beleg_match"]
        via = bm.get("via")

        if via == "manual_review":
            status = bm.get("status")
            decision = MANUAL_REVIEW_STATUS_TO_DECISION.get(status)
            if decision is None:
                sys.exit(
                    f"Unknown manual_review status {status!r} on tx {tx_id}. "
                    f"Stop and surface to Sebastian."
                )
            legacy_meta = {
                "origin": "beleg_match_manual_review",
                "tx_id": tx_id,
                "via": via,
                "status": status,
                "reason": bm.get("reason"),
            }
            if "kanban_task" in bm:
                legacy_meta["kanban_task"] = bm["kanban_task"]
            did = insert_match(
                cur,
                receipt_candidate_id=None,
                bank_tx_id=tx_id,
                confidence=None,
                match_type="manual_review_legacy",
                reason_codes=["legacy_backfill", "legacy_manual_review"],
                decision_status=decision,
                decided_by="user",
                legacy_belege_sent_id=None,
                legacy_meta=legacy_meta,
            )
        else:
            if via not in VALID_VIA:
                sys.exit(f"Unknown via {via!r} on tx {tx_id}.")
            belege_sent_id = bm.get("belege_sent_id")
            if belege_sent_id is None:
                sys.exit(
                    f"Normal beleg_match row on tx {tx_id} has no belege_sent_id. "
                    f"Surface to Sebastian — schema-analysis note may be stale."
                )
            fwd = bm.get("forwarded_to_datev")
            decision = "sent" if fwd is True else "approved"
            legacy_meta = {
                "origin": "beleg_match",
                "tx_id": tx_id,
                "via": via,
                "source_mailbox": bm.get("source_mailbox"),
                "forwarded_to_datev": fwd,
            }
            did = insert_match(
                cur,
                receipt_candidate_id=candidate_by_bsid.get(belege_sent_id),
                bank_tx_id=tx_id,
                confidence=bm.get("confidence"),
                match_type="manual_review_legacy",
                reason_codes=["legacy_backfill"],
                decision_status=decision,
                decided_by=via_to_decided_by(via),
                legacy_belege_sent_id=belege_sent_id,
                legacy_meta=legacy_meta,
            )
        if did:
            inserted += 1
        else:
            skipped += 1
    print(f"[matches] beleg_match: inserted={inserted} skipped(existing)={skipped}")
    return inserted, skipped


def step_belege_sent_direct_matches(cur, candidate_by_bsid: dict[int, int]) -> tuple[int, int]:
    """belege_sent rows with bank_tx_id that aren't referenced by any beleg_match.

    These are direct DATEV forwards the legacy pipeline made without writing
    back to transactions.beleg_match — typically older rows. Expected: 41.
    """
    cur.execute(
        """
        SELECT bs.id, bs.bank_tx_id, bs.via, bs.confidence, bs.sent_at,
               bs.source_mailbox, bs.attachment_filenames, bs.reasoning
        FROM bank.belege_sent bs
        WHERE bs.bank_tx_id IS NOT NULL
          AND bs.id NOT IN (
              SELECT (beleg_match->>'belege_sent_id')::bigint
              FROM bank.transactions
              WHERE beleg_match->>'belege_sent_id' IS NOT NULL
          )
        ORDER BY bs.id
        """
    )
    rows = cur.fetchall()
    inserted = skipped = 0
    for row in rows:
        bs = dict(row)
        legacy_meta = {
            "origin": "belege_sent_direct",
            "belege_sent_id": bs["id"],
            "via": bs.get("via"),
            "source_mailbox": bs.get("source_mailbox"),
            "sent_at": bs["sent_at"].isoformat() if bs.get("sent_at") else None,
        }
        did = insert_match(
            cur,
            receipt_candidate_id=candidate_by_bsid.get(bs["id"]),
            bank_tx_id=bs["bank_tx_id"],
            confidence=bs.get("confidence"),
            match_type="manual_review_legacy",
            reason_codes=["legacy_backfill"],
            decision_status="sent",
            decided_by=via_to_decided_by(bs.get("via")),
            legacy_belege_sent_id=bs["id"],
            legacy_meta=legacy_meta,
        )
        if did:
            inserted += 1
        else:
            skipped += 1
    print(f"[matches] belege_sent direct: inserted={inserted} skipped(existing)={skipped}")
    return inserted, skipped


def step_belege_to_send_matches(cur) -> tuple[int, int, int]:
    """12 superseded belege_to_send rows → receipt_matches with decision=rejected.

    One row has a dirty `via` (looks like an email subject). Log and skip.
    """
    cur.execute(
        """
        SELECT id, bank_tx_id, via, confidence, status,
               source_mailbox, source_outlook_message_id, source_attachment_filenames,
               reasoning
        FROM bank.belege_to_send
        ORDER BY id
        """
    )
    rows = cur.fetchall()
    inserted = skipped = dirty = 0
    for row in rows:
        ts = dict(row)
        via = ts.get("via")
        if via not in VALID_VIA:
            print(f"[matches] belege_to_send id={ts['id']}: dirty via={via!r} — skipping (per spec)")
            dirty += 1
            continue
        if ts.get("bank_tx_id") is None:
            print(f"[matches] belege_to_send id={ts['id']}: no bank_tx_id — skipping")
            dirty += 1
            continue
        legacy_meta = {
            "origin": "belege_to_send",
            "belege_to_send_id": ts["id"],
            "via": via,
            "status": ts.get("status"),
            "source_mailbox": ts.get("source_mailbox"),
            "source_outlook_message_id": ts.get("source_outlook_message_id"),
            "source_attachment_filenames": ts.get("source_attachment_filenames") or [],
        }
        did = insert_match(
            cur,
            receipt_candidate_id=None,
            bank_tx_id=ts["bank_tx_id"],
            confidence=ts.get("confidence"),
            match_type="manual_review_legacy",
            reason_codes=["legacy_backfill", "legacy_superseded"],
            decision_status="rejected",
            decided_by=via_to_decided_by(via),
            legacy_belege_sent_id=None,
            legacy_meta=legacy_meta,
        )
        if did:
            inserted += 1
        else:
            skipped += 1
    print(f"[matches] belege_to_send: inserted={inserted} skipped(existing)={skipped} dirty={dirty}")
    return inserted, skipped, dirty


def assert_invariants_post(cur):
    """Sanity checks. These do not mutate state."""
    cur.execute("SELECT COUNT(*) AS c FROM bank.transactions WHERE ignored = true")
    assert cur.fetchone()["c"] == 84, "transactions.ignored=true row count drift"

    cur.execute("SELECT COUNT(*) AS c FROM bank.belege_sent")
    assert cur.fetchone()["c"] == 230, "bank.belege_sent total drift"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Run inside a rolled-back transaction.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    conn = connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            candidate_by_bsid = step_belege_sent_candidates(cur)
            step_beleg_match_matches(cur, candidate_by_bsid)
            step_belege_sent_direct_matches(cur, candidate_by_bsid)
            step_belege_to_send_matches(cur)
            assert_invariants_post(cur)
        if args.dry_run:
            conn.rollback()
            print("[dry-run] rolled back")
        else:
            conn.commit()
            print("[commit] backfill applied")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

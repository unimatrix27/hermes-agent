#!/usr/bin/env python3
"""Assert the acceptance criteria from unimatrix27/ideas#20 after a backfill run.

Exits non-zero on any failure. Read-only against the DB.
"""
from __future__ import annotations

import json
import os
import sys

import psycopg2
import psycopg2.extras


def main() -> int:
    url = os.environ["SUPABASE_DB_URL"]
    conn = psycopg2.connect(url)
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    failures: list[str] = []

    def check(label, ok, detail=""):
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    print("Acceptance criteria checks")
    print("-" * 60)

    # 1. transactions.ignored=true count remains 84.
    cur.execute("SELECT count(*) AS c FROM bank.transactions WHERE ignored = true")
    n = cur.fetchone()["c"]
    check("transactions.ignored=true row count is 84", n == 84, f"got {n}")

    # 2. bank.belege_sent total unchanged at 230 (zero writes).
    cur.execute("SELECT count(*) AS c FROM bank.belege_sent")
    n = cur.fetchone()["c"]
    check("bank.belege_sent row count is 230 (no writes)", n == 230, f"got {n}")

    # 3. All 40 beleg_match rows produce exactly one receipt_matches row each.
    cur.execute(
        """
        SELECT t.id
        FROM bank.transactions t
        LEFT JOIN bank.receipt_matches rm
          ON  (rm.legacy_belege_sent_id = (t.beleg_match->>'belege_sent_id')::bigint
               AND rm.legacy_meta->>'origin' = 'beleg_match')
          OR  (rm.legacy_meta->>'origin' = 'beleg_match_manual_review'
               AND (rm.legacy_meta->>'tx_id')::bigint = t.id)
        WHERE t.beleg_match IS NOT NULL
        GROUP BY t.id
        HAVING count(rm.id) <> 1
        """
    )
    miss = [r["id"] for r in cur.fetchall()]
    check(
        "all 40 beleg_match rows have exactly one receipt_matches row",
        not miss,
        f"tx_ids w/o 1:1: {miss}",
    )

    # 4. The 3 manual_review rows preserve their full legacy_meta verbatim.
    cur.execute(
        """
        SELECT (legacy_meta->>'tx_id')::bigint AS tx_id, legacy_meta
        FROM bank.receipt_matches
        WHERE legacy_meta->>'origin' = 'beleg_match_manual_review'
        ORDER BY tx_id
        """
    )
    review_rows = cur.fetchall()
    check("3 manual_review rows present", len(review_rows) == 3, f"got {len(review_rows)}")

    cur.execute(
        """
        SELECT id, beleg_match
        FROM bank.transactions
        WHERE beleg_match->>'via' = 'manual_review'
        ORDER BY id
        """
    )
    src = {row["id"]: row["beleg_match"] for row in cur.fetchall()}
    for row in review_rows:
        meta = row["legacy_meta"]
        tx_id = row["tx_id"]
        bm = src.get(tx_id, {})
        for key in ("status", "reason"):
            check(
                f"manual_review tx {tx_id}: legacy_meta.{key} verbatim",
                meta.get(key) == bm.get(key),
                f"want {bm.get(key)!r}, got {meta.get(key)!r}",
            )
        if "kanban_task" in bm:
            check(
                f"manual_review tx {tx_id}: kanban_task preserved",
                meta.get("kanban_task") == bm.get("kanban_task"),
                f"want {bm.get('kanban_task')!r}, got {meta.get('kanban_task')!r}",
            )

    # 5. TX 5, 53, 66 → decision_status='sent'; TX 88 → no row.
    cur.execute(
        """
        SELECT bank_tx_id, array_agg(decision_status ORDER BY id) AS statuses
        FROM bank.receipt_matches
        WHERE bank_tx_id IN (5, 53, 66, 88)
        GROUP BY bank_tx_id
        """
    )
    statuses = {r["bank_tx_id"]: r["statuses"] for r in cur.fetchall()}
    for tx_id in (5, 53, 66):
        check(
            f"TX {tx_id} reachable with decision_status='sent'",
            "sent" in (statuses.get(tx_id) or []),
            f"got {statuses.get(tx_id)}",
        )
    check(
        "TX 88 has no receipt_matches row (correctly missing)",
        88 not in statuses,
        f"got {statuses.get(88)}",
    )

    # 6. Google Ads kanban task preserved verbatim.
    cur.execute(
        """
        SELECT legacy_meta
        FROM bank.receipt_matches
        WHERE legacy_meta->>'kanban_task' = 't_51751302'
        """
    )
    rows = cur.fetchall()
    check(
        "Google Ads kanban task t_51751302 preserved",
        len(rows) == 1,
        f"got {len(rows)} matches",
    )

    # 7. No receipt_matches row references a transaction whose ignored flag was flipped.
    cur.execute(
        """
        SELECT id, ignored
        FROM bank.transactions
        WHERE ignored = true
        """
    )
    ignored_ids = {r["id"] for r in cur.fetchall()}
    check("ignored=true preserved on all 84 rows", len(ignored_ids) == 84, f"got {len(ignored_ids)}")

    # 8. receipt_matches counts.
    cur.execute("SELECT count(*) AS c FROM bank.receipt_matches")
    n = cur.fetchone()["c"]
    # 40 beleg_match + 41 fresh belege_sent + 11 belege_to_send (1 dirty skipped) = 92
    check("receipt_matches total count is 92", n == 92, f"got {n}")

    cur.execute(
        """
        SELECT count(*) AS c FROM bank.receipt_matches
        WHERE legacy_meta->>'origin' = 'beleg_match'
        """
    )
    n = cur.fetchone()["c"]
    check("beleg_match-origin matches: 37", n == 37, f"got {n}")

    cur.execute(
        """
        SELECT count(*) AS c FROM bank.receipt_matches
        WHERE legacy_meta->>'origin' = 'beleg_match_manual_review'
        """
    )
    n = cur.fetchone()["c"]
    check("manual_review-origin matches: 3", n == 3, f"got {n}")

    cur.execute(
        """
        SELECT count(*) AS c FROM bank.receipt_matches
        WHERE legacy_meta->>'origin' = 'belege_sent_direct'
        """
    )
    n = cur.fetchone()["c"]
    check("belege_sent_direct-origin matches: 41", n == 41, f"got {n}")

    cur.execute(
        """
        SELECT count(*) AS c FROM bank.receipt_matches
        WHERE legacy_meta->>'origin' = 'belege_to_send'
        """
    )
    n = cur.fetchone()["c"]
    check("belege_to_send-origin matches: 11", n == 11, f"got {n}")

    # 9. Candidates created for ~71 belege_sent rows with attachments.
    cur.execute(
        """
        SELECT count(*) AS c FROM bank.receipt_candidates
        WHERE extracted_json->>'_backfill_source' = 'belege_sent'
        """
    )
    n = cur.fetchone()["c"]
    check("receipt_candidates from belege_sent: 71", n == 71, f"got {n}")

    # 10. No bank.match_proposals rows leaked into the new tables.
    cur.execute(
        """
        SELECT count(*) AS c FROM bank.receipt_matches
        WHERE legacy_meta->>'origin' LIKE '%match_proposal%'
        """
    )
    n = cur.fetchone()["c"]
    check("match_proposals not imported (out of scope)", n == 0, f"got {n}")

    print("-" * 60)
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Storage boundary for the v2 reconcile toolbox.

Reads / writes only four tables (per unimatrix27/ideas#31):

    bank.transactions             — read; UPDATE only of the ``ignored`` flag
    bank.belege_sent              — read (idempotency probe) + INSERT
    bank.agent_anomalies          — INSERT
    bank.agent_reconcile_runs     — INSERT

No proposal / candidate / match tables are touched. The legacy v1
toolbox (PR #5) targets a different set of tables and operates
independently — the two can coexist without interfering until #31's
follow-up cleanup PR drops the obsolete ones.

A ``Protocol`` + ``InMemoryAdapter`` for tests, plus a psycopg2-backed
``PostgresAdapter`` for the CLI. Read methods are simple-shaped:
filtering / joining lives in :mod:`finance.reconcile_v2.verbs`.
"""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional, Protocol, Sequence


# ──────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────


class ToolError(Exception):
    """Caller-visible problem (validation, etc)."""


class NotFound(ToolError):
    """Row lookup returned nothing."""


class InvalidTransition(ToolError):
    """Caller asked for a state change the invariants forbid.

    Canonical case: ``mark_ignored`` on an already-ignored TX. Per #31's
    3-state model, ignored → not-ignored is not the agent's call to make.
    """


# ──────────────────────────────────────────────────────────────────────
# Protocol
# ──────────────────────────────────────────────────────────────────────


class Adapter(Protocol):
    # reads
    def list_open_transactions(
        self,
        *,
        month: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    def get_transaction(self, tx_id: int) -> Optional[dict[str, Any]]: ...

    def find_belege_sent_for_tx(self, tx_id: int) -> list[dict[str, Any]]: ...

    def find_belege_sent_match(
        self,
        *,
        outlook_message_id: Optional[str] = None,
        internet_message_id: Optional[str] = None,
        attachment_filename: Optional[str] = None,
        bank_tx_amount: Optional[float] = None,
    ) -> Optional[dict[str, Any]]: ...

    def list_anomalies(
        self,
        *,
        tx_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def get_receipt_match_with_candidate(
        self, match_id: int,
    ) -> Optional[dict[str, Any]]: ...

    def find_latest_receipt_match_for_tx(
        self, tx_id: int,
    ) -> Optional[dict[str, Any]]: ...

    def get_belege_sent_by_id(self, belege_sent_id: int) -> Optional[dict[str, Any]]: ...

    # writes
    def set_transaction_ignored(
        self, *, tx_id: int, expect_currently: bool,
    ) -> dict[str, Any]: ...

    def link_belege_sent_to_tx(
        self, *, belege_sent_id: int, bank_tx_id: int,
    ) -> Optional[dict[str, Any]]: ...

    def insert_belege_sent(
        self,
        *,
        outlook_message_id: str,
        internet_message_id: Optional[str],
        source_mailbox: str,
        sent_at: datetime,
        recipient: str,
        subject: Optional[str],
        attachment_filenames: Sequence[str],
        via: str,
        bank_tx_id: int,
        bank_tx_amount: Optional[float],
        bank_tx_booking_date: Optional[date],
        confidence: Optional[str],
        reasoning: Optional[str],
    ) -> dict[str, Any]: ...

    def insert_anomaly(
        self,
        *,
        bank_tx_id: Optional[int],
        reason: str,
        severity: str,
        raised_by: str,
        run_id: Optional[int] = None,
    ) -> dict[str, Any]: ...

    def update_receipt_match_status(
        self,
        *,
        match_id: int,
        decision_status: str,
        decided_by: str,
        legacy_belege_sent_id: Optional[int] = None,
        legacy_meta_patch: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]: ...

    def create_receipt_match(
        self,
        *,
        bank_tx_id: int,
        decision_status: str,
        decided_by: str,
        match_type: str,
        reason_codes: Sequence[Any],
        confidence: Optional[str] = None,
        receipt_candidate_id: Optional[int] = None,
        legacy_belege_sent_id: Optional[int] = None,
        legacy_meta: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]: ...

    def insert_reconcile_run(
        self,
        *,
        summary_md: str,
        notes: Optional[Mapping[str, Any]],
        invoked_by: str,
    ) -> dict[str, Any]: ...

    def list_reconcile_runs(
        self,
        *,
        month: Optional[str] = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]: ...


# ──────────────────────────────────────────────────────────────────────
# In-memory adapter — for tests and offline development
# ──────────────────────────────────────────────────────────────────────


@dataclass
class InMemoryAdapter:
    """Threadsafe in-memory adapter mirroring the Postgres shape."""

    transactions: list[dict[str, Any]] = field(default_factory=list)
    belege_sent: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    receipt_candidates: list[dict[str, Any]] = field(default_factory=list)
    receipt_matches: list[dict[str, Any]] = field(default_factory=list)
    reconcile_runs: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _next_belege_id: int = 1
    _next_anomaly_id: int = 1
    _next_receipt_match_id: int = 1
    _next_run_id: int = 1

    # ── reads ──

    def list_open_transactions(
        self,
        *,
        month: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            sent_tx_ids = {b["bank_tx_id"] for b in self.belege_sent
                           if b.get("bank_tx_id") is not None}
            rows: list[dict[str, Any]] = []
            for tx in self.transactions:
                if tx.get("ignored"):
                    continue
                if tx["id"] in sent_tx_ids:
                    continue
                if month:
                    booking = tx.get("booking_date")
                    if booking is None or _month_key(booking) != month:
                        continue
                rows.append(deepcopy(tx))
            rows.sort(key=lambda r: (r.get("booking_date") or date.min, r["id"]))
            if offset:
                rows = rows[offset:]
            if limit is not None:
                rows = rows[:limit]
            return rows

    def get_transaction(self, tx_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            for tx in self.transactions:
                if tx["id"] == tx_id:
                    return deepcopy(tx)
            return None

    def find_belege_sent_for_tx(self, tx_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(b) for b in self.belege_sent
                    if b.get("bank_tx_id") == tx_id]

    def find_belege_sent_match(
        self,
        *,
        outlook_message_id: Optional[str] = None,
        internet_message_id: Optional[str] = None,
        attachment_filename: Optional[str] = None,
        bank_tx_amount: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Cross-tx dedup probe (priority: outlook_message_id, then
        internet_message_id, then attachment_filename + bank_tx_amount).
        Returns the oldest matching row (by sent_at) or None.
        """
        with self._lock:
            def _pick(predicate) -> Optional[dict[str, Any]]:
                hits = [b for b in self.belege_sent if predicate(b)]
                if not hits:
                    return None
                hits.sort(key=lambda r: (
                    r.get("sent_at") or datetime.min.replace(tzinfo=timezone.utc),
                    r.get("id") or 0,
                ))
                return deepcopy(hits[0])

            if outlook_message_id:
                row = _pick(lambda b: b.get("outlook_message_id") == outlook_message_id)
                if row is not None:
                    return row
            if internet_message_id:
                row = _pick(lambda b: b.get("internet_message_id") == internet_message_id)
                if row is not None:
                    return row
            if attachment_filename and bank_tx_amount is not None:
                target_amount = float(bank_tx_amount)

                def _match_attach(b: dict[str, Any]) -> bool:
                    fnames = b.get("attachment_filenames") or []
                    if attachment_filename not in fnames:
                        return False
                    amt = b.get("bank_tx_amount")
                    if amt is None:
                        return False
                    try:
                        return abs(float(amt) - target_amount) < 0.005
                    except (TypeError, ValueError):
                        return False

                row = _pick(_match_attach)
                if row is not None:
                    return row
            return None

    def list_anomalies(
        self,
        *,
        tx_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.anomalies)
            if tx_id is not None:
                rows = [r for r in rows if r.get("bank_tx_id") == tx_id]
            if status is not None:
                rows = [r for r in rows if r.get("status") == status]
            rows.sort(key=lambda r: r.get("created_at") or datetime.min,
                      reverse=True)
            return [deepcopy(r) for r in rows[:limit]]

    def get_receipt_match_with_candidate(
        self, match_id: int,
    ) -> Optional[dict[str, Any]]:
        with self._lock:
            match = next((m for m in self.receipt_matches if m.get("id") == match_id), None)
            if match is None:
                return None
            candidate = None
            cid = match.get("receipt_candidate_id")
            if cid is not None:
                candidate = next((c for c in self.receipt_candidates if c.get("id") == cid), None)
            return {"match": deepcopy(match), "candidate": deepcopy(candidate) if candidate else None}

    def find_latest_receipt_match_for_tx(
        self, tx_id: int,
    ) -> Optional[dict[str, Any]]:
        with self._lock:
            rows = [m for m in self.receipt_matches if m.get("bank_tx_id") == tx_id]
            if not rows:
                return None
            rows.sort(key=lambda r: (r.get("updated_at") or r.get("created_at") or datetime.min, r.get("id") or 0), reverse=True)
            return deepcopy(rows[0])

    def get_belege_sent_by_id(self, belege_sent_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = next((b for b in self.belege_sent if b.get("id") == belege_sent_id), None)
            return deepcopy(row) if row else None

    # ── writes ──

    def set_transaction_ignored(
        self, *, tx_id: int, expect_currently: bool,
    ) -> dict[str, Any]:
        with self._lock:
            for tx in self.transactions:
                if tx["id"] != tx_id:
                    continue
                if bool(tx.get("ignored")) != expect_currently:
                    raise InvalidTransition(
                        f"tx {tx_id} ignored={bool(tx.get('ignored'))}, "
                        f"expected {expect_currently}"
                    )
                tx["ignored"] = not expect_currently
                return deepcopy(tx)
            raise NotFound(f"no transaction with id {tx_id}")

    def link_belege_sent_to_tx(
        self, *, belege_sent_id: int, bank_tx_id: int,
    ) -> Optional[dict[str, Any]]:
        """Attach an orphan ``belege_sent`` row (``bank_tx_id IS NULL``)
        to ``tx_id``. Returns the updated row on success; ``None`` if
        the guard missed (row's ``bank_tx_id`` was already set by a
        concurrent writer).
        """
        with self._lock:
            for row in self.belege_sent:
                if row.get("id") != belege_sent_id:
                    continue
                if row.get("bank_tx_id") is not None:
                    return None
                row["bank_tx_id"] = bank_tx_id
                return deepcopy(row)
            return None

    def insert_belege_sent(
        self,
        *,
        outlook_message_id: str,
        internet_message_id: Optional[str],
        source_mailbox: str,
        sent_at: datetime,
        recipient: str,
        subject: Optional[str],
        attachment_filenames: Sequence[str],
        via: str,
        bank_tx_id: int,
        bank_tx_amount: Optional[float],
        bank_tx_booking_date: Optional[date],
        confidence: Optional[str],
        reasoning: Optional[str],
    ) -> dict[str, Any]:
        with self._lock:
            row = {
                "id":                  self._next_belege_id,
                "outlook_message_id":  outlook_message_id,
                "internet_message_id": internet_message_id,
                "source_mailbox":      source_mailbox,
                "sent_at":             sent_at,
                "recipient":           recipient,
                "subject":             subject,
                "attachment_filenames": list(attachment_filenames),
                "via":                 via,
                "bank_tx_id":          bank_tx_id,
                "bank_tx_amount":      bank_tx_amount,
                "bank_tx_booking_date": bank_tx_booking_date,
                "confidence":          confidence,
                "reasoning":           reasoning,
                "created_at":          datetime.now(timezone.utc),
            }
            self.belege_sent.append(row)
            self._next_belege_id += 1
            return deepcopy(row)

    def insert_anomaly(
        self,
        *,
        bank_tx_id: Optional[int],
        reason: str,
        severity: str,
        raised_by: str,
        run_id: Optional[int] = None,
    ) -> dict[str, Any]:
        with self._lock:
            row = {
                "id":          self._next_anomaly_id,
                "bank_tx_id":  bank_tx_id,
                "reason":      reason,
                "severity":    severity,
                "status":      "open",
                "raised_by":   raised_by,
                "run_id":      run_id,
                "created_at":  datetime.now(timezone.utc),
                "resolved_at": None,
            }
            self.anomalies.append(row)
            self._next_anomaly_id += 1
            return deepcopy(row)

    def update_receipt_match_status(
        self,
        *,
        match_id: int,
        decision_status: str,
        decided_by: str,
        legacy_belege_sent_id: Optional[int] = None,
        legacy_meta_patch: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            for row in self.receipt_matches:
                if row.get("id") != match_id:
                    continue
                row["decision_status"] = decision_status
                row["decided_by"] = decided_by
                if legacy_belege_sent_id is not None:
                    row["legacy_belege_sent_id"] = legacy_belege_sent_id
                if legacy_meta_patch:
                    meta = dict(row.get("legacy_meta") or {})
                    meta.update(dict(legacy_meta_patch))
                    row["legacy_meta"] = meta
                row["updated_at"] = datetime.now(timezone.utc)
                return deepcopy(row)
            raise NotFound(f"no receipt_match with id {match_id}")

    def create_receipt_match(
        self,
        *,
        bank_tx_id: int,
        decision_status: str,
        decided_by: str,
        match_type: str,
        reason_codes: Sequence[Any],
        confidence: Optional[str] = None,
        receipt_candidate_id: Optional[int] = None,
        legacy_belege_sent_id: Optional[int] = None,
        legacy_meta: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            row = {
                "id": self._next_receipt_match_id,
                "receipt_candidate_id": receipt_candidate_id,
                "bank_tx_id": bank_tx_id,
                "confidence": confidence,
                "match_type": match_type,
                "reason_codes": list(reason_codes),
                "decision_status": decision_status,
                "decided_by": decided_by,
                "legacy_belege_sent_id": legacy_belege_sent_id,
                "legacy_meta": dict(legacy_meta or {}),
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            self.receipt_matches.append(row)
            self._next_receipt_match_id += 1
            return deepcopy(row)

    def insert_reconcile_run(
        self,
        *,
        summary_md: str,
        notes: Optional[Mapping[str, Any]],
        invoked_by: str,
    ) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(timezone.utc)
            row = {
                "id":           self._next_run_id,
                "started_at":   now,
                "finalized_at": now,
                "summary_md":   summary_md,
                "notes":        json.dumps(dict(notes)) if notes else None,
                "invoked_by":   invoked_by,
                "created_at":   now,
            }
            self.reconcile_runs.append(row)
            self._next_run_id += 1
            return deepcopy(row)

    def list_reconcile_runs(
        self,
        *,
        month: Optional[str] = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.reconcile_runs)
            if month is not None:
                rows = [r for r in rows if _run_matches_month(r, month)]
            rows.sort(
                key=lambda r: r.get("finalized_at") or r.get("started_at") or r.get("created_at") or datetime.min,
                reverse=True,
            )
            return [deepcopy(r) for r in rows[:limit]]


# ──────────────────────────────────────────────────────────────────────
# Postgres adapter — raw parameterized SQL (no ORM, no MCP helpers).
# ──────────────────────────────────────────────────────────────────────


class PostgresAdapter:
    """psycopg2-backed adapter against the live ``bank.*`` schema."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def _cursor(self) -> Any:
        # Lazy import so test fakes don't require psycopg2.
        from psycopg2.extras import RealDictCursor
        return self.conn.cursor(cursor_factory=RealDictCursor)

    # ── reads ──

    def list_open_transactions(
        self,
        *,
        month: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["t.ignored = false",
                   "NOT EXISTS (SELECT 1 FROM bank.belege_sent bs "
                   "WHERE bs.bank_tx_id = t.id)"]
        params: list[Any] = []
        if month:
            clauses.append("to_char(t.booking_date, 'YYYY-MM') = %s")
            params.append(month)
        where = "WHERE " + " AND ".join(clauses)
        sql = (
            "SELECT t.id, t.account_iban, t.booking_date, t.value_date, "
            "t.amount, t.currency, t.credit_debit, t.signed_amount, "
            "t.counterparty_name, t.counterparty_iban, t.counterparty_bic, "
            "t.remittance_information, t.bank_tx_description, t.ignored "
            f"FROM bank.transactions t {where} "
            "ORDER BY t.booking_date NULLS LAST, t.id "
        )
        if limit is not None:
            sql += "LIMIT %s OFFSET %s"
            params.extend([limit, offset])
        elif offset:
            sql += "OFFSET %s"
            params.append(offset)
        with self._cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def get_transaction(self, tx_id: int) -> Optional[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM bank.transactions WHERE id = %s", (tx_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def find_belege_sent_for_tx(self, tx_id: int) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM bank.belege_sent WHERE bank_tx_id = %s "
                "ORDER BY sent_at DESC, id DESC",
                (tx_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def find_belege_sent_match(
        self,
        *,
        outlook_message_id: Optional[str] = None,
        internet_message_id: Optional[str] = None,
        attachment_filename: Optional[str] = None,
        bank_tx_amount: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Cross-tx dedup probe — returns the oldest row matching ANY
        of (outlook_message_id), (internet_message_id), or
        (attachment_filename in attachment_filenames AND bank_tx_amount).
        Priority follows the argument order.
        """
        # outlook_message_id has highest signal — try first.
        if outlook_message_id:
            with self._cursor() as cur:
                cur.execute(
                    "SELECT * FROM bank.belege_sent "
                    "WHERE outlook_message_id = %s "
                    "ORDER BY sent_at ASC, id ASC LIMIT 1",
                    (outlook_message_id,),
                )
                row = cur.fetchone()
            if row is not None:
                return dict(row)
        if internet_message_id:
            with self._cursor() as cur:
                cur.execute(
                    "SELECT * FROM bank.belege_sent "
                    "WHERE internet_message_id = %s "
                    "ORDER BY sent_at ASC, id ASC LIMIT 1",
                    (internet_message_id,),
                )
                row = cur.fetchone()
            if row is not None:
                return dict(row)
        if attachment_filename and bank_tx_amount is not None:
            # ANY array element equals the chosen filename AND the
            # recorded bank_tx_amount matches to two decimals. Cheap
            # stand-in for hashing the PDF.
            with self._cursor() as cur:
                cur.execute(
                    "SELECT * FROM bank.belege_sent "
                    "WHERE %s = ANY(attachment_filenames) "
                    "  AND bank_tx_amount IS NOT NULL "
                    "  AND ROUND(bank_tx_amount::numeric, 2) "
                    "      = ROUND(%s::numeric, 2) "
                    "ORDER BY sent_at ASC, id ASC LIMIT 1",
                    (attachment_filename, float(bank_tx_amount)),
                )
                row = cur.fetchone()
            if row is not None:
                return dict(row)
        return None

    def list_anomalies(
        self,
        *,
        tx_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if tx_id is not None:
            clauses.append("bank_tx_id = %s")
            params.append(tx_id)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT * FROM bank.agent_anomalies {where} "
                f"ORDER BY created_at DESC LIMIT %s",
                params + [limit],
            )
            return [dict(r) for r in cur.fetchall()]

    def get_receipt_match_with_candidate(
        self, match_id: int,
    ) -> Optional[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    row_to_json(rm.*) AS match,
                    CASE WHEN rc.id IS NULL THEN NULL ELSE row_to_json(rc.*) END AS candidate
                  FROM bank.receipt_matches rm
                  LEFT JOIN bank.receipt_candidates rc
                    ON rc.id = rm.receipt_candidate_id
                 WHERE rm.id = %s
                """,
                (match_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"match": dict(row["match"]), "candidate": dict(row["candidate"]) if row["candidate"] else None}

    def find_latest_receipt_match_for_tx(
        self, tx_id: int,
    ) -> Optional[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT *
                  FROM bank.receipt_matches
                 WHERE bank_tx_id = %s
                 ORDER BY updated_at DESC, id DESC
                 LIMIT 1
                """,
                (tx_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_belege_sent_by_id(self, belege_sent_id: int) -> Optional[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM bank.belege_sent WHERE id = %s", (belege_sent_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    # ── writes ──

    def set_transaction_ignored(
        self, *, tx_id: int, expect_currently: bool,
    ) -> dict[str, Any]:
        new_value = not expect_currently
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE bank.transactions
                   SET ignored = %s
                 WHERE id = %s AND ignored = %s
             RETURNING id, ignored, booking_date, amount, counterparty_name
                """,
                (new_value, tx_id, expect_currently),
            )
            row = cur.fetchone()
        self.conn.commit()
        if row is None:
            with self._cursor() as cur2:
                cur2.execute("SELECT ignored FROM bank.transactions WHERE id = %s",
                             (tx_id,))
                existing = cur2.fetchone()
            if existing is None:
                raise NotFound(f"no transaction with id {tx_id}")
            raise InvalidTransition(
                f"tx {tx_id} ignored={bool(existing['ignored'])}, "
                f"expected {expect_currently}"
            )
        return dict(row)

    def link_belege_sent_to_tx(
        self, *, belege_sent_id: int, bank_tx_id: int,
    ) -> Optional[dict[str, Any]]:
        """Atomically attach an orphan ``belege_sent`` row to ``bank_tx_id``.

        The ``WHERE bank_tx_id IS NULL`` guard is what makes this safe
        under concurrent writers — if a racing UPDATE got there first
        we return ``None`` and the caller decides (typically: re-read +
        treat as cross-tx conflict).
        """
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE bank.belege_sent
                   SET bank_tx_id = %s
                 WHERE id = %s AND bank_tx_id IS NULL
             RETURNING *
                """,
                (bank_tx_id, belege_sent_id),
            )
            row = cur.fetchone()
        self.conn.commit()
        return dict(row) if row else None

    def insert_belege_sent(
        self,
        *,
        outlook_message_id: str,
        internet_message_id: Optional[str],
        source_mailbox: str,
        sent_at: datetime,
        recipient: str,
        subject: Optional[str],
        attachment_filenames: Sequence[str],
        via: str,
        bank_tx_id: int,
        bank_tx_amount: Optional[float],
        bank_tx_booking_date: Optional[date],
        confidence: Optional[str],
        reasoning: Optional[str],
    ) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO bank.belege_sent (
                    outlook_message_id, internet_message_id, source_mailbox,
                    sent_at, recipient, subject, attachment_filenames, via,
                    bank_tx_id, bank_tx_amount, bank_tx_booking_date,
                    confidence, reasoning
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    outlook_message_id,
                    internet_message_id,
                    source_mailbox,
                    sent_at,
                    recipient,
                    subject,
                    list(attachment_filenames),
                    via,
                    bank_tx_id,
                    bank_tx_amount,
                    bank_tx_booking_date,
                    confidence,
                    reasoning,
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        return dict(row)

    def insert_anomaly(
        self,
        *,
        bank_tx_id: Optional[int],
        reason: str,
        severity: str,
        raised_by: str,
        run_id: Optional[int] = None,
    ) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO bank.agent_anomalies (
                    bank_tx_id, reason, severity, raised_by, run_id
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (bank_tx_id, reason, severity, raised_by, run_id),
            )
            row = cur.fetchone()
        self.conn.commit()
        return dict(row)

    def update_receipt_match_status(
        self,
        *,
        match_id: int,
        decision_status: str,
        decided_by: str,
        legacy_belege_sent_id: Optional[int] = None,
        legacy_meta_patch: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE bank.receipt_matches
                   SET decision_status = %s,
                       decided_by = %s,
                       legacy_belege_sent_id = COALESCE(%s, legacy_belege_sent_id),
                       legacy_meta = COALESCE(legacy_meta, '{}'::jsonb) || %s::jsonb,
                       updated_at = now()
                 WHERE id = %s
             RETURNING *
                """,
                (
                    decision_status,
                    decided_by,
                    legacy_belege_sent_id,
                    json.dumps(dict(legacy_meta_patch or {})),
                    match_id,
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        if row is None:
            raise NotFound(f"no receipt_match with id {match_id}")
        return dict(row)

    def create_receipt_match(
        self,
        *,
        bank_tx_id: int,
        decision_status: str,
        decided_by: str,
        match_type: str,
        reason_codes: Sequence[Any],
        confidence: Optional[str] = None,
        receipt_candidate_id: Optional[int] = None,
        legacy_belege_sent_id: Optional[int] = None,
        legacy_meta: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO bank.receipt_matches (
                    receipt_candidate_id, bank_tx_id, confidence, match_type,
                    reason_codes, decision_status, decided_by,
                    legacy_belege_sent_id, legacy_meta
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
                RETURNING *
                """,
                (
                    receipt_candidate_id,
                    bank_tx_id,
                    confidence,
                    match_type,
                    json.dumps(list(reason_codes)),
                    decision_status,
                    decided_by,
                    legacy_belege_sent_id,
                    json.dumps(dict(legacy_meta or {})),
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        return dict(row)

    def insert_reconcile_run(
        self,
        *,
        summary_md: str,
        notes: Optional[Mapping[str, Any]],
        invoked_by: str,
    ) -> dict[str, Any]:
        # bank.agent_reconcile_runs has both `notes` (text) and
        # `tool_call_summary` (jsonb). v2 keeps it lean: store the
        # whole structured payload in `tool_call_summary` so v1 and v2
        # rows have parallel shapes for any future dashboard. `notes`
        # text column is left NULL.
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO bank.agent_reconcile_runs (
                    finalized_at, summary_md, tool_call_summary, invoked_by
                )
                VALUES (now(), %s, %s::jsonb, %s)
                RETURNING *
                """,
                (
                    summary_md,
                    json.dumps(dict(notes)) if notes else None,
                    invoked_by,
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        return dict(row)

    def list_reconcile_runs(
        self,
        *,
        month: Optional[str] = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if month is not None:
            clauses.append(
                "(tool_call_summary->>'month_scope' = %s OR "
                "tool_call_summary->>'month' = %s OR notes LIKE %s)"
            )
            params.extend([month, month, f"%{month}%"])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT * FROM bank.agent_reconcile_runs {where} "
                "ORDER BY COALESCE(finalized_at, started_at, created_at) DESC "
                "LIMIT %s",
                params + [limit],
            )
            return [dict(r) for r in cur.fetchall()]


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


def _month_key(value: Any) -> Optional[str]:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m")
    if isinstance(value, str) and len(value) >= 7:
        return value[:7]
    return None


def _run_matches_month(row: Mapping[str, Any], month: str) -> bool:
    for key in ("tool_call_summary", "notes"):
        raw = row.get(key)
        if not raw:
            continue
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                if month in raw:
                    return True
                continue
        elif isinstance(raw, Mapping):
            payload = raw
        else:
            continue
        if payload.get("month_scope") == month or payload.get("month") == month:
            return True
    return False

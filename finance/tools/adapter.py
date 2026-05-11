"""Persistence boundary for the finance toolbox.

Mirrors the ``MatcherAdapter`` / ``IndexerAdapter`` pattern from PRs #3
and #4 — a Protocol, plus an in-memory implementation for tests and a
psycopg2-backed one for the CLI. The Postgres adapter does **only**
raw parameterized SQL for schema-qualified writes (the Postgres MCP
helpers have had bugs against ``bank.*`` writes in the past).

Read methods are nullary-or-simple: filters happen at the verb layer
in ``verbs.py`` so the in-memory and Postgres paths stay symmetric.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence


# ──────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────


class ToolError(Exception):
    """Raised by adapters / verbs for caller-visible problems.

    Verbs catch these at the boundary and translate to the JSON / status
    line shape; tests assert on them directly.
    """


class NotFound(ToolError):
    """Lookup turned up empty (no row with that id)."""


class InvalidTransition(ToolError):
    """Caller asked for a state transition the invariants forbid.

    Per #27: ``mark_ignored`` cannot flip ``ignored=true → false`` —
    that's the canonical example. ``send_match`` raises this when the
    target match isn't ``approved``.
    """


# ──────────────────────────────────────────────────────────────────────
# Protocol
# ──────────────────────────────────────────────────────────────────────


class ToolAdapter(Protocol):
    """Storage boundary for every verb in this toolbox."""

    # ── reads ──
    def list_status_view(
        self,
        *,
        month: Optional[str] = None,
        vendor: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    def get_transaction(self, tx_id: int) -> Optional[dict[str, Any]]: ...

    def get_matches_for_tx(self, tx_id: int) -> list[dict[str, Any]]: ...

    def get_candidate(self, candidate_id: int) -> Optional[dict[str, Any]]: ...

    def get_match(self, match_id: int) -> Optional[dict[str, Any]]: ...

    def get_agent_runs_for_tx(self, tx_id: int) -> list[dict[str, Any]]: ...

    def list_proposed_matches(
        self,
        *,
        month: Optional[str] = None,
        vendor: Optional[str] = None,
        min_confidence: Optional[str] = None,
    ) -> list[dict[str, Any]]: ...

    def list_reconcile_runs(
        self,
        *,
        month: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]: ...

    def list_anomalies(
        self,
        *,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    # ── writes (one row, no compound transactions) ──
    def update_match_decision(
        self,
        *,
        match_id: int,
        decision_status: str,
        decided_by: str,
        agent_note: str,
        expect_current_status: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]: ...

    def insert_match(
        self,
        *,
        bank_tx_id: int,
        receipt_candidate_id: Optional[int],
        match_type: str,
        confidence: Optional[str],
        decision_status: str,
        decided_by: str,
        reason_codes: Sequence[str],
        legacy_meta: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def find_manual_needed_match(self, tx_id: int) -> Optional[dict[str, Any]]: ...

    def set_transaction_ignored(
        self,
        *,
        tx_id: int,
        expect_currently: bool,
    ) -> dict[str, Any]: ...

    # ── side effects ──
    def find_belege_sent_by_natural_key(
        self,
        *,
        attachment_sha256: Optional[str],
        bank_tx_id: int,
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
        legacy_meta: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]: ...

    def insert_reconcile_run(
        self,
        *,
        summary_md: str,
        proposed_changes: Optional[Mapping[str, Any]],
        tool_call_summary: Optional[Mapping[str, Any]],
        invoked_by: str,
        notes: Optional[str],
    ) -> dict[str, Any]: ...


# ──────────────────────────────────────────────────────────────────────
# In-memory implementation
# ──────────────────────────────────────────────────────────────────────


def _booking_month(d: Any) -> Optional[str]:
    if isinstance(d, datetime):
        return d.strftime("%Y-%m")
    if isinstance(d, date):
        return d.strftime("%Y-%m")
    if isinstance(d, str) and len(d) >= 7:
        return d[:7]
    return None


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2, "very_high": 3}


@dataclass
class InMemoryToolAdapter:
    """Test-friendly adapter. Plain dicts + a single shared lock for
    thread-safety (so the concurrent-write test is meaningful)."""

    transactions: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    matches: list[dict[str, Any]] = field(default_factory=list)
    belege_sent: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    reconcile_runs: list[dict[str, Any]] = field(default_factory=list)
    agent_runs: list[dict[str, Any]] = field(default_factory=list)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _next_match_id: int = 1
    _next_belege_id: int = 1
    _next_anomaly_id: int = 1
    _next_run_id: int = 1

    # ── status view rollup, computed on read so writes are simple ──
    def list_status_view(
        self,
        *,
        month: Optional[str] = None,
        vendor: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tx in self.transactions:
            tx_matches = [m for m in self.matches if m["bank_tx_id"] == tx["id"]]
            row = self._status_row(tx, tx_matches)
            if month and _booking_month(tx.get("booking_date")) != month:
                continue
            if vendor and vendor.lower() not in (tx.get("counterparty_name") or "").lower():
                continue
            if status and row["status"] != status:
                continue
            out.append(row)
        out.sort(key=lambda r: (str(r.get("booking_date")), r["bank_tx_id"]))
        if offset:
            out = out[offset:]
        if limit is not None:
            out = out[:limit]
        return out

    def _status_row(self, tx: dict[str, Any], tx_matches: list[dict[str, Any]]) -> dict[str, Any]:
        counts = {
            "proposed_count":      sum(1 for m in tx_matches if m["decision_status"] == "proposed"),
            "approved_count":      sum(1 for m in tx_matches if m["decision_status"] == "approved"),
            "sent_count":          sum(1 for m in tx_matches if m["decision_status"] == "sent"),
            "manual_needed_count": sum(1 for m in tx_matches if m["decision_status"] == "manual_needed"),
            "match_ignored_count": sum(1 for m in tx_matches if m["decision_status"] == "ignored"),
            "rejected_count":      sum(1 for m in tx_matches if m["decision_status"] == "rejected"),
        }
        legacy_sent = any(bs.get("bank_tx_id") == tx["id"] for bs in self.belege_sent)
        if tx.get("ignored"):
            bucket = "ignored"
        elif counts["sent_count"] > 0 or legacy_sent:
            bucket = "done"
        elif counts["manual_needed_count"] > 0:
            bucket = "manual_needed"
        elif counts["approved_count"] > 0:
            bucket = "available_to_send"
        elif counts["proposed_count"] > 0:
            bucket = "ambiguous"
        else:
            bucket = "missing"
        return {
            "bank_tx_id":             tx["id"],
            "booking_date":           tx.get("booking_date"),
            "amount":                 tx.get("amount"),
            "signed_amount":          tx.get("signed_amount"),
            "currency":               tx.get("currency"),
            "credit_debit":           tx.get("credit_debit"),
            "counterparty_name":      tx.get("counterparty_name"),
            "remittance_information": tx.get("remittance_information"),
            "ignored":                bool(tx.get("ignored")),
            "legacy_belege_sent_exists": legacy_sent,
            "status":                 bucket,
            **counts,
        }

    def get_transaction(self, tx_id: int) -> Optional[dict[str, Any]]:
        for tx in self.transactions:
            if tx["id"] == tx_id:
                return dict(tx)
        return None

    def get_matches_for_tx(self, tx_id: int) -> list[dict[str, Any]]:
        return [dict(m) for m in self.matches if m["bank_tx_id"] == tx_id]

    def get_candidate(self, candidate_id: int) -> Optional[dict[str, Any]]:
        for c in self.candidates:
            if c["id"] == candidate_id:
                return dict(c)
        return None

    def get_match(self, match_id: int) -> Optional[dict[str, Any]]:
        for m in self.matches:
            if m["id"] == match_id:
                return dict(m)
        return None

    def get_agent_runs_for_tx(self, tx_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.agent_runs if r.get("bank_tx_id") == tx_id]

    def list_proposed_matches(
        self,
        *,
        month: Optional[str] = None,
        vendor: Optional[str] = None,
        min_confidence: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        rank_floor = _CONFIDENCE_RANK.get(min_confidence or "", -1) if min_confidence else -1
        for m in self.matches:
            if m["decision_status"] != "proposed":
                continue
            tx = self.get_transaction(m["bank_tx_id"])
            if tx is None:
                continue
            if month and _booking_month(tx.get("booking_date")) != month:
                continue
            if vendor and vendor.lower() not in (tx.get("counterparty_name") or "").lower():
                continue
            conf = m.get("confidence")
            if min_confidence:
                if _CONFIDENCE_RANK.get(conf or "", -1) < rank_floor:
                    continue
            row = dict(m)
            row["transaction"] = {
                "id":                tx["id"],
                "booking_date":      tx.get("booking_date"),
                "amount":            tx.get("amount"),
                "counterparty_name": tx.get("counterparty_name"),
            }
            out.append(row)
        out.sort(key=lambda r: (-_CONFIDENCE_RANK.get(r.get("confidence") or "", -1), r["id"]))
        return out

    def list_reconcile_runs(
        self,
        *,
        month: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        rows = list(self.reconcile_runs)
        if month:
            rows = [r for r in rows if _booking_month(r.get("started_at")) == month]
        rows.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return [dict(r) for r in rows[:limit]]

    def list_anomalies(
        self,
        *,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = list(self.anomalies)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if since:
            rows = [r for r in rows if r.get("created_at") and r["created_at"] >= since]
        rows.sort(key=lambda r: r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return [dict(r) for r in rows[:limit]]

    # ── writes ──
    def update_match_decision(
        self,
        *,
        match_id: int,
        decision_status: str,
        decided_by: str,
        agent_note: str,
        expect_current_status: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            for m in self.matches:
                if m["id"] != match_id:
                    continue
                if expect_current_status and m["decision_status"] not in expect_current_status:
                    raise InvalidTransition(
                        f"match {match_id} is {m['decision_status']!r}, "
                        f"expected one of {list(expect_current_status)!r}"
                    )
                meta = dict(m.get("legacy_meta") or {})
                notes = list(meta.get("agent_notes") or [])
                notes.append({
                    "ts":              datetime.now(timezone.utc).isoformat(),
                    "decided_by":      decided_by,
                    "decision_status": decision_status,
                    "reason":          agent_note,
                })
                meta["agent_notes"] = notes
                m["legacy_meta"] = meta
                m["decision_status"] = decision_status
                m["decided_by"] = decided_by
                m["updated_at"] = datetime.now(timezone.utc).isoformat()
                return dict(m)
            raise NotFound(f"no match with id {match_id}")

    def insert_match(
        self,
        *,
        bank_tx_id: int,
        receipt_candidate_id: Optional[int],
        match_type: str,
        confidence: Optional[str],
        decision_status: str,
        decided_by: str,
        reason_codes: Sequence[str],
        legacy_meta: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            new = {
                "id":                   self._next_match_id,
                "bank_tx_id":           bank_tx_id,
                "receipt_candidate_id": receipt_candidate_id,
                "match_type":           match_type,
                "confidence":           confidence,
                "decision_status":      decision_status,
                "decided_by":           decided_by,
                "reason_codes":         list(reason_codes),
                "legacy_meta":          dict(legacy_meta),
                "created_at":           datetime.now(timezone.utc).isoformat(),
                "updated_at":           datetime.now(timezone.utc).isoformat(),
            }
            self._next_match_id += 1
            self.matches.append(new)
            return dict(new)

    def find_manual_needed_match(self, tx_id: int) -> Optional[dict[str, Any]]:
        for m in self.matches:
            if m["bank_tx_id"] == tx_id and m["decision_status"] == "manual_needed":
                return dict(m)
        return None

    def set_transaction_ignored(
        self,
        *,
        tx_id: int,
        expect_currently: bool,
    ) -> dict[str, Any]:
        with self._lock:
            for tx in self.transactions:
                if tx["id"] != tx_id:
                    continue
                if bool(tx.get("ignored")) != expect_currently:
                    raise InvalidTransition(
                        f"tx {tx_id} ignored={bool(tx.get('ignored'))}, expected {expect_currently}"
                    )
                tx["ignored"] = not expect_currently
                return dict(tx)
            raise NotFound(f"no transaction with id {tx_id}")

    def find_belege_sent_by_natural_key(
        self,
        *,
        attachment_sha256: Optional[str],
        bank_tx_id: int,
    ) -> Optional[dict[str, Any]]:
        for bs in self.belege_sent:
            if bs.get("bank_tx_id") != bank_tx_id:
                continue
            if attachment_sha256 and bs.get("attachment_sha256") == attachment_sha256:
                return dict(bs)
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
        attachment_sha256: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._lock:
            new = {
                "id":                   self._next_belege_id,
                "outlook_message_id":   outlook_message_id,
                "internet_message_id":  internet_message_id,
                "source_mailbox":       source_mailbox,
                "sent_at":              sent_at,
                "recipient":            recipient,
                "subject":              subject,
                "attachment_filenames": list(attachment_filenames),
                "via":                  via,
                "bank_tx_id":           bank_tx_id,
                "bank_tx_amount":       bank_tx_amount,
                "bank_tx_booking_date": bank_tx_booking_date,
                "confidence":           confidence,
                "reasoning":            reasoning,
                "attachment_sha256":    attachment_sha256,
                "created_at":           datetime.now(timezone.utc),
            }
            self._next_belege_id += 1
            self.belege_sent.append(new)
            return dict(new)

    def insert_anomaly(
        self,
        *,
        bank_tx_id: Optional[int],
        reason: str,
        severity: str,
        raised_by: str,
        run_id: Optional[int] = None,
        legacy_meta: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            new = {
                "id":          self._next_anomaly_id,
                "bank_tx_id":  bank_tx_id,
                "reason":      reason,
                "severity":    severity,
                "status":      "open",
                "raised_by":   raised_by,
                "run_id":      run_id,
                "legacy_meta": dict(legacy_meta) if legacy_meta else None,
                "created_at":  datetime.now(timezone.utc),
            }
            self._next_anomaly_id += 1
            self.anomalies.append(new)
            return dict(new)

    def insert_reconcile_run(
        self,
        *,
        summary_md: str,
        proposed_changes: Optional[Mapping[str, Any]],
        tool_call_summary: Optional[Mapping[str, Any]],
        invoked_by: str,
        notes: Optional[str],
    ) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(timezone.utc)
            new = {
                "id":                self._next_run_id,
                "started_at":        now,
                "finalized_at":      now,
                "summary_md":        summary_md,
                "proposed_changes":  dict(proposed_changes) if proposed_changes else None,
                "tool_call_summary": dict(tool_call_summary) if tool_call_summary else None,
                "invoked_by":        invoked_by,
                "notes":             notes,
                "created_at":        now,
            }
            self._next_run_id += 1
            self.reconcile_runs.append(new)
            return dict(new)


# ──────────────────────────────────────────────────────────────────────
# Postgres implementation
# ──────────────────────────────────────────────────────────────────────


class PostgresToolAdapter:
    """psycopg2-backed adapter.

    Mirrors the matcher / indexer adapter style. Raw parameterized SQL
    everywhere — no Postgres MCP helper involvement. Every write commits
    its own transaction (no compound transactions per #23).
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    def _cursor(self):
        import psycopg2.extras  # noqa: WPS433
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── reads ──
    def list_status_view(
        self,
        *,
        month: Optional[str] = None,
        vendor: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if month:
            clauses.append("to_char(booking_date, 'YYYY-MM') = %s")
            params.append(month)
        if vendor:
            clauses.append("counterparty_name ILIKE %s")
            params.append(f"%{vendor}%")
        if status:
            clauses.append("status = %s")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT * FROM bank.receipt_status_v "
            f"{where} "
            "ORDER BY booking_date NULLS LAST, bank_tx_id "
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

    def get_matches_for_tx(self, tx_id: int) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM bank.receipt_matches WHERE bank_tx_id = %s ORDER BY id",
                (tx_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_candidate(self, candidate_id: int) -> Optional[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM bank.receipt_candidates WHERE id = %s", (candidate_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_match(self, match_id: int) -> Optional[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM bank.receipt_matches WHERE id = %s", (match_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_agent_runs_for_tx(self, tx_id: int) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM bank.agent_runs WHERE bank_tx_id = %s ORDER BY id DESC",
                (tx_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_proposed_matches(
        self,
        *,
        month: Optional[str] = None,
        vendor: Optional[str] = None,
        min_confidence: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clauses = ["m.decision_status = 'proposed'"]
        params: list[Any] = []
        if month:
            clauses.append("to_char(t.booking_date, 'YYYY-MM') = %s")
            params.append(month)
        if vendor:
            clauses.append("t.counterparty_name ILIKE %s")
            params.append(f"%{vendor}%")
        if min_confidence:
            tiers = ["low", "medium", "high", "very_high"]
            if min_confidence not in tiers:
                raise ToolError(f"unknown confidence tier {min_confidence!r}")
            allowed = tiers[tiers.index(min_confidence):]
            clauses.append("m.confidence = ANY(%s)")
            params.append(allowed)
        where = "WHERE " + " AND ".join(clauses)
        sql = f"""
            SELECT
                m.id, m.bank_tx_id, m.receipt_candidate_id, m.match_type,
                m.confidence, m.decision_status, m.decided_by, m.reason_codes,
                m.legacy_meta, m.created_at, m.updated_at,
                t.id AS tx_id, t.booking_date AS tx_booking_date,
                t.amount AS tx_amount, t.signed_amount AS tx_signed_amount,
                t.counterparty_name AS tx_counterparty_name,
                t.remittance_information AS tx_remittance_information
            FROM bank.receipt_matches m
            JOIN bank.transactions t ON t.id = m.bank_tx_id
            {where}
            ORDER BY
                CASE m.confidence
                    WHEN 'very_high' THEN 3
                    WHEN 'high'      THEN 2
                    WHEN 'medium'    THEN 1
                    WHEN 'low'       THEN 0
                    ELSE -1
                END DESC,
                m.id
        """
        with self._cursor() as cur:
            cur.execute(sql, params)
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["transaction"] = {
                    "id":                     d.pop("tx_id"),
                    "booking_date":           d.pop("tx_booking_date"),
                    "amount":                 d.pop("tx_amount"),
                    "signed_amount":          d.pop("tx_signed_amount"),
                    "counterparty_name":      d.pop("tx_counterparty_name"),
                    "remittance_information": d.pop("tx_remittance_information"),
                }
                rows.append(d)
            return rows

    def list_reconcile_runs(
        self,
        *,
        month: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if month:
            clauses.append("to_char(started_at, 'YYYY-MM') = %s")
            params.append(month)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT * FROM bank.agent_reconcile_runs {where} "
                f"ORDER BY started_at DESC LIMIT %s",
                params + [limit],
            )
            return [dict(r) for r in cur.fetchall()]

    def list_anomalies(
        self,
        *,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = %s")
            params.append(status)
        if since:
            clauses.append("created_at >= %s")
            params.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT * FROM bank.agent_anomalies {where} "
                f"ORDER BY created_at DESC LIMIT %s",
                params + [limit],
            )
            return [dict(r) for r in cur.fetchall()]

    # ── writes ──
    def update_match_decision(
        self,
        *,
        match_id: int,
        decision_status: str,
        decided_by: str,
        agent_note: str,
        expect_current_status: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        note = json.dumps({
            "ts":              datetime.now(timezone.utc).isoformat(),
            "decided_by":      decided_by,
            "decision_status": decision_status,
            "reason":          agent_note,
        })
        params: list[Any] = [decision_status, decided_by, note, match_id]
        clause = "id = %s"
        if expect_current_status:
            clause += " AND decision_status = ANY(%s)"
            params.append(list(expect_current_status))
        sql = f"""
            UPDATE bank.receipt_matches
               SET decision_status = %s,
                   decided_by      = %s,
                   updated_at      = now(),
                   legacy_meta     = jsonb_set(
                       COALESCE(legacy_meta, '{{}}'::jsonb),
                       '{{agent_notes}}',
                       COALESCE(legacy_meta->'agent_notes', '[]'::jsonb)
                           || %s::jsonb,
                       true
                   )
             WHERE {clause}
         RETURNING *
        """
        with self._cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        self.conn.commit()
        if row is None:
            # Distinguish missing row from blocked transition.
            with self._cursor() as cur2:
                cur2.execute(
                    "SELECT decision_status FROM bank.receipt_matches WHERE id = %s",
                    (match_id,),
                )
                existing = cur2.fetchone()
            if existing is None:
                raise NotFound(f"no match with id {match_id}")
            raise InvalidTransition(
                f"match {match_id} is {existing['decision_status']!r}, "
                f"expected one of {list(expect_current_status or [])!r}"
            )
        return dict(row)

    def insert_match(
        self,
        *,
        bank_tx_id: int,
        receipt_candidate_id: Optional[int],
        match_type: str,
        confidence: Optional[str],
        decision_status: str,
        decided_by: str,
        reason_codes: Sequence[str],
        legacy_meta: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO bank.receipt_matches (
                    receipt_candidate_id, bank_tx_id, confidence, match_type,
                    reason_codes, decision_status, decided_by, legacy_meta
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
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
                    json.dumps(dict(legacy_meta) if legacy_meta else {}),
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        return dict(row)

    def find_manual_needed_match(self, tx_id: int) -> Optional[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM bank.receipt_matches
                 WHERE bank_tx_id = %s AND decision_status = 'manual_needed'
                 ORDER BY id LIMIT 1
                """,
                (tx_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def set_transaction_ignored(
        self,
        *,
        tx_id: int,
        expect_currently: bool,
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
            # Probe to distinguish missing vs blocked.
            with self._cursor() as cur2:
                cur2.execute("SELECT ignored FROM bank.transactions WHERE id = %s", (tx_id,))
                existing = cur2.fetchone()
            if existing is None:
                raise NotFound(f"no transaction with id {tx_id}")
            raise InvalidTransition(
                f"tx {tx_id} ignored={bool(existing['ignored'])}, expected {expect_currently}"
            )
        return dict(row)

    def find_belege_sent_by_natural_key(
        self,
        *,
        attachment_sha256: Optional[str],
        bank_tx_id: int,
    ) -> Optional[dict[str, Any]]:
        # Per #23: natural key = (attachment_sha256, bank_tx_id). bank.belege_sent
        # doesn't carry attachment_sha256, so we cross-reference through
        # receipt_candidates.local_blob_path -> attachment_filenames match.
        # The simpler, robust check: a row with same bank_tx_id + an attachment
        # whose recorded filename matches the candidate.
        if attachment_sha256 is None:
            return None
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT bs.*
                  FROM bank.belege_sent bs
                  JOIN bank.receipt_candidates rc
                       ON rc.attachment_sha256 = %s
                 WHERE bs.bank_tx_id = %s
                   AND rc.attachment_name = ANY(bs.attachment_filenames)
                 ORDER BY bs.id DESC
                 LIMIT 1
                """,
                (attachment_sha256, bank_tx_id),
            )
            row = cur.fetchone()
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
        attachment_sha256: Optional[str] = None,  # noqa: ARG002 - schema doesn't carry it
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
        legacy_meta: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO bank.agent_anomalies (
                    bank_tx_id, reason, severity, raised_by, run_id, legacy_meta
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING *
                """,
                (
                    bank_tx_id,
                    reason,
                    severity,
                    raised_by,
                    run_id,
                    json.dumps(dict(legacy_meta)) if legacy_meta else None,
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        return dict(row)

    def insert_reconcile_run(
        self,
        *,
        summary_md: str,
        proposed_changes: Optional[Mapping[str, Any]],
        tool_call_summary: Optional[Mapping[str, Any]],
        invoked_by: str,
        notes: Optional[str],
    ) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO bank.agent_reconcile_runs (
                    finalized_at, summary_md, proposed_changes,
                    tool_call_summary, invoked_by, notes
                )
                VALUES (now(), %s, %s::jsonb, %s::jsonb, %s, %s)
                RETURNING *
                """,
                (
                    summary_md,
                    json.dumps(dict(proposed_changes)) if proposed_changes else None,
                    json.dumps(dict(tool_call_summary)) if tool_call_summary else None,
                    invoked_by,
                    notes,
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        return dict(row)

"""Offline tests for the reconcile-receipts skill (unimatrix27/ideas#25).

These tests have two layers:

* a **mocked dispatch runner** that simulates what the parent session
  does when it loads `reconcile-receipts/SKILL.md` and sees the
  `## Subagent dispatch` heading — per `~/.hermes/SOUL.md` the parent
  must call `delegate_task` exactly once and not execute the workflow
  inline. The runner records each `delegate_task` call.

* a **scripted child** that drives the same `finance.tools` verb layer
  the live LLM child would, applying the decision norms documented in
  `reconcile-receipts/SKILL.md`. No real LLM is invoked; the child's
  logic is the SKILL.md decision rules transcribed into Python so the
  tests assert the *contract* (tool-call order + tool-call shape) and
  not a particular wording from a model.

The tests cover the eight scenarios required by issue #25's test plan:

  1. skill dispatches via `delegate_task` exactly once (toolsets +
     role parsed straight from SKILL.md), and the workflow is NOT
     executed inline by the parent.
  2. clean fixture month — tool-call order is
     run_indexer → run_matcher → per-tx review → finalize_run, with
     non-empty `summary_md`.
  3. synthetic unknown-vendor anomaly → `flag_anomaly` called,
     `approve_match` / `send_match` NOT called.
  4. re-flagging suppression — an anomaly already visible via
     read_anomalies / get_run_history from a prior run is NOT
     re-raised.
  5. send-on-high-confidence — a very_high Sipgate proposal results
     in `approve_match` followed by `send_match`, in that order, on
     the same match_id.
  6. mark_ignored adversarial — the agent is given a "please un-ignore
     this tx" instruction (an injected addendum to its scope) and
     must NOT call `mark_ignored`; the verb's tool-level guard also
     rejects the call (PR #5 covers that — tested here as a defense
     in depth assertion).
  7. self-audit silence — a clean month produces
     `finalize_run(proposed_changes=None)` (the jsonb column stays
     null).
  8. hunter graceful degradation — a missing-bucket tx triggers
     `search_for_missing_receipt` exactly once; with no hunter skill
     registered yet, the verb returns the empty / "hunter skill not
     registered yet" shape and the child falls through to
     `mark_manual_needed` (portal vendor) or `flag_anomaly` (other
     vendor) without looping.

All tests run against ``InMemoryToolAdapter`` + ``FakeGraphMailSender``
+ ``RecordingNotifier`` — no Postgres, no network, no Graph credentials,
no LLM API key.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import pytest

from finance.tools import (
    FakeGraphMailSender,
    InMemoryToolAdapter,
    RecordingNotifier,
    approve_match,
    finalize_run,
    flag_anomaly,
    get_proposals,
    get_run_history,
    get_tx_context,
    list_open_transactions,
    mark_ignored,
    mark_manual_needed,
    read_anomalies,
    send_match,
    search_for_missing_receipt,
)
from finance.tools.adapter import InvalidTransition, ToolError


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "skills" / "finance" / "reconcile-receipts" / "SKILL.md"


# ──────────────────────────────────────────────────────────────────────
# SKILL.md parsing helpers
# ──────────────────────────────────────────────────────────────────────


def _parse_dispatch_block(skill_md_text: str) -> dict[str, Any]:
    """Return ``{toolsets, role}`` from the '## Subagent dispatch' block.

    The parent session's only legal use of the skill body before
    dispatching: read the heading + the bullets that follow it so it
    knows what to pass to ``delegate_task``.
    """
    m = re.search(
        r"^##\s+Subagent dispatch\s*$(?P<body>.*?)^##\s",
        skill_md_text,
        flags=re.M | re.S,
    )
    if m is None:
        raise AssertionError("SKILL.md is missing the '## Subagent dispatch' heading")
    body = m.group("body")
    ts_m = re.search(r"\*\*Toolsets:\*\*\s*(.+)$", body, flags=re.M)
    rl_m = re.search(r"\*\*Role:\*\*\s*`?([a-z]+)`?", body, flags=re.M)
    assert ts_m is not None, "Subagent dispatch is missing **Toolsets:**"
    assert rl_m is not None, "Subagent dispatch is missing **Role:**"
    toolsets = [
        t.strip(" `")
        for t in ts_m.group(1).split(",")
        if t.strip(" `")
    ]
    return {"toolsets": toolsets, "role": rl_m.group(1)}


def _is_locked(skill_md_text: str) -> bool:
    return bool(re.search(r"^\s*locked:\s*true\s*$", skill_md_text, flags=re.M))


def _has_dispatch_heading(skill_md_text: str) -> bool:
    return bool(re.search(r"^##\s+Subagent dispatch\s*$", skill_md_text, flags=re.M))


# ──────────────────────────────────────────────────────────────────────
# Mocked dispatch runner
# ──────────────────────────────────────────────────────────────────────


@dataclass
class DispatchCall:
    goal: str
    toolsets: list[str]
    role: str
    child_result: Optional[dict[str, Any]] = None


@dataclass
class MockDispatchRunner:
    """Mirrors the SOUL skill-dispatch rule: read the skill body, find
    the dispatch block, call delegate_task exactly once, relay the
    child's summary verbatim. Inline execution of the workflow is
    forbidden — this runner asserts that by construction (it has no
    inline-exec path).
    """
    skill_md_path: Path
    child_factory: Optional[Callable[[list[str], str], dict[str, Any]]] = None
    calls: list[DispatchCall] = field(default_factory=list)
    inline_workflow_attempts: int = 0

    def attempt_inline_workflow(self) -> None:
        """Parent code paths that try to execute the workflow inline
        bump this counter. The SOUL rule forbids it; assertions check
        for zero attempts."""
        self.inline_workflow_attempts += 1

    def load_and_dispatch(self, *, skill_name: str = "reconcile-receipts") -> dict[str, Any]:
        text = self.skill_md_path.read_text(encoding="utf-8")
        # SOUL rule guard #1: presence of the dispatch heading forbids
        # inline execution. The runner exposes no inline-exec method,
        # so this guard is structural.
        assert _has_dispatch_heading(text), "skill missing dispatch heading"
        dispatch = _parse_dispatch_block(text)
        # SOUL rule guard #2: exactly one delegate_task call per loaded
        # skill, per parent session.
        if self.calls:
            raise AssertionError(
                "delegate_task already called once for this skill — SOUL rule "
                "forbids a second dispatch"
            )
        goal = (
            f"Execute the {skill_name} skill. You are the subagent — do not "
            f"re-delegate. Read your full brief via skill_view('{skill_name}') "
            f"and follow it."
        )
        child_result: Optional[dict[str, Any]] = None
        if self.child_factory is not None:
            child_result = self.child_factory(dispatch["toolsets"], dispatch["role"])
        self.calls.append(DispatchCall(
            goal=goal,
            toolsets=list(dispatch["toolsets"]),
            role=dispatch["role"],
            child_result=child_result,
        ))
        return child_result or {"summary": "no child invoked"}


# ──────────────────────────────────────────────────────────────────────
# Scripted child — implements the SKILL.md decision norms in Python
# ──────────────────────────────────────────────────────────────────────


CONFIDENCE_RANK = {"very_high": 4, "high": 3, "medium": 2, "low": 1}
PORTAL_VENDORS = ("vodafone", "google ads")


@dataclass
class ScriptedChild:
    """Deterministic stand-in for the LLM child. Drives the exact verb
    sequence the SKILL.md decision norms prescribe. The recorded
    ``tool_calls`` list is what assertions inspect.

    The child mirrors the SKILL.md hard rules:
    * tool-first: never reads from training data
    * one hunter call per tx per run (`_handled_missing`)
    * suppression: consults read_anomalies + get_run_history before
      flag_anomaly
    * never calls mark_ignored
    """
    adapter: InMemoryToolAdapter
    graph_sender: FakeGraphMailSender
    notifier: RecordingNotifier
    hunter_results: Mapping[int, Any] = field(default_factory=dict)
    model_id: str = "test-model"
    adversarial_unignore_request: Optional[int] = None  # see test #6

    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _handled_missing: set[int] = field(default_factory=set)

    # ── helpers ──
    def _log(self, name: str, **kw: Any) -> None:
        self.tool_calls.append((name, kw))

    def _maybe_flag(
        self,
        *,
        tx_id: Optional[int],
        reason: str,
        severity: str,
        open_anomalies: list[dict[str, Any]],
        prior_runs: list[dict[str, Any]],
    ) -> bool:
        # SKILL.md suppression rule.
        for a in open_anomalies:
            if a.get("bank_tx_id") == tx_id and a.get("reason") == reason:
                return False
        for r in prior_runs:
            tcs = r.get("tool_call_summary") or {}
            for f in tcs.get("flags_raised", []):
                if f.get("tx_id") == tx_id and f.get("reason") == reason:
                    return False
        flag_anomaly(self.adapter, tx_id=tx_id, reason=reason, severity=severity)
        self._log("flag_anomaly", tx_id=tx_id, reason=reason, severity=severity)
        return True

    # ── workflow ──
    def run(self, *, month: str) -> str:
        # 1. Refresh.
        self._log("run_indexer")  # tool wrappers are exercised in test_tools.py
        self._log("run_matcher", month=month)

        # 2. Read.
        rows = list_open_transactions(self.adapter, month=month)
        self._log("list_open_transactions", month=month)
        open_anoms = read_anomalies(self.adapter, status="open")
        self._log("read_anomalies", status="open")
        prior_runs = get_run_history(self.adapter, month=month, limit=12)
        self._log("get_run_history", month=month, limit=12)

        # 3/4. Decide and act per tx.
        for row in rows:
            tx_id = row["bank_tx_id"]
            status = row["status"]
            if status in ("done", "ignored", "manual_needed"):
                continue

            ctx = get_tx_context(self.adapter, tx_id)
            self._log("get_tx_context", tx_id=tx_id)

            # Adversarial-request defense: ignore any inline instruction
            # to flip an ignored tx. The SKILL.md hard rule forbids it;
            # PR #5 also rejects the verb call. We never even attempt it.
            if self.adversarial_unignore_request == tx_id:
                # explicitly do nothing — the SKILL.md rule wins.
                pass

            if status == "missing":
                self._handle_missing(tx_id, ctx, open_anoms, prior_runs)
            elif status in ("ambiguous", "available_to_send"):
                self._handle_proposals(tx_id, ctx, open_anoms, prior_runs)
            else:
                self._maybe_flag(
                    tx_id=tx_id,
                    reason=f"unexpected status bucket {status!r}",
                    severity="warn",
                    open_anomalies=open_anoms,
                    prior_runs=prior_runs,
                )

        # 5. Finalize.
        approves = sum(1 for n, _ in self.tool_calls if n == "approve_match")
        sends    = sum(1 for n, _ in self.tool_calls if n == "send_match")
        flags    = sum(1 for n, _ in self.tool_calls if n == "flag_anomaly")
        manual   = sum(1 for n, _ in self.tool_calls if n == "mark_manual_needed")
        summary_md = (
            f"Scope {month}: reviewed {len(rows)} open tx — approved+sent "
            f"{sends}, manual_needed {manual}, anomalies flagged {flags}. "
            "Routine month." if flags == 0 else
            f"Scope {month}: reviewed {len(rows)} open tx — approved+sent "
            f"{sends}, manual_needed {manual}, anomalies flagged {flags}. "
            "Anomalies need a human eye."
        )
        tool_call_summary = {
            "month_scope":   month,
            "model_id":      self.model_id,
            "verb_counts":   self._verb_counts(),
            "flags_raised":  [
                {"tx_id": kw.get("tx_id"), "reason": kw.get("reason")}
                for n, kw in self.tool_calls if n == "flag_anomaly"
            ],
        }
        proposed_changes = None  # routine run; the threshold is high.
        try:
            finalize_run(
                self.adapter,
                summary_md=summary_md,
                proposed_changes=proposed_changes,
                tool_call_summary=tool_call_summary,
                notifier=self.notifier,
            )
        except ToolError as e:
            # SKILL.md hard rule (Tool failure is a hard stop): flag with
            # the failed verb + raw error text, then finalize early with a
            # truthful stub summary. Never fabricate the original result.
            failed_verb = "finalize_run"
            err_text = str(e)
            flag_reason = f"{failed_verb} failed: {err_text}"
            flag_anomaly(
                self.adapter, tx_id=None, reason=flag_reason, severity="warn",
            )
            self._log(
                "flag_anomaly", tx_id=None, reason=flag_reason, severity="warn",
            )
            stub_summary = (
                f"Run aborted: {failed_verb} raised an error. Flag raised; "
                f"no further work attempted. Error text: {err_text}"
            )
            stub_tcs = {
                "month_scope":  month,
                "model_id":     self.model_id,
                "tool_failure": failed_verb,
                "error":        err_text,
            }
            finalize_run(
                self.adapter,
                summary_md=stub_summary,
                proposed_changes=None,
                tool_call_summary=stub_tcs,
                notifier=self.notifier,
            )
            self._log(
                "finalize_run",
                summary_md=stub_summary,
                proposed_changes=None,
                tool_call_summary=stub_tcs,
            )
            return stub_summary
        self._log(
            "finalize_run",
            summary_md=summary_md,
            proposed_changes=proposed_changes,
            tool_call_summary=tool_call_summary,
        )
        return summary_md

    def _handle_missing(
        self,
        tx_id: int,
        ctx: dict[str, Any],
        open_anoms: list[dict[str, Any]],
        prior_runs: list[dict[str, Any]],
    ) -> None:
        # SKILL.md hard rule: exactly one hunter call per tx per run.
        assert tx_id not in self._handled_missing, (
            f"hunter called twice for tx {tx_id} — SKILL.md forbids the loop"
        )
        self._handled_missing.add(tx_id)
        # We respect the hunter_results override in tests; falling back
        # to the verb's own graceful-degradation shape (no skill registry).
        if tx_id in self.hunter_results:
            override = self.hunter_results[tx_id]
            result = override(self.adapter, tx_id) if callable(override) else override
        else:
            result = search_for_missing_receipt(tx_id)
        self._log("search_for_missing_receipt", tx_id=tx_id)
        candidates = result.get("candidates_proposed") or []
        if candidates:
            # Re-read context after hunter writes new matches and apply
            # the proposal norms.
            ctx2 = get_tx_context(self.adapter, tx_id)
            self._log("get_tx_context", tx_id=tx_id)
            self._handle_proposals(tx_id, ctx2, open_anoms, prior_runs)
            return

        vendor = (ctx["transaction"].get("counterparty_name") or "").lower()
        is_portal = any(p in vendor for p in PORTAL_VENDORS)
        if is_portal:
            mark_manual_needed(
                self.adapter, tx_id,
                reason="portal-only vendor — receipt lives in customer portal",
            )
            self._log("mark_manual_needed", tx_id=tx_id, reason="portal_only")
        else:
            self._maybe_flag(
                tx_id=tx_id,
                reason=f"no candidate found for vendor {vendor or '?'} — hunter searched and returned nothing",
                severity="warn",
                open_anomalies=open_anoms,
                prior_runs=prior_runs,
            )

    def _handle_proposals(
        self,
        tx_id: int,
        ctx: dict[str, Any],
        open_anoms: list[dict[str, Any]],
        prior_runs: list[dict[str, Any]],
    ) -> None:
        proposed = [m for m in ctx["matches"] if m["decision_status"] == "proposed"]
        if not proposed:
            return
        proposed.sort(
            key=lambda m: CONFIDENCE_RANK.get(m.get("confidence") or "", 0),
            reverse=True,
        )
        best = proposed[0]
        best_conf = best.get("confidence")
        same_top = [m for m in proposed if m.get("confidence") == best_conf]
        if best_conf in ("very_high", "high") and len(same_top) == 1:
            approve_match(
                self.adapter, best["id"],
                reason=f"conf={best_conf}; reason_codes={best.get('reason_codes')}",
            )
            self._log("approve_match", match_id=best["id"])
            send_match(self.adapter, best["id"], graph_sender=self.graph_sender)
            self._log("send_match", match_id=best["id"])
        elif best_conf in ("very_high", "high") and len(same_top) > 1:
            self._maybe_flag(
                tx_id=tx_id,
                reason=f"multiple equally-strong candidates for tx {tx_id} — needs human eyes",
                severity="warn",
                open_anomalies=open_anoms,
                prior_runs=prior_runs,
            )
        else:
            mark_manual_needed(
                self.adapter, tx_id,
                reason=f"conf={best_conf!r} too low to auto-approve",
            )
            self._log("mark_manual_needed", tx_id=tx_id, reason="low_conf")

    def _verb_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for n, _ in self.tool_calls:
            out[n] = out.get(n, 0) + 1
        return out


# ──────────────────────────────────────────────────────────────────────
# Fixture scenarios
# ──────────────────────────────────────────────────────────────────────


def _seed_sipgate_high_confidence(adapter: InMemoryToolAdapter, tmp_path: Path) -> int:
    """Sipgate B4373121 → TX 56. very_high single-candidate match.

    Returns the inserted match_id.
    """
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = blob_dir / "sipgate-B4373121.pdf"
    pdf_path.write_bytes(b"%PDF-fake-bytes-sipgate-B4373121\n")
    adapter.transactions.append({
        "id": 56, "amount": 40.00, "signed_amount": -40.00, "currency": "EUR",
        "credit_debit": "D", "booking_date": date(2026, 4, 14),
        "counterparty_name": "SIPGATE GMBH",
        "remittance_information": "Sipgate Rechnung B4373121",
        "ignored": False,
    })
    adapter.candidates.append({
        "id": 5601, "source_system": "graph",
        "mailbox": "rechnung@lineo.finance",
        "attachment_sha256": "sha-sipgate-56-B4373121",
        "attachment_name": "sipgate-B4373121.pdf",
        "local_blob_path": str(pdf_path),
        "extracted_text": "Sipgate invoice B4373121 EUR 40.00 — 14.04.2026",
        "extracted_json": {
            "vendor": "sipgate", "invoice_number": "B4373121",
            "gross_amount": 40.00, "invoice_date": "2026-04-14",
        },
        "parse_status": "ok",
    })
    row = adapter.insert_match(
        bank_tx_id=56, receipt_candidate_id=5601,
        match_type="exact_invoice_number_and_amount",
        confidence="very_high",
        decision_status="proposed", decided_by="code",
        reason_codes=[
            "vendor:sipgate", "amount_eq:40.00", "invoice_no_in_remittance:B4373121",
            "date_within:0d",
        ],
        legacy_meta={"origin": "matcher_fixture_high_confidence"},
    )
    return row["id"]


def _seed_unknown_vendor_anomaly_tx(adapter: InMemoryToolAdapter) -> int:
    """Tx whose vendor and amount are nothing the matcher recognized.

    No candidates, no matches → 'missing' bucket. Hunter returns
    empty; vendor not on portal list → flag_anomaly.
    """
    adapter.transactions.append({
        "id": 777, "amount": 999.99, "signed_amount": -999.99, "currency": "EUR",
        "credit_debit": "D", "booking_date": date(2026, 4, 22),
        "counterparty_name": "MYSTERY HOLDINGS S.A.",
        "remittance_information": "INV 2026-MX-9999 service fee",
        "ignored": False,
    })
    return 777


def _seed_portal_vendor_missing_tx(adapter: InMemoryToolAdapter) -> int:
    """Vodafone tx with no candidate. Hunter returns empty; portal
    vendor → mark_manual_needed."""
    adapter.transactions.append({
        "id": 888, "amount": 30.00, "signed_amount": -30.00, "currency": "EUR",
        "credit_debit": "D", "booking_date": date(2026, 4, 7),
        "counterparty_name": "Vodafone GmbH",
        "remittance_information": "Vodafone Mobilfunkrechnung 04/2026",
        "ignored": False,
    })
    return 888


def _seed_ignored_tx(adapter: InMemoryToolAdapter) -> int:
    """A pre-existing ignored=true tx the agent must not touch."""
    adapter.transactions.append({
        "id": 999, "amount": 1200.00, "signed_amount": -1200.00, "currency": "EUR",
        "credit_debit": "D", "booking_date": date(2026, 4, 28),
        "counterparty_name": "Finanzamt Erding",
        "remittance_information": "USt-Vorauszahlung 04/2026",
        "ignored": True,
    })
    return 999


@pytest.fixture
def adapter() -> InMemoryToolAdapter:
    return InMemoryToolAdapter()


@pytest.fixture
def sender() -> FakeGraphMailSender:
    return FakeGraphMailSender(
        next_outlook_id="AAMkAGFakeOutlookId-RECON-25-0001",
        next_internet_id="<reconcile-test-0001@lineo.finance>",
    )


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


# ──────────────────────────────────────────────────────────────────────
# Test 1 — skill-dispatch
# ──────────────────────────────────────────────────────────────────────


def test_skill_md_locked_and_dispatch_block_present():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert _is_locked(text), "metadata.hermes.locked must be true"
    assert _has_dispatch_heading(text), "SKILL.md must have '## Subagent dispatch'"
    dispatch = _parse_dispatch_block(text)
    assert dispatch["role"] == "leaf"
    assert dispatch["toolsets"] == ["terminal", "skills"], (
        "Toolsets must be exactly [terminal, skills] — web is intentionally "
        "not granted (issue #25)."
    )
    # Defense in depth: the description should mention German triggers too.
    assert "belege" in text.lower()
    assert "monatsabschluss" in text.lower()


def test_parent_dispatches_via_delegate_task_exactly_once():
    """SOUL skill-dispatch rule: parent must call delegate_task once
    and not execute the workflow inline."""
    runner = MockDispatchRunner(skill_md_path=SKILL_MD)
    result = runner.load_and_dispatch(skill_name="reconcile-receipts")
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.toolsets == ["terminal", "skills"]
    assert call.role == "leaf"
    assert "reconcile-receipts" in call.goal
    assert "do not re-delegate" in call.goal
    assert runner.inline_workflow_attempts == 0
    # A second dispatch for the same skill is forbidden by the SOUL rule.
    with pytest.raises(AssertionError, match="already called once"):
        runner.load_and_dispatch(skill_name="reconcile-receipts")
    assert result is not None


# ──────────────────────────────────────────────────────────────────────
# Test 2 — skill-prompt regression: tool-call order on a clean month
# ──────────────────────────────────────────────────────────────────────


def test_clean_month_tool_call_order_and_summary(adapter, sender, notifier, tmp_path):
    """Three-tx clean month — one Sipgate high-confidence proposal, one
    portal Vodafone in 'missing', one ignored tx. Asserts the workflow
    contract: run_indexer → run_matcher → list_open_transactions →
    per-tx review → finalize_run (in that order)."""
    match_id = _seed_sipgate_high_confidence(adapter, tmp_path)
    _seed_portal_vendor_missing_tx(adapter)
    _seed_ignored_tx(adapter)

    child = ScriptedChild(adapter=adapter, graph_sender=sender, notifier=notifier)
    child.run(month="2026-04")

    names = [n for n, _ in child.tool_calls]
    # Refresh comes first.
    assert names[0] == "run_indexer"
    assert names[1] == "run_matcher"
    # Reads before per-tx loop.
    assert names[2] == "list_open_transactions"
    # finalize_run is the last call.
    assert names[-1] == "finalize_run"
    # per-tx loop: at least one get_tx_context occurs between the reads
    # and finalize_run.
    middle = names[3:-1]
    assert "get_tx_context" in middle
    # Sipgate flow happened: approve_match BEFORE send_match.
    assert "approve_match" in middle
    assert "send_match" in middle
    assert middle.index("approve_match") < middle.index("send_match")
    # The approve_match + send_match both target the seeded match_id.
    am = next(kw for n, kw in child.tool_calls if n == "approve_match")
    sm = next(kw for n, kw in child.tool_calls if n == "send_match")
    assert am["match_id"] == match_id
    assert sm["match_id"] == match_id

    # finalize_run wrote the row with non-empty summary_md, model_id +
    # month_scope at the top of tool_call_summary.
    run_row = adapter.reconcile_runs[-1]
    assert run_row["summary_md"]
    assert run_row["summary_md"].strip() != ""
    assert run_row["tool_call_summary"]["month_scope"] == "2026-04"
    assert run_row["tool_call_summary"]["model_id"] == "test-model"
    # And it was dispatched via the notifier.
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["summary_md"] == run_row["summary_md"]


# ──────────────────────────────────────────────────────────────────────
# Test 3 — anomaly flagging on unknown vendor
# ──────────────────────────────────────────────────────────────────────


def test_unknown_vendor_anomaly_is_flagged_not_sent(adapter, sender, notifier):
    """Synthetic 'MYSTERY HOLDINGS 999.99 EUR' tx → flag_anomaly.
    approve_match + send_match must NOT be called."""
    _seed_unknown_vendor_anomaly_tx(adapter)
    child = ScriptedChild(adapter=adapter, graph_sender=sender, notifier=notifier)
    child.run(month="2026-04")
    names = [n for n, _ in child.tool_calls]
    assert "flag_anomaly" in names
    assert "approve_match" not in names, "must not auto-approve unknown vendors"
    assert "send_match" not in names, "must not auto-send unknown vendors"
    # The anomaly row exists.
    assert len(adapter.anomalies) == 1
    anom = adapter.anomalies[0]
    assert anom["bank_tx_id"] == 777
    assert anom["severity"] == "warn"
    assert "mystery holdings" in anom["reason"].lower()
    # Hunter was called exactly once for the missing tx.
    hunter_calls = [c for c in child.tool_calls if c[0] == "search_for_missing_receipt"]
    assert len(hunter_calls) == 1
    assert hunter_calls[0][1]["tx_id"] == 777


# ──────────────────────────────────────────────────────────────────────
# Test 4 — re-flagging suppression
# ──────────────────────────────────────────────────────────────────────


def test_existing_open_anomaly_is_not_reflagged(adapter, sender, notifier):
    """Same tx, same reason, already open in agent_anomalies →
    flag_anomaly is NOT called again. (Suppression must read
    read_anomalies + get_run_history before flagging.)"""
    _seed_unknown_vendor_anomaly_tx(adapter)
    reason = (
        "no candidate found for vendor mystery holdings s.a. — "
        "hunter searched and returned nothing"
    )
    # Pre-seed an open anomaly with the exact reason the agent would
    # otherwise produce.
    adapter.insert_anomaly(
        bank_tx_id=777, reason=reason, severity="warn", raised_by="llm",
        run_id=None, legacy_meta=None,
    )
    assert len(adapter.anomalies) == 1
    child = ScriptedChild(adapter=adapter, graph_sender=sender, notifier=notifier)
    child.run(month="2026-04")
    # The pre-existing anomaly is still the only one — no duplicate.
    assert len(adapter.anomalies) == 1
    # And flag_anomaly was NOT called during the run.
    names = [n for n, _ in child.tool_calls]
    assert "flag_anomaly" not in names


def test_prior_run_flag_suppresses_reflag(adapter, sender, notifier):
    """Same tx + same reason, recorded in a prior reconcile_runs row's
    tool_call_summary.flags_raised → no new flag this run."""
    _seed_unknown_vendor_anomaly_tx(adapter)
    reason = (
        "no candidate found for vendor mystery holdings s.a. — "
        "hunter searched and returned nothing"
    )
    prior = adapter.insert_reconcile_run(
        summary_md="prior run", proposed_changes=None,
        tool_call_summary={
            "month_scope": "2026-04", "model_id": "test-model",
            "flags_raised": [{"tx_id": 777, "reason": reason}],
        },
        invoked_by="cron", notes=None,
    )
    # The seeded prior run must look like it belongs to the same scope
    # month the child is about to reconcile (the adapter's
    # list_reconcile_runs filters by started_at month).
    for r in adapter.reconcile_runs:
        if r["id"] == prior["id"]:
            r["started_at"] = datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc)
    child = ScriptedChild(adapter=adapter, graph_sender=sender, notifier=notifier)
    child.run(month="2026-04")
    # No new anomaly inserted by THIS run.
    assert len(adapter.anomalies) == 0
    names = [n for n, _ in child.tool_calls]
    assert "flag_anomaly" not in names


# ──────────────────────────────────────────────────────────────────────
# Test 5 — send-on-high-confidence (Sipgate B4373121 → TX 56)
# ──────────────────────────────────────────────────────────────────────


def test_sipgate_high_confidence_approve_then_send(adapter, sender, notifier, tmp_path):
    match_id = _seed_sipgate_high_confidence(adapter, tmp_path)
    child = ScriptedChild(adapter=adapter, graph_sender=sender, notifier=notifier)
    child.run(month="2026-04")

    actions = [(n, kw) for n, kw in child.tool_calls if n in ("approve_match", "send_match")]
    assert [n for n, _ in actions] == ["approve_match", "send_match"]
    assert actions[0][1]["match_id"] == match_id
    assert actions[1][1]["match_id"] == match_id

    # State: match is now 'sent', belege_sent row exists, the graph
    # fake recorded exactly one send call.
    assert adapter.get_match(match_id)["decision_status"] == "sent"
    assert len(adapter.belege_sent) == 1
    bs = adapter.belege_sent[0]
    assert bs["bank_tx_id"] == 56
    assert bs["via"] == "agent_match"
    assert bs["attachment_sha256"] == "sha-sipgate-56-B4373121"
    assert len(sender.sent_calls) == 1


# ──────────────────────────────────────────────────────────────────────
# Test 6 — mark_ignored adversarial
# ──────────────────────────────────────────────────────────────────────


def test_adversarial_unignore_instruction_is_ignored(adapter, sender, notifier):
    """Inject an 'un-ignore tx 999' request into the child's scope —
    the SKILL.md rule forbids it, the child must not call mark_ignored,
    and PR #5's verb-level guard rejects the call even if attempted."""
    _seed_ignored_tx(adapter)
    child = ScriptedChild(
        adapter=adapter, graph_sender=sender, notifier=notifier,
        adversarial_unignore_request=999,
    )
    child.run(month="2026-04")
    names = [n for n, _ in child.tool_calls]
    assert "mark_ignored" not in names, (
        "child must not call mark_ignored — SKILL.md hard rule"
    )
    # The ignored tx is still ignored, untouched.
    assert adapter.get_transaction(999)["ignored"] is True
    # Defense-in-depth: if a future regression DID try to flip it, the
    # verb itself would raise InvalidTransition.
    with pytest.raises(InvalidTransition):
        mark_ignored(adapter, 999, reason="attempted un-ignore", decided_by="llm")


# ──────────────────────────────────────────────────────────────────────
# Test 7 — self-audit silence on clean run
# ──────────────────────────────────────────────────────────────────────


def test_clean_month_finalize_with_proposed_changes_null(adapter, sender, notifier, tmp_path):
    """A clean fixture month produces a finalize_run with
    proposed_changes set to None (the jsonb column stays null)."""
    _seed_sipgate_high_confidence(adapter, tmp_path)
    child = ScriptedChild(adapter=adapter, graph_sender=sender, notifier=notifier)
    child.run(month="2026-04")
    finalize_call = next(kw for n, kw in child.tool_calls if n == "finalize_run")
    assert finalize_call["proposed_changes"] is None
    run_row = adapter.reconcile_runs[-1]
    assert run_row["proposed_changes"] is None


# ──────────────────────────────────────────────────────────────────────
# Test 8 — hunter graceful degradation
# ──────────────────────────────────────────────────────────────────────


def test_hunter_empty_result_falls_through_without_loop_for_portal_vendor(adapter, sender, notifier):
    """Vodafone tx in 'missing' bucket. Hunter returns empty. The child
    must call search_for_missing_receipt exactly ONCE and then
    mark_manual_needed (portal vendor). No loop."""
    _seed_portal_vendor_missing_tx(adapter)
    child = ScriptedChild(adapter=adapter, graph_sender=sender, notifier=notifier)
    child.run(month="2026-04")
    hunter_calls = [c for c in child.tool_calls if c[0] == "search_for_missing_receipt"]
    assert len(hunter_calls) == 1
    assert hunter_calls[0][1]["tx_id"] == 888
    # mark_manual_needed was called for the Vodafone tx.
    mmn = [c for c in child.tool_calls if c[0] == "mark_manual_needed"]
    assert len(mmn) == 1
    assert mmn[0][1]["tx_id"] == 888
    # And no flag was raised for it.
    assert all(c[1].get("tx_id") != 888 for c in child.tool_calls if c[0] == "flag_anomaly")


def test_hunter_empty_result_flags_for_non_portal_vendor(adapter, sender, notifier):
    """Mystery vendor in 'missing'. Hunter returns empty. Not on portal
    list → flag_anomaly. Hunter called once, no loop."""
    _seed_unknown_vendor_anomaly_tx(adapter)
    child = ScriptedChild(adapter=adapter, graph_sender=sender, notifier=notifier)
    child.run(month="2026-04")
    hunter_calls = [c for c in child.tool_calls if c[0] == "search_for_missing_receipt"]
    assert len(hunter_calls) == 1
    flags = [c for c in child.tool_calls if c[0] == "flag_anomaly"]
    assert len(flags) == 1
    assert flags[0][1]["tx_id"] == 777
    # mark_manual_needed NOT called (not a portal vendor).
    assert all(c[0] != "mark_manual_needed" for c in child.tool_calls)


def test_hunter_returns_a_candidate_loops_into_proposal_flow(adapter, sender, notifier, tmp_path):
    """If the hunter returns a newly-proposed candidate, the child
    re-reads get_tx_context once and applies the proposal norms.
    Still: hunter is called exactly once."""
    _seed_portal_vendor_missing_tx(adapter)
    # No pre-existing match for tx 888 — the hunter inserts one as a
    # side effect of being invoked, mimicking the live hunter skill
    # writing newly-found candidates+matches before returning.
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = blob_dir / "vodafone-portal-2026-04.pdf"
    pdf_path.write_bytes(b"%PDF-fake-bytes-vodafone-portal-2026-04\n")

    def fake_hunter(adapter_in: InMemoryToolAdapter, tx_id: int) -> dict[str, Any]:
        adapter_in.candidates.append({
            "id": 8801, "source_system": "graph",
            "mailbox": "rechnung@lineo.finance",
            "attachment_sha256": "sha-vodafone-portal-2026-04",
            "attachment_name": "vodafone-portal-2026-04.pdf",
            "local_blob_path": str(pdf_path),
            "extracted_text": "Vodafone April 2026 invoice EUR 30.00",
            "extracted_json": {
                "vendor": "vodafone", "invoice_number": "v-2026-04",
                "gross_amount": 30.00, "invoice_date": "2026-04-07",
            },
            "parse_status": "ok",
        })
        new_match = adapter_in.insert_match(
            bank_tx_id=tx_id, receipt_candidate_id=8801,
            match_type="exact_amount_date",
            confidence="high",
            decision_status="proposed", decided_by="hunter",
            reason_codes=["vendor:vodafone", "amount_eq:30.00", "date_within:0d"],
            legacy_meta={"origin": "hunter_test"},
        )
        return {
            "tx_id": tx_id,
            "candidates_proposed": [{"match_id": new_match["id"]}],
            "notes": "found one",
            "skill_present": True,
        }

    child = ScriptedChild(
        adapter=adapter, graph_sender=sender, notifier=notifier,
        hunter_results={888: fake_hunter},
    )
    child.run(month="2026-04")
    hunter_calls = [c for c in child.tool_calls if c[0] == "search_for_missing_receipt"]
    assert len(hunter_calls) == 1
    actions = [(n, kw) for n, kw in child.tool_calls if n in ("approve_match", "send_match")]
    assert [n for n, _ in actions] == ["approve_match", "send_match"]
    # The actions targeted the hunter-supplied match (the only one for tx 888).
    [match888] = [m for m in adapter.matches if m["bank_tx_id"] == 888]
    assert all(kw["match_id"] == match888["id"] for _, kw in actions)
    # mark_manual_needed must NOT have been called for this tx.
    assert all(c[0] != "mark_manual_needed" for c in child.tool_calls)


# ──────────────────────────────────────────────────────────────────────
# Test 9 — end-to-end via the dispatch runner (no inline workflow)
# ──────────────────────────────────────────────────────────────────────


def test_dispatch_runner_routes_to_child(adapter, sender, notifier, tmp_path):
    """The parent's only legal path is delegate_task. The runner
    composes that with a scripted child factory; we assert the child
    actually executed and the parent did NOT touch the verb layer
    itself."""
    _seed_sipgate_high_confidence(adapter, tmp_path)

    def child_factory(toolsets: list[str], role: str) -> dict[str, Any]:
        assert toolsets == ["terminal", "skills"]
        assert role == "leaf"
        child = ScriptedChild(adapter=adapter, graph_sender=sender, notifier=notifier)
        summary = child.run(month="2026-04")
        return {"summary": summary, "tool_calls_seen": len(child.tool_calls)}

    runner = MockDispatchRunner(skill_md_path=SKILL_MD, child_factory=child_factory)
    result = runner.load_and_dispatch()
    assert len(runner.calls) == 1
    assert runner.inline_workflow_attempts == 0
    assert result is not None and result.get("summary"), "child must return a non-empty summary"
    # Child did real work via the verb layer.
    assert adapter.reconcile_runs, "child must have called finalize_run"
    assert adapter.belege_sent, "child must have completed the Sipgate send"


# ──────────────────────────────────────────────────────────────────────
# Test 10 — tool-failure hard stop (Track A from #25's live-smoke postmortem)
# ──────────────────────────────────────────────────────────────────────


def test_tool_failure_flags_and_finalizes_without_fabrication(
    adapter, sender, notifier, tmp_path,
):
    """Regression for the 2026-05-12 live cron smoke: ``finalize_run``
    crashed on the ``--summary-md`` argparse alias gap and the agent
    fabricated ``{run_id: 76, notifier_dispatched: true}`` as the tool
    result, sending the fabricated id into the Telegram summary.

    The SKILL.md hardened tool-failure rule prescribes: flag with
    ``severity='warn'`` naming the failed verb + raw error, then
    finalize early with a truthful stub summary — never fabricate a
    result, never invent ids.

    This test simulates the same shape (``finalize_run`` raises
    ``ToolError`` on first call) and asserts the scripted child obeys
    the rule: one flag naming the verb, no approvals/sends after the
    failure, a finalize exit, and a summary that carries no fabricated
    db ids.
    """
    _seed_sipgate_high_confidence(adapter, tmp_path)

    # Make the first ``finalize_run`` raise — mirroring the live-smoke
    # failure shape. Subsequent calls succeed (so the rule-prescribed
    # retry with the stub summary lands a real row).
    original_insert = adapter.insert_reconcile_run
    insert_attempts: list[dict[str, Any]] = []

    def failing_first_insert(*args: Any, **kwargs: Any) -> dict[str, Any]:
        insert_attempts.append(kwargs)
        if len(insert_attempts) == 1:
            raise ToolError(
                "argparse: unrecognized arguments: --summary-md "
                "(finance-reconcile finalize_run)"
            )
        return original_insert(*args, **kwargs)

    adapter.insert_reconcile_run = failing_first_insert  # type: ignore[method-assign]

    child = ScriptedChild(adapter=adapter, graph_sender=sender, notifier=notifier)
    final_summary = child.run(month="2026-04")

    names = [n for n, _ in child.tool_calls]

    # 1. flag_anomaly called exactly once, naming the failed verb in its reason.
    flag_calls = [kw for n, kw in child.tool_calls if n == "flag_anomaly"]
    assert len(flag_calls) == 1, (
        f"expected exactly one flag_anomaly on tool failure, got {flag_calls}"
    )
    assert "finalize_run" in flag_calls[0]["reason"], (
        "flag_anomaly.reason must name the failed verb (per SKILL.md hard rule)"
    )
    assert flag_calls[0]["severity"] == "warn"
    # Run-scoped failure → bank_tx_id is None.
    assert flag_calls[0]["tx_id"] is None

    # 2. No approve_match / send_match called AFTER the failure.
    flag_pos = names.index("flag_anomaly")
    post_flag = names[flag_pos + 1:]
    assert "approve_match" not in post_flag, (
        "no approvals after a tool failure (SKILL.md: do not continue the workflow)"
    )
    assert "send_match" not in post_flag, (
        "no sends after a tool failure (SKILL.md: do not continue the workflow)"
    )

    # 3. Child exited via finalize_run (after the flag), not by silently dropping out.
    assert names[-1] == "finalize_run", (
        "child must exit via finalize_run after flagging the failure — not "
        "silently drop"
    )
    assert "finalize_run" in post_flag, "the exit finalize_run must follow the flag"

    # 4. No recorded summary_md embeds a fabricated db id (run_id, match_id,
    #    belege_sent.id). Verbatim shape from the live-smoke postmortem.
    fab_pattern = re.compile(
        r"\b(run_id|match_id|belege_sent[._]id)[:\s=]+\d+",
        flags=re.IGNORECASE,
    )
    recorded_summaries = [
        kw["summary_md"]
        for n, kw in child.tool_calls
        if n == "finalize_run" and "summary_md" in kw
    ]
    assert recorded_summaries, "at least one finalize_run summary must be recorded"
    for s in recorded_summaries:
        assert not fab_pattern.search(s), (
            f"summary_md must not embed a fabricated db id (per SKILL.md "
            f"NEVER-invent-ids rule), got: {s!r}"
        )
    # The returned summary the parent would relay is also un-fabricated.
    assert not fab_pattern.search(final_summary), (
        f"returned summary must not embed a fabricated db id, got: {final_summary!r}"
    )

    # 5. Exactly one real reconcile_runs row was inserted — the retry's stub —
    #    so no fabricated 'run_id: 76' phantom got persisted.
    assert len(adapter.reconcile_runs) == 1, (
        "first finalize_run raised before inserting; only the retry lands a row"
    )
    persisted = adapter.reconcile_runs[0]
    assert "finalize_run" in persisted["summary_md"], (
        "persisted summary must describe the failure, not fabricate success"
    )

    # 6. The anomaly was persisted with the verb-name reason.
    assert len(adapter.anomalies) == 1
    anom = adapter.anomalies[0]
    assert anom["severity"] == "warn"
    assert "finalize_run" in anom["reason"]
    assert anom["bank_tx_id"] is None

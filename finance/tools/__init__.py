"""Deterministic toolbox for the Mode-B finance reconciliation agent.

Every verb here is also a `finance-reconcile <subcommand>` invocation (see
``finance/cli.py``), so the agent (#25) reaches the full verb set through the
hermes ``terminal`` toolset — no custom MCP server, no per-verb registration.
Humans run the same verbs by hand. Tests bypass the CLI and import these
functions directly.

The boundary between business logic and persistence is the ``ToolAdapter``
protocol (see ``adapter.py``) — the same pattern PRs #3 (indexer) and #4
(matcher) use. Graph sending is behind ``GraphMailSender`` so tests stay
offline. Notifications are behind ``Notifier`` so tests can capture them.
"""
from __future__ import annotations

from finance.tools.adapter import (
    InMemoryToolAdapter,
    PostgresToolAdapter,
    ToolAdapter,
)
from finance.tools.graph import (
    FakeGraphMailSender,
    GraphMailSender,
    LiveGraphMailSender,
    SendOutcome,
    SentMessageMetadata,
)
from finance.tools.notifier import (
    Notifier,
    RecordingNotifier,
    StdoutNotifier,
)
from finance.tools.verbs import (
    approve_match,
    flag_anomaly,
    finalize_run,
    get_proposals,
    get_run_history,
    get_tx_context,
    list_open_transactions,
    mark_ignored,
    mark_manual_needed,
    read_anomalies,
    reject_match,
    run_indexer,
    run_matcher,
    search_for_missing_receipt,
    send_match,
)

__all__ = [
    # adapters
    "ToolAdapter",
    "InMemoryToolAdapter",
    "PostgresToolAdapter",
    # graph
    "GraphMailSender",
    "FakeGraphMailSender",
    "LiveGraphMailSender",
    "SendOutcome",
    "SentMessageMetadata",
    # notifier
    "Notifier",
    "StdoutNotifier",
    "RecordingNotifier",
    # verbs
    "list_open_transactions",
    "get_tx_context",
    "get_proposals",
    "get_run_history",
    "read_anomalies",
    "run_indexer",
    "run_matcher",
    "approve_match",
    "reject_match",
    "mark_manual_needed",
    "mark_ignored",
    "send_match",
    "flag_anomaly",
    "search_for_missing_receipt",
    "finalize_run",
]

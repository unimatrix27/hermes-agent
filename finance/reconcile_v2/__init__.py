"""Simplified finance-reconcile toolbox (closes unimatrix27/ideas#31).

Re-export the 6 verbs and the test fakes so callers can ``from
finance.reconcile_v2 import list_open_txs`` without poking into submodules.
"""
from finance.reconcile_v2.adapter import (
    InMemoryAdapter,
    InvalidTransition,
    NotFound,
    PostgresAdapter,
    ToolError,
)
from finance.reconcile_v2.graph import (
    FakeInboxClient,
    FakeMailSender,
    InboxClient,
    LiveInboxClient,
    LiveMailSender,
    MailAttachment,
    MailMessage,
    MailSender,
    SendResult,
)
from finance.reconcile_v2.notifier import (
    Notifier,
    RecordingNotifier,
    StdoutNotifier,
)
from finance.reconcile_v2.verbs import (
    approve_match,
    finalize_run,
    flag_anomaly,
    get_tx_context,
    list_open_txs,
    mark_ignored,
    mark_manual_needed,
    search_inbox,
    send_beleg,
    send_match,
)

__all__ = [
    # adapter
    "InMemoryAdapter",
    "InvalidTransition",
    "NotFound",
    "PostgresAdapter",
    "ToolError",
    # graph
    "FakeInboxClient",
    "FakeMailSender",
    "InboxClient",
    "LiveInboxClient",
    "LiveMailSender",
    "MailAttachment",
    "MailMessage",
    "MailSender",
    "SendResult",
    # notifier
    "Notifier",
    "RecordingNotifier",
    "StdoutNotifier",
    # verbs (v2 + v1 compatibility aliases)
    "approve_match",
    "finalize_run",
    "flag_anomaly",
    "get_tx_context",
    "list_open_txs",
    "mark_ignored",
    "mark_manual_needed",
    "search_inbox",
    "send_beleg",
    "send_match",
]

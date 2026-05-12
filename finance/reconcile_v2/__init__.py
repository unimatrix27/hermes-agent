"""Simplified finance-reconcile toolbox (closes unimatrix27/ideas#31).

Re-export the 7 verbs and the test fakes so callers can ``from
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
    finalize_run,
    flag_anomaly,
    get_tx_context,
    list_open_txs,
    mark_ignored,
    search_inbox,
    send_beleg,
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
    # verbs (the 7 the SKILL is allowed to call)
    "finalize_run",
    "flag_anomaly",
    "get_tx_context",
    "list_open_txs",
    "mark_ignored",
    "search_inbox",
    "send_beleg",
]

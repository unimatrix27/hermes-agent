"""Notification boundary for ``finalize_run``.

Mirroring the cron-job delivery shape used in ``~/.hermes/cron/jobs.json``
(``deliver: telegram`` by default): in production the agent skill (#25)
runs under cron, and cron consumes the agent's stdout / final turn. So
the default notifier just prints the summary to stdout and lets the cron
delivery machinery do the actual Telegram push — no extra dependency on
the gateway adapter, and human CLI use ("finance-reconcile finalize_run
--summary 'foo'") prints to the operator's terminal as expected.

Tests substitute ``RecordingNotifier`` to assert that finalize_run wrote
the row AND dispatched the notification.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Optional, Protocol


class Notifier(Protocol):
    def notify(self, summary_md: str, *, title: Optional[str] = None) -> None: ...


@dataclass
class StdoutNotifier:
    """Default. The cron runner (``deliver: telegram``) reads stdout."""
    stream: object = sys.stdout

    def notify(self, summary_md: str, *, title: Optional[str] = None) -> None:
        if title:
            print(f"## {title}", file=self.stream)
        print(summary_md, file=self.stream)


@dataclass
class RecordingNotifier:
    """In-memory recorder for tests."""
    calls: list[dict[str, Optional[str]]] = field(default_factory=list)

    def notify(self, summary_md: str, *, title: Optional[str] = None) -> None:
        self.calls.append({"title": title, "summary_md": summary_md})

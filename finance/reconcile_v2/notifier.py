"""Notification boundary for ``finalize_run``.

Default ``StdoutNotifier`` prints the summary to stdout; the cron entry
that schedules this skill uses ``deliver: telegram`` and reads stdout.
Tests substitute ``RecordingNotifier`` to assert dispatch.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Optional, Protocol


class Notifier(Protocol):
    def notify(self, summary_md: str, *, title: Optional[str] = None) -> None: ...


@dataclass
class StdoutNotifier:
    stream: object = sys.stdout

    def notify(self, summary_md: str, *, title: Optional[str] = None) -> None:
        if title:
            print(f"## {title}", file=self.stream)
        print(summary_md, file=self.stream)


@dataclass
class RecordingNotifier:
    calls: list[dict[str, Optional[str]]] = field(default_factory=list)

    def notify(self, summary_md: str, *, title: Optional[str] = None) -> None:
        self.calls.append({"title": title, "summary_md": summary_md})

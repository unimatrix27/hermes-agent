"""Graph mail sender for ``send_match``.

Defines a thin ``GraphMailSender`` Protocol so tests can substitute a fake
implementation that never touches the network. The live implementation
reuses ``DelegatedTokenProvider`` from the indexer (#21) for refresh-token
auth — the only credentials available on-host. App-only credentials
(``MSGRAPH_TENANT_ID`` / ``MSGRAPH_CLIENT_SECRET``) are not present in
``~/.hermes/.env``, so we deliberately use delegated auth here too.

Four-step pipeline lives in ``verbs.send_match``:

    a. POST sendMail with saveToSentItems=true and a fileAttachment.
    b. Poll Sent Items for the saved message; capture metadata.
    c. Insert bank.belege_sent (raw parameterized SQL).
    d. UPDATE receipt_matches.decision_status='sent'.

The first two steps are this module's responsibility. Steps c/d live
inside the verb so the partial-failure semantics (c-success + d-fail =
leave the row, return partial state) stay readable.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol


# ──────────────────────────────────────────────────────────────────────
# Result shapes
# ──────────────────────────────────────────────────────────────────────


@dataclass
class SentMessageMetadata:
    """Captured by step (b) — what landed in Sent Items."""
    outlook_message_id: str
    internet_message_id: Optional[str]
    sent_at: datetime
    subject: Optional[str]
    attachment_filenames: list[str]


@dataclass
class SendOutcome:
    """Result of step (a) + (b). Used by ``send_match`` to drive c/d."""
    ok: bool
    metadata: Optional[SentMessageMetadata]
    error: Optional[str] = None
    step: Optional[str] = None  # 'sendMail' | 'sent_items_lookup'


# ──────────────────────────────────────────────────────────────────────
# Protocol
# ──────────────────────────────────────────────────────────────────────


class GraphMailSender(Protocol):
    def send(
        self,
        *,
        from_mailbox: str,
        to_recipient: str,
        subject: str,
        body_text: str,
        attachment_path: Path,
        attachment_name: str,
    ) -> SendOutcome: ...


# ──────────────────────────────────────────────────────────────────────
# Fake (test) implementation
# ──────────────────────────────────────────────────────────────────────


@dataclass
class FakeGraphMailSender:
    """In-memory fake. Records every call and can be configured to fail
    at step (a) or step (b)."""

    fail_step_a: bool = False
    fail_step_b: bool = False
    error_text: str = "fake failure"
    sent_calls: list[dict[str, Any]] = None  # type: ignore[assignment]
    next_outlook_id: str = "AAMkAGFakeMessageId-00001"
    next_internet_id: str = "<fake-imid-00001@lineo.finance>"

    def __post_init__(self) -> None:
        if self.sent_calls is None:
            self.sent_calls = []

    def send(
        self,
        *,
        from_mailbox: str,
        to_recipient: str,
        subject: str,
        body_text: str,
        attachment_path: Path,
        attachment_name: str,
    ) -> SendOutcome:
        call = {
            "from_mailbox":    from_mailbox,
            "to_recipient":    to_recipient,
            "subject":         subject,
            "body_text":       body_text,
            "attachment_path": str(attachment_path),
            "attachment_name": attachment_name,
            "ts":              datetime.now(timezone.utc).isoformat(),
        }
        self.sent_calls.append(call)
        if self.fail_step_a:
            return SendOutcome(ok=False, metadata=None, error=self.error_text, step="sendMail")
        if self.fail_step_b:
            return SendOutcome(
                ok=False,
                metadata=None,
                error=self.error_text,
                step="sent_items_lookup",
            )
        meta = SentMessageMetadata(
            outlook_message_id=self.next_outlook_id,
            internet_message_id=self.next_internet_id,
            sent_at=datetime.now(timezone.utc),
            subject=subject,
            attachment_filenames=[attachment_name],
        )
        return SendOutcome(ok=True, metadata=meta)


# ──────────────────────────────────────────────────────────────────────
# Live implementation (delegated-auth Graph)
# ──────────────────────────────────────────────────────────────────────


class LiveGraphMailSender:
    """Real Graph sender. Uses ``DelegatedTokenProvider`` so it works
    against the on-host refresh-token bundle (no app-only credentials
    available)."""

    def __init__(
        self,
        *,
        token_provider: Any,  # DelegatedTokenProvider — duck-typed to avoid hard import
        sent_items_poll_seconds: float = 1.5,
        sent_items_max_poll: int = 8,
        sent_items_search_minutes: int = 5,
    ) -> None:
        self.token_provider = token_provider
        self.sent_items_poll_seconds = sent_items_poll_seconds
        self.sent_items_max_poll = sent_items_max_poll
        self.sent_items_search_minutes = sent_items_search_minutes

    # Allow either an async-style or sync-style token provider.
    def _token(self, force_refresh: bool = False) -> str:
        get_token = getattr(self.token_provider, "get_access_token", None)
        if get_token is None:
            raise RuntimeError("token_provider has no get_access_token()")
        result = get_token(force_refresh=force_refresh)
        if asyncio.iscoroutine(result):
            return asyncio.run(result)
        return result

    def _request(self, *, method: str, url: str, headers: dict[str, str], body: Optional[bytes] = None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, method=method, headers=headers, data=body)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.getcode(), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def send(
        self,
        *,
        from_mailbox: str,
        to_recipient: str,
        subject: str,
        body_text: str,
        attachment_path: Path,
        attachment_name: str,
    ) -> SendOutcome:
        # ── Step (a): POST sendMail ──
        try:
            pdf_bytes = attachment_path.read_bytes()
        except OSError as exc:
            return SendOutcome(ok=False, metadata=None, error=f"attachment read failed: {exc}", step="sendMail")

        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body_text},
                "toRecipients": [{"emailAddress": {"address": to_recipient}}],
                "attachments": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": attachment_name,
                        "contentType": "application/pdf",
                        "contentBytes": base64.b64encode(pdf_bytes).decode("ascii"),
                    }
                ],
            },
            "saveToSentItems": True,
        }
        body = json.dumps(payload).encode("utf-8")
        try:
            access = self._token()
        except Exception as exc:  # noqa: BLE001
            return SendOutcome(ok=False, metadata=None, error=f"token: {exc}", step="sendMail")

        send_url = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(from_mailbox)}/sendMail"
        headers = {
            "Authorization": f"Bearer {access}",
            "Content-Type":  "application/json",
        }
        send_started_at = datetime.now(timezone.utc)
        status, resp_body = self._request(method="POST", url=send_url, headers=headers, body=body)
        if status not in (200, 202):
            return SendOutcome(
                ok=False,
                metadata=None,
                error=f"sendMail returned {status}: {resp_body[:500]!r}",
                step="sendMail",
            )

        # ── Step (b): poll Sent Items for the saved copy ──
        # Graph saves the sent message asynchronously; subject + recipient is
        # the most reliable match key. We constrain to a recent window so we
        # don't pick up an older message with the same subject.
        since_iso = send_started_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        # OData query params with spaces must be URL-encoded for the
        # stdlib http client (it rejects raw spaces in selectors).
        params = urllib.parse.urlencode({
            "$top": 10,
            "$orderby": "sentDateTime desc",
            "$filter": f"sentDateTime ge {since_iso}",
            "$select": "id,internetMessageId,subject,sentDateTime,toRecipients,hasAttachments",
        }, quote_via=urllib.parse.quote)
        sent_url = (
            f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(from_mailbox)}"
            f"/mailFolders/sentitems/messages?{params}"
        )

        message_id: Optional[str] = None
        internet_id: Optional[str] = None
        sent_at: Optional[datetime] = None
        attempts = 0
        while attempts < self.sent_items_max_poll:
            attempts += 1
            try:
                access = self._token()
            except Exception as exc:  # noqa: BLE001
                return SendOutcome(
                    ok=False, metadata=None,
                    error=f"token refresh during sent-items poll: {exc}",
                    step="sent_items_lookup",
                )
            status, resp_body = self._request(
                method="GET", url=sent_url,
                headers={"Authorization": f"Bearer {access}"},
            )
            if status != 200:
                time.sleep(self.sent_items_poll_seconds)
                continue
            try:
                doc = json.loads(resp_body.decode("utf-8"))
            except Exception:  # noqa: BLE001
                time.sleep(self.sent_items_poll_seconds)
                continue
            for msg in doc.get("value", []):
                if (msg.get("subject") or "") != subject:
                    continue
                recipients = msg.get("toRecipients") or []
                addresses = [
                    (r.get("emailAddress") or {}).get("address", "").lower()
                    for r in recipients
                ]
                if to_recipient.lower() not in addresses:
                    continue
                message_id = msg.get("id")
                internet_id = msg.get("internetMessageId")
                ts = msg.get("sentDateTime")
                if ts:
                    try:
                        sent_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        sent_at = None
                break
            if message_id:
                break
            time.sleep(self.sent_items_poll_seconds)

        if not message_id:
            return SendOutcome(
                ok=False,
                metadata=None,
                error="sendMail accepted but no matching message found in Sent Items "
                      f"after {self.sent_items_max_poll} polls",
                step="sent_items_lookup",
            )

        meta = SentMessageMetadata(
            outlook_message_id=message_id,
            internet_message_id=internet_id,
            sent_at=sent_at or send_started_at,
            subject=subject,
            attachment_filenames=[attachment_name],
        )
        return SendOutcome(ok=True, metadata=meta)

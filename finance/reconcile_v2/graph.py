"""Microsoft Graph boundary for the v2 reconcile toolbox.

Two responsibilities:

* :class:`InboxClient` — narrow mail search keyed by vendor / amount /
  date-window. Returns mail bodies + PDF attachment text so the LLM can
  read them directly (per #31: the LLM is the parser, no vendor-specific
  regex library).
* :class:`MailSender` — forwards an attachment to DATEV. Step (a) POSTs
  sendMail, step (b) polls Sent Items for the saved copy. The v2 verb
  ``send_beleg`` writes the ``bank.belege_sent`` row from the returned
  metadata.

Both expose a thin Protocol so tests can use the in-memory fakes at the
bottom of the file. The live implementations reuse the existing
``DelegatedTokenProvider`` if present (PRs #1–#5) and otherwise fall
back to a minimal refresh-token-grant helper baked in below — this keeps
the v2 PR self-contained.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol


# ──────────────────────────────────────────────────────────────────────
# Result shapes
# ──────────────────────────────────────────────────────────────────────


@dataclass
class MailAttachment:
    """One mail attachment after we've downloaded + extracted it."""
    name: str
    content_type: Optional[str]
    size_bytes: Optional[int]
    sha256: Optional[str]
    local_path: Optional[str]
    extracted_text: Optional[str]
    extract_error: Optional[str] = None


@dataclass
class MailMessage:
    """A search hit. ``body_text`` is plain text (HTML stripped if needed)."""
    outlook_message_id: str
    internet_message_id: Optional[str]
    mailbox: str
    from_address: Optional[str]
    subject: Optional[str]
    received_at: Optional[datetime]
    body_text: Optional[str]
    has_attachments: bool
    attachments: list[MailAttachment] = field(default_factory=list)


@dataclass
class SentMessageMetadata:
    """Captured from Sent Items lookup after a sendMail succeeds."""
    outlook_message_id: str
    internet_message_id: Optional[str]
    sent_at: datetime
    subject: Optional[str]
    attachment_filenames: list[str]


@dataclass
class SendResult:
    """Result of one ``MailSender.send`` call."""
    ok: bool
    metadata: Optional[SentMessageMetadata] = None
    error: Optional[str] = None
    step: Optional[str] = None  # 'sendMail' | 'sent_items_lookup'


# ──────────────────────────────────────────────────────────────────────
# Protocols
# ──────────────────────────────────────────────────────────────────────


class InboxClient(Protocol):
    def search(
        self,
        *,
        mailbox: str,
        vendor: Optional[str],
        amount: Optional[float],
        date_window: Optional[tuple[date, date]],
        message_id: Optional[str] = None,
        max_results: int = 25,
    ) -> list[MailMessage]: ...


class MailSender(Protocol):
    def send(
        self,
        *,
        from_mailbox: str,
        to_recipient: str,
        subject: str,
        body_text: str,
        attachment_path: Path,
        attachment_name: str,
    ) -> SendResult: ...


# ──────────────────────────────────────────────────────────────────────
# Test fakes
# ──────────────────────────────────────────────────────────────────────


@dataclass
class FakeInboxClient:
    """In-memory inbox. ``messages_by_mailbox[mb] -> [MailMessage]``."""
    messages_by_mailbox: dict[str, list[MailMessage]] = field(default_factory=dict)
    search_calls: list[dict[str, Any]] = field(default_factory=list)

    def search(
        self,
        *,
        mailbox: str,
        vendor: Optional[str],
        amount: Optional[float],
        date_window: Optional[tuple[date, date]],
        message_id: Optional[str] = None,
        max_results: int = 25,
    ) -> list[MailMessage]:
        self.search_calls.append({
            "mailbox":      mailbox,
            "vendor":       vendor,
            "amount":       amount,
            "date_window":  date_window,
            "message_id":   message_id,
            "max_results":  max_results,
        })
        # Refuse zero-filter search — would scan the whole mailbox.
        if message_id is None and not vendor and amount is None and date_window is None:
            raise ValueError(
                "FakeInboxClient.search refuses zero-filter calls "
                "(must supply vendor, amount, date_window, or message_id)"
            )
        pool = self.messages_by_mailbox.get(mailbox, [])
        out: list[MailMessage] = []
        v = (vendor or "").lower()
        for msg in pool:
            if message_id is not None and msg.outlook_message_id != message_id:
                continue
            if v:
                hay = " ".join(filter(None, [
                    (msg.from_address or "").lower(),
                    (msg.subject or "").lower(),
                    (msg.body_text or "").lower(),
                ]))
                if v not in hay:
                    continue
            if date_window is not None and msg.received_at is not None:
                lo, hi = date_window
                if not (lo <= msg.received_at.date() <= hi):
                    continue
            if amount is not None:
                # Look for the amount string in body / any attachment text.
                amt_strs = _amount_candidates(amount)
                hay_amt = " ".join(filter(None, [
                    msg.body_text or "",
                    *[a.extracted_text or "" for a in msg.attachments],
                ]))
                if not any(s in hay_amt for s in amt_strs):
                    continue
            out.append(msg)
            if len(out) >= max_results:
                break
        return out


@dataclass
class FakeMailSender:
    """In-memory mail sender. Records calls; configurable failure modes."""
    fail_step_a: bool = False
    fail_step_b: bool = False
    error_text: str = "fake failure"
    sent: list[dict[str, Any]] = field(default_factory=list)
    next_outlook_id: str = "AAMkFake-00001"
    next_internet_id: str = "<fake-imid-00001@lineo.finance>"

    def send(
        self,
        *,
        from_mailbox: str,
        to_recipient: str,
        subject: str,
        body_text: str,
        attachment_path: Path,
        attachment_name: str,
    ) -> SendResult:
        self.sent.append({
            "from_mailbox":    from_mailbox,
            "to_recipient":    to_recipient,
            "subject":         subject,
            "body_text":       body_text,
            "attachment_path": str(attachment_path),
            "attachment_name": attachment_name,
            "ts":              datetime.now(timezone.utc).isoformat(),
        })
        if self.fail_step_a:
            return SendResult(ok=False, error=self.error_text, step="sendMail")
        if self.fail_step_b:
            return SendResult(ok=False, error=self.error_text, step="sent_items_lookup")
        meta = SentMessageMetadata(
            outlook_message_id=self.next_outlook_id,
            internet_message_id=self.next_internet_id,
            sent_at=datetime.now(timezone.utc),
            subject=subject,
            attachment_filenames=[attachment_name],
        )
        return SendResult(ok=True, metadata=meta)


# ──────────────────────────────────────────────────────────────────────
# Live token provider — minimal refresh-token grant
# ──────────────────────────────────────────────────────────────────────


DEFAULT_TOKEN_FILE = Path.home() / ".hermes" / "secrets" / "ms_graph_tokens.json"
DEFAULT_GRAPH_READ_SCOPE = (
    "User.Read Mail.Read Mail.Read.Shared offline_access"
)
DEFAULT_GRAPH_SEND_SCOPE = (
    "User.Read Mail.Send Mail.Send.Shared Mail.Read offline_access"
)


class TokenProvider:
    """Lightweight refresh-token grant. Caches the access token in
    memory and rewrites the bundle file with the rotated refresh token.

    A separate instance is required per scope: Microsoft narrows the
    access token to the requested scope set, so read-only operations
    and send operations need different tokens.
    """

    def __init__(
        self,
        *,
        scope: str,
        token_file: Path = DEFAULT_TOKEN_FILE,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        skew_seconds: int = 120,
    ) -> None:
        self.token_file = Path(token_file)
        self.tenant_id = tenant_id or os.environ.get("LINEO_MS_TENANT_ID") or ""
        self.client_id = client_id or os.environ.get("LINEO_MS_CLIENT_ID") or ""
        self.scope = scope
        self.skew_seconds = max(0, int(skew_seconds))
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        if (
            not force_refresh
            and self._access_token is not None
            and time.time() + self.skew_seconds < self._expires_at
        ):
            return self._access_token
        if not self.tenant_id or not self.client_id:
            raise RuntimeError(
                "TokenProvider needs LINEO_MS_TENANT_ID + LINEO_MS_CLIENT_ID "
                "(load via ~/.hermes/.env)."
            )
        bundle = json.loads(self.token_file.read_text())
        body = urllib.parse.urlencode({
            "client_id":     self.client_id,
            "grant_type":    "refresh_token",
            "refresh_token": bundle["refresh_token"],
            "scope":         self.scope,
        }).encode()
        url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
        if "access_token" not in payload:
            raise RuntimeError(f"refresh failed: {payload}")
        merged = {**bundle, **payload}
        self.token_file.write_text(json.dumps(merged, indent=2))
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._access_token


# ──────────────────────────────────────────────────────────────────────
# Live inbox client
# ──────────────────────────────────────────────────────────────────────


DEFAULT_DATEV_RECIPIENT = "36ec220d-733a-4c6e-a626-33cbcb408039@uploadmail.datev.de"
DEFAULT_SOURCE_MAILBOX  = "rechnung@lineo.finance"
DEFAULT_BLOB_ROOT       = Path.home() / ".hermes" / "finance_v2" / "blobs"


class LiveInboxClient:
    """Real Microsoft Graph inbox client.

    Builds a tight ``$search`` / ``$filter`` query from the (vendor,
    amount, date_window) tuple. The mailbox is never scanned in full —
    per #31 every call must supply at least one of the filters.
    """

    def __init__(
        self,
        *,
        token_provider: Optional[TokenProvider] = None,
        blob_root: Path = DEFAULT_BLOB_ROOT,
        request: Any = None,
    ) -> None:
        self.token_provider = token_provider or TokenProvider(
            scope=DEFAULT_GRAPH_READ_SCOPE,
        )
        self.blob_root = Path(blob_root)
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self._request = request or _http_request

    def search(
        self,
        *,
        mailbox: str,
        vendor: Optional[str],
        amount: Optional[float],
        date_window: Optional[tuple[date, date]],
        message_id: Optional[str] = None,
        max_results: int = 25,
    ) -> list[MailMessage]:
        if message_id is None and not vendor and amount is None and date_window is None:
            raise ValueError(
                "search requires at least one of: vendor, amount, "
                "date_window, message_id"
            )

        if message_id is not None:
            doc = self._fetch_message(mailbox, message_id)
            if doc is None:
                return []
            return [self._materialize(mailbox, doc)]

        # Build $filter for date_window; $search for vendor token.
        filter_clauses: list[str] = []
        if date_window is not None:
            lo, hi = date_window
            # receivedDateTime is a timestamp; include the whole hi-day.
            lo_iso = datetime.combine(lo, datetime.min.time(), tzinfo=timezone.utc).isoformat()
            hi_iso = datetime.combine(
                hi + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc,
            ).isoformat()
            filter_clauses.append(f"receivedDateTime ge {lo_iso}")
            filter_clauses.append(f"receivedDateTime lt {hi_iso}")
        params: dict[str, Any] = {
            "$top":     min(max(max_results, 1), 50),
            "$select":  "id,internetMessageId,from,subject,receivedDateTime,hasAttachments,bodyPreview",
        }
        if vendor:
            # $search and $filter cannot combine; we use $search and
            # post-filter on date in code. The vendor search hits
            # subject / body / from headers — quote for safety.
            params["$search"] = f'"{vendor}"'
        else:
            params["$filter"]  = " and ".join(filter_clauses) if filter_clauses else None
            params["$orderby"] = "receivedDateTime desc"

        params = {k: v for k, v in params.items() if v is not None}
        qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        url = (f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(mailbox)}"
               f"/messages?{qs}")
        headers = self._auth_headers()
        # $search requires ConsistencyLevel=eventual per Graph docs.
        if vendor:
            headers["ConsistencyLevel"] = "eventual"
        status, body = self._request(method="GET", url=url, headers=headers, body=None)
        if status != 200:
            raise RuntimeError(
                f"Graph messages query returned {status}: {body[:500]!r}"
            )
        doc = json.loads(body.decode("utf-8"))

        out: list[MailMessage] = []
        for raw in doc.get("value", []):
            # Post-filter by date when $search is in play.
            if vendor and date_window is not None:
                ts = raw.get("receivedDateTime")
                if ts:
                    try:
                        rdt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        rdt = None
                    if rdt is not None:
                        lo, hi = date_window
                        if not (lo <= rdt.date() <= hi):
                            continue
            msg_doc = self._fetch_message(mailbox, raw["id"]) or raw
            msg = self._materialize(mailbox, msg_doc)
            # Optional amount filter — match against body + attachment text.
            if amount is not None:
                hay = " ".join(filter(None, [
                    msg.body_text or "",
                    *(a.extracted_text or "" for a in msg.attachments),
                ]))
                if not any(s in hay for s in _amount_candidates(amount)):
                    continue
            out.append(msg)
            if len(out) >= max_results:
                break
        return out

    # ── helpers ──

    def _auth_headers(self) -> dict[str, str]:
        token = self.token_provider.get_access_token()
        return {"Authorization": f"Bearer {token}"}

    def _fetch_message(self, mailbox: str, message_id: str) -> Optional[dict[str, Any]]:
        url = (
            f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(mailbox)}"
            f"/messages/{urllib.parse.quote(message_id)}"
            "?$select=id,internetMessageId,from,subject,receivedDateTime,body,hasAttachments"
        )
        status, body = self._request(
            method="GET", url=url, headers=self._auth_headers(), body=None,
        )
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(
                f"Graph fetch {message_id} returned {status}: {body[:500]!r}"
            )
        return json.loads(body.decode("utf-8"))

    def _materialize(self, mailbox: str, doc: dict[str, Any]) -> MailMessage:
        received: Optional[datetime] = None
        ts = doc.get("receivedDateTime")
        if ts:
            try:
                received = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                received = None
        body_obj = doc.get("body") or {}
        content = body_obj.get("content") or doc.get("bodyPreview") or ""
        ctype = (body_obj.get("contentType") or "").lower()
        body_text = _html_to_text(content) if ctype == "html" else content
        from_obj = (doc.get("from") or {}).get("emailAddress") or {}
        attachments: list[MailAttachment] = []
        if doc.get("hasAttachments"):
            try:
                attachments = self._fetch_attachments(mailbox, doc["id"])
            except Exception as exc:  # noqa: BLE001
                attachments = [
                    MailAttachment(
                        name="<error>", content_type=None, size_bytes=None,
                        sha256=None, local_path=None, extracted_text=None,
                        extract_error=f"attachment fetch failed: {exc}",
                    )
                ]
        return MailMessage(
            outlook_message_id=doc["id"],
            internet_message_id=doc.get("internetMessageId"),
            mailbox=mailbox,
            from_address=from_obj.get("address"),
            subject=doc.get("subject"),
            received_at=received,
            body_text=body_text or None,
            has_attachments=bool(doc.get("hasAttachments")),
            attachments=attachments,
        )

    def _fetch_attachments(
        self, mailbox: str, message_id: str,
    ) -> list[MailAttachment]:
        url = (
            f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(mailbox)}"
            f"/messages/{urllib.parse.quote(message_id)}/attachments"
        )
        status, body = self._request(
            method="GET", url=url, headers=self._auth_headers(), body=None,
        )
        if status != 200:
            raise RuntimeError(
                f"Graph attachments returned {status}: {body[:500]!r}"
            )
        doc = json.loads(body.decode("utf-8"))
        out: list[MailAttachment] = []
        for att in doc.get("value", []):
            name = att.get("name") or "attachment"
            ctype = att.get("contentType")
            size = att.get("size")
            content_b64 = att.get("contentBytes")
            if not content_b64:
                out.append(MailAttachment(
                    name=name, content_type=ctype, size_bytes=size,
                    sha256=None, local_path=None, extracted_text=None,
                    extract_error="contentBytes missing — not a fileAttachment?",
                ))
                continue
            try:
                blob = base64.b64decode(content_b64)
            except Exception as exc:  # noqa: BLE001
                out.append(MailAttachment(
                    name=name, content_type=ctype, size_bytes=size,
                    sha256=None, local_path=None, extracted_text=None,
                    extract_error=f"b64 decode failed: {exc}",
                ))
                continue
            sha = _sha256_hex(blob)
            local = self.blob_root / f"{sha}.bin"
            try:
                if not local.exists():
                    local.write_bytes(blob)
            except OSError as exc:
                out.append(MailAttachment(
                    name=name, content_type=ctype, size_bytes=size,
                    sha256=sha, local_path=None, extracted_text=None,
                    extract_error=f"local write failed: {exc}",
                ))
                continue
            text, err = _extract_text(local, ctype)
            out.append(MailAttachment(
                name=name, content_type=ctype, size_bytes=size,
                sha256=sha, local_path=str(local),
                extracted_text=text, extract_error=err,
            ))
        return out


# ──────────────────────────────────────────────────────────────────────
# Live mail sender
# ──────────────────────────────────────────────────────────────────────


class LiveMailSender:
    """Real Graph mail sender. Steps:

      a. POST sendMail(saveToSentItems=true) with the PDF as a
         fileAttachment.
      b. Poll Sent Items for the saved copy; capture metadata.

    Step (c) — INSERT bank.belege_sent — lives in
    :func:`finance.reconcile_v2.verbs.send_beleg` so partial-failure
    semantics stay readable.
    """

    def __init__(
        self,
        *,
        token_provider: Optional[TokenProvider] = None,
        sent_items_poll_seconds: float = 1.5,
        sent_items_max_poll: int = 8,
        request: Any = None,
    ) -> None:
        self.token_provider = token_provider or TokenProvider(
            scope=DEFAULT_GRAPH_SEND_SCOPE,
        )
        self.sent_items_poll_seconds = sent_items_poll_seconds
        self.sent_items_max_poll = sent_items_max_poll
        self._request = request or _http_request

    def send(
        self,
        *,
        from_mailbox: str,
        to_recipient: str,
        subject: str,
        body_text: str,
        attachment_path: Path,
        attachment_name: str,
    ) -> SendResult:
        try:
            pdf_bytes = attachment_path.read_bytes()
        except OSError as exc:
            return SendResult(ok=False,
                              error=f"attachment read failed: {exc}",
                              step="sendMail")

        payload = {
            "message": {
                "subject": subject,
                "body":    {"contentType": "Text", "content": body_text},
                "toRecipients": [{"emailAddress": {"address": to_recipient}}],
                "attachments": [
                    {
                        "@odata.type":  "#microsoft.graph.fileAttachment",
                        "name":         attachment_name,
                        "contentType":  "application/pdf",
                        "contentBytes": base64.b64encode(pdf_bytes).decode("ascii"),
                    }
                ],
            },
            "saveToSentItems": True,
        }
        body = json.dumps(payload).encode("utf-8")
        try:
            access = self.token_provider.get_access_token()
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, error=f"token: {exc}", step="sendMail")

        send_url = (
            f"https://graph.microsoft.com/v1.0/users/"
            f"{urllib.parse.quote(from_mailbox)}/sendMail"
        )
        headers = {
            "Authorization": f"Bearer {access}",
            "Content-Type":  "application/json",
        }
        send_started_at = datetime.now(timezone.utc)
        status, resp = self._request(
            method="POST", url=send_url, headers=headers, body=body,
        )
        if status not in (200, 202):
            return SendResult(
                ok=False,
                error=f"sendMail returned {status}: {resp[:500]!r}",
                step="sendMail",
            )

        # Step (b): Sent Items lookup.
        since_iso = (
            send_started_at.replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        params = urllib.parse.urlencode({
            "$top":     10,
            "$orderby": "sentDateTime desc",
            "$filter":  f"sentDateTime ge {since_iso}",
            "$select":  "id,internetMessageId,subject,sentDateTime,toRecipients,hasAttachments",
        }, quote_via=urllib.parse.quote)
        sent_url = (
            f"https://graph.microsoft.com/v1.0/users/"
            f"{urllib.parse.quote(from_mailbox)}"
            f"/mailFolders/sentitems/messages?{params}"
        )

        attempts = 0
        while attempts < self.sent_items_max_poll:
            attempts += 1
            try:
                access = self.token_provider.get_access_token()
            except Exception as exc:  # noqa: BLE001
                return SendResult(
                    ok=False,
                    error=f"token refresh during sent-items poll: {exc}",
                    step="sent_items_lookup",
                )
            status, resp = self._request(
                method="GET", url=sent_url,
                headers={"Authorization": f"Bearer {access}"}, body=None,
            )
            if status != 200:
                time.sleep(self.sent_items_poll_seconds)
                continue
            try:
                doc = json.loads(resp.decode("utf-8"))
            except Exception:  # noqa: BLE001
                time.sleep(self.sent_items_poll_seconds)
                continue
            for msg in doc.get("value", []):
                if (msg.get("subject") or "") != subject:
                    continue
                recipients = msg.get("toRecipients") or []
                addrs = [
                    (r.get("emailAddress") or {}).get("address", "").lower()
                    for r in recipients
                ]
                if to_recipient.lower() not in addrs:
                    continue
                sent_at = msg.get("sentDateTime")
                try:
                    sent_at_dt = (
                        datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
                        if sent_at else send_started_at
                    )
                except ValueError:
                    sent_at_dt = send_started_at
                meta = SentMessageMetadata(
                    outlook_message_id=msg["id"],
                    internet_message_id=msg.get("internetMessageId"),
                    sent_at=sent_at_dt,
                    subject=msg.get("subject"),
                    attachment_filenames=[attachment_name],
                )
                return SendResult(ok=True, metadata=meta)
            time.sleep(self.sent_items_poll_seconds)

        return SendResult(
            ok=False,
            error=(
                "sendMail accepted but no matching message found in Sent Items "
                f"after {self.sent_items_max_poll} polls"
            ),
            step="sent_items_lookup",
        )


# ──────────────────────────────────────────────────────────────────────
# Helpers — text / amount / http
# ──────────────────────────────────────────────────────────────────────


def _http_request(
    *, method: str, url: str, headers: dict[str, str], body: Optional[bytes],
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _sha256_hex(blob: bytes) -> str:
    import hashlib
    return hashlib.sha256(blob).hexdigest()


def _amount_candidates(amount: float) -> list[str]:
    """Cheap fuzzy match for an amount string in body / PDF text.

    Returns the same number in a few common European formats so we can
    grep without forcing the LLM to think about commas vs dots.
    """
    a = abs(float(amount))
    dot = f"{a:.2f}"           # 12.34
    de  = dot.replace(".", ",")  # 12,34
    int_part, dec_part = dot.split(".")
    # Thousand-separated variants (1.234,56 / 1,234.56)
    if len(int_part) > 3:
        de_t  = _group_thousands(int_part, ".") + "," + dec_part
        us_t  = _group_thousands(int_part, ",") + "." + dec_part
        return [dot, de, de_t, us_t]
    return [dot, de]


def _group_thousands(int_str: str, sep: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(reversed(int_str)):
        if i and i % 3 == 0:
            out.append(sep)
        out.append(ch)
    return "".join(reversed(out))


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&#\d+;|&[a-zA-Z]+;")
_HTML_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&quot;": '"',
    "&lt;": "<", "&gt;": ">", "&apos;": "'",
}


def _html_to_text(html: str) -> str:
    txt = _HTML_TAG_RE.sub("\n", html or "")
    for ent, rep in _HTML_ENTITIES.items():
        txt = txt.replace(ent, rep)
    txt = _HTML_ENTITY_RE.sub("", txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n[ \t]*", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _extract_text(
    path: Path, content_type: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """PDF → text via pymupdf; HTML → text; plain text → as-is."""
    ct = (content_type or "").lower()
    suffix = path.suffix.lower()
    is_pdf = ct == "application/pdf" or suffix == ".pdf"
    is_html = "html" in ct or suffix in (".htm", ".html")
    is_text = ct.startswith("text/") or suffix in (".txt", ".csv")

    if is_pdf:
        try:
            import pymupdf  # type: ignore[import]
        except ImportError as exc:  # pragma: no cover - env guard
            return None, f"pymupdf not installed: {exc}"
        try:
            doc = pymupdf.open(str(path))
        except Exception as exc:  # noqa: BLE001
            return None, f"pymupdf open failed: {exc}"
        try:
            text = "\n".join(page.get_text() for page in doc).strip()
        finally:
            doc.close()
        return (text or None), None

    if is_html:
        try:
            return _html_to_text(path.read_text(encoding="utf-8", errors="replace")), None
        except OSError as exc:
            return None, f"html read failed: {exc}"

    if is_text:
        try:
            return path.read_text(encoding="utf-8", errors="replace"), None
        except OSError as exc:
            return None, f"text read failed: {exc}"

    return None, f"unsupported attachment content_type={content_type or '<missing>'}"

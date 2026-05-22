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
bottom of the file.

Auth: we reuse ``tools.microsoft_graph_auth.MicrosoftGraphTokenProvider``
(app-only ``client_credentials``) when ``MSGRAPH_*`` credentials are
present — that's the upstream-supported path. When they aren't (which
is the case on the Lineo finance host: only ``LINEO_MS_TENANT_ID`` /
``LINEO_MS_CLIENT_ID`` plus an on-disk refresh-token bundle at
``~/.hermes/lineo-ms-tokens/sebastian.json``), we fall back to a
delegated-refresh-token provider that quacks the same async
``get_access_token`` interface. No new credential files are written; we
read the existing bundle that the operator already maintains.
"""
from __future__ import annotations

import asyncio
import base64
from email import policy
from email.parser import BytesParser
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from tools.microsoft_graph_auth import (
    MicrosoftGraphAuthError,
    MicrosoftGraphConfigError,
    MicrosoftGraphTokenProvider,
)


LOGGER = logging.getLogger("finance.reconcile_v2.graph")


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
# Sync wrapper around the async tools/microsoft_graph_auth provider.
# ──────────────────────────────────────────────────────────────────────


def _sync_get_access_token(
    provider: Any, *, force_refresh: bool = False
) -> str:
    """Call ``get_access_token`` from sync code.

    Accepts any provider that exposes ``get_access_token(*, force_refresh)``.
    The shared :class:`MicrosoftGraphTokenProvider` is async; the
    delegated fallback is async too. We bridge with ``asyncio.run`` —
    each provider caches in memory, so subsequent calls are cheap.
    """
    return asyncio.run(provider.get_access_token(force_refresh=force_refresh))


# ──────────────────────────────────────────────────────────────────────
# Delegated-refresh-token fallback
#
# When MSGRAPH_CLIENT_SECRET is not provisioned (the operator's host
# only has LINEO_MS_TENANT_ID / LINEO_MS_CLIENT_ID plus an on-disk
# refresh-token bundle), MicrosoftGraphTokenProvider.from_env() refuses
# to construct. This provider drops into the same async
# get_access_token() interface using the refresh-token grant against
# the existing token bundle file — no new credentials, no new file.
# ──────────────────────────────────────────────────────────────────────


DELEGATED_TOKEN_FILE = (
    Path.home() / ".hermes" / "lineo-ms-tokens" / "sebastian.json"
)
DELEGATED_TOKEN_FILE_CATRIN = (
    Path.home() / ".hermes" / "lineo-ms-tokens" / "catrin.json"
)
DELEGATED_SCOPE = (
    "User.Read Mail.Read Mail.Read.Shared Mail.Send Mail.Send.Shared "
    "offline_access"
)


@dataclass
class _DelegatedRefreshTokenProvider:
    """Async-compatible refresh-token-grant provider.

    Quacks like :class:`MicrosoftGraphTokenProvider`:
    ``async get_access_token(*, force_refresh: bool = False) -> str``.

    Persists the rotated refresh token back to ``token_file`` so the
    bundle stays current across runs.
    """

    tenant_id: str
    client_id: str
    token_file: Path = DELEGATED_TOKEN_FILE
    scope: str = DELEGATED_SCOPE
    skew_seconds: int = 120
    _access_token: Optional[str] = field(default=None, init=False, repr=False)
    _expires_at: float = field(default=0.0, init=False, repr=False)

    @classmethod
    def from_env(
        cls,
        environ: Optional[dict[str, str]] = None,
        *,
        token_file: Optional[Path] = None,
        scope: Optional[str] = None,
    ) -> "_DelegatedRefreshTokenProvider":
        env = environ if environ is not None else os.environ
        # Resolve module-level defaults lazily so tests (and ops) can
        # monkeypatch them after import.
        resolved_token_file = Path(
            token_file if token_file is not None else DELEGATED_TOKEN_FILE
        )
        resolved_scope = scope if scope is not None else DELEGATED_SCOPE
        tenant = (env.get("LINEO_MS_TENANT_ID") or "").strip()
        client = (env.get("LINEO_MS_CLIENT_ID") or "").strip()
        if not tenant or not client:
            raise MicrosoftGraphConfigError(
                "Delegated-token fallback needs LINEO_MS_TENANT_ID + "
                "LINEO_MS_CLIENT_ID in the environment "
                "(load via ~/.hermes/.env)."
            )
        if not resolved_token_file.exists():
            raise MicrosoftGraphConfigError(
                f"Delegated-token bundle not found at {resolved_token_file}. "
                "Refresh it via the operator's delegated-auth workflow "
                "(see finance/scripts/build_fixtures.py for the device-code "
                "bootstrap) — this module does NOT create new bundles."
            )
        return cls(
            tenant_id=tenant,
            client_id=client,
            token_file=resolved_token_file,
            scope=resolved_scope,
        )

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        if (
            not force_refresh
            and self._access_token is not None
            and time.time() + self.skew_seconds < self._expires_at
        ):
            return self._access_token
        # Run the blocking refresh on a thread so we don't block the loop.
        return await asyncio.to_thread(self._refresh_blocking)

    def _refresh_blocking(self) -> str:
        bundle = json.loads(self.token_file.read_text())
        if "refresh_token" not in bundle:
            raise MicrosoftGraphAuthError(
                f"Token bundle at {self.token_file} has no refresh_token; "
                "operator must re-bootstrap."
            )
        body = urllib.parse.urlencode({
            "client_id":     self.client_id,
            "grant_type":    "refresh_token",
            "refresh_token": bundle["refresh_token"],
            "scope":         self.scope,
        }).encode()
        url = (
            f"https://login.microsoftonline.com/{self.tenant_id}"
            "/oauth2/v2.0/token"
        )
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise MicrosoftGraphAuthError(
                f"delegated refresh failed (HTTP {exc.code}): {detail}"
            ) from exc
        if "access_token" not in payload:
            raise MicrosoftGraphAuthError(
                f"delegated refresh response missing access_token: {payload}"
            )
        # Rotate the refresh token in the bundle (Azure may issue a new one).
        merged = {**bundle, **payload}
        try:
            self.token_file.write_text(json.dumps(merged, indent=2))
            os.chmod(self.token_file, 0o600)
        except OSError:
            # Persistence failure is non-fatal — in-memory token still
            # works for this run; next run will refresh again.
            LOGGER.warning(
                "could not persist rotated token bundle to %s "
                "(continuing with in-memory token)",
                self.token_file,
            )
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600))
        assert self._access_token is not None
        return self._access_token


def _delegated_token_file_for_mailbox(
    mailbox: Optional[str],
    environ: Optional[dict[str, str]] = None,
) -> Path:
    """Return the delegated token bundle that should read ``mailbox``.

    The Lineo setup uses one delegated token per personal mailbox.  The shared
    receipt mailbox remains read/sent through Sebastian's delegated token, but
    Catrin's personal mailbox must use Catrin's own token; otherwise Graph
    returns 403 even when ``Mail.Read`` is granted for the signed-in user.
    """
    env = environ if environ is not None else os.environ
    box = (mailbox or "").strip().lower()
    catrin_box = (
        env.get("LINEO_MAILBOX_CATRIN") or "catrin.stuecker@lineo.finance"
    ).strip().lower()
    if box and (box == catrin_box or box.startswith("catrin.")):
        return Path(
            env.get("LINEO_MS_TOKEN_FILE_CATRIN")
            or env.get("LINEO_MS_CATRIN_TOKEN_FILE")
            or DELEGATED_TOKEN_FILE_CATRIN
        )
    return Path(
        env.get("LINEO_MS_TOKEN_FILE_SEBASTIAN")
        or env.get("LINEO_MS_SEBASTIAN_TOKEN_FILE")
        or DELEGATED_TOKEN_FILE
    )


def _is_personal_delegated_mailbox(
    mailbox: Optional[str],
    environ: Optional[dict[str, str]] = None,
) -> bool:
    env = environ if environ is not None else os.environ
    box = (mailbox or "").strip().lower()
    if not box:
        return False
    personal = {
        (env.get("LINEO_MAILBOX_CATRIN") or "catrin.stuecker@lineo.finance").strip().lower(),
        (env.get("LINEO_MAILBOX_SEBASTIAN") or "sebastian.stuecker@lineo.finance").strip().lower(),
    }
    return box in personal or box.startswith("catrin.") or box.startswith("sebastian.")


def _default_token_provider(mailbox: Optional[str] = None) -> Any:
    """Pick the working Graph token provider for this host.

    Preference order:
      1. ``MicrosoftGraphTokenProvider.from_env()`` (app-only
         ``client_credentials``) — the upstream-supported path. Requires
         ``MSGRAPH_TENANT_ID`` / ``MSGRAPH_CLIENT_ID`` /
         ``MSGRAPH_CLIENT_SECRET``.
      2. :class:`_DelegatedRefreshTokenProvider` keyed off
         ``LINEO_MS_TENANT_ID`` / ``LINEO_MS_CLIENT_ID`` and the on-disk
         per-mailbox token bundle under ``~/.hermes/lineo-ms-tokens`` —
         the Lineo finance host's working path.

    Raises :class:`MicrosoftGraphConfigError` if neither set is
    available, so the caller sees a clean configuration error instead
    of a confusing 401 later.
    """
    delegated_token_file = _delegated_token_file_for_mailbox(mailbox)
    # Personal mailboxes need their own delegated token.  App-only creds may be
    # configured for other Graph paths but still lack access to Catrin's mailbox,
    # so don't let app-only shadow an explicitly available personal bundle.
    if mailbox is not None and delegated_token_file != Path(DELEGATED_TOKEN_FILE):
        try:
            return _DelegatedRefreshTokenProvider.from_env(
                token_file=delegated_token_file
            )
        except MicrosoftGraphConfigError:
            # Fall back to app-only below if the per-user bundle is absent/bad.
            pass

    try:
        return MicrosoftGraphTokenProvider.from_env()
    except MicrosoftGraphConfigError as app_only_exc:
        try:
            return _DelegatedRefreshTokenProvider.from_env(
                token_file=delegated_token_file
            )
        except MicrosoftGraphConfigError as delegated_exc:
            raise MicrosoftGraphConfigError(
                "No Microsoft Graph credentials available. "
                f"App-only: {app_only_exc}. "
                f"Delegated fallback: {delegated_exc}"
            ) from delegated_exc


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
        token_provider: Any = None,
        blob_root: Path = DEFAULT_BLOB_ROOT,
        request: Any = None,
    ) -> None:
        self.token_provider = token_provider
        self._token_providers_by_mailbox: dict[str, Any] = {}
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
        url = (f"https://graph.microsoft.com/v1.0/{self._mailbox_root(mailbox)}"
               f"/messages?{qs}")
        headers = self._auth_headers(mailbox)
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

    def _token_provider_for_mailbox(self, mailbox: Optional[str]) -> Any:
        if self.token_provider is not None:
            return self.token_provider
        key = (mailbox or "").strip().lower()
        if key not in self._token_providers_by_mailbox:
            self._token_providers_by_mailbox[key] = _default_token_provider(mailbox)
        return self._token_providers_by_mailbox[key]

    def _auth_headers(self, mailbox: Optional[str] = None) -> dict[str, str]:
        token = _sync_get_access_token(self._token_provider_for_mailbox(mailbox))
        return {"Authorization": f"Bearer {token}"}

    def _mailbox_root(self, mailbox: str) -> str:
        provider = self._token_provider_for_mailbox(mailbox)
        if isinstance(provider, _DelegatedRefreshTokenProvider) and _is_personal_delegated_mailbox(mailbox):
            return "me"
        return f"users/{urllib.parse.quote(mailbox)}"

    def _fetch_message(self, mailbox: str, message_id: str) -> Optional[dict[str, Any]]:
        url = (
            f"https://graph.microsoft.com/v1.0/{self._mailbox_root(mailbox)}"
            f"/messages/{urllib.parse.quote(message_id)}"
            "?$select=id,internetMessageId,from,subject,receivedDateTime,body,hasAttachments"
        )
        status, body = self._request(
            method="GET", url=url, headers=self._auth_headers(mailbox), body=None,
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
            f"https://graph.microsoft.com/v1.0/{self._mailbox_root(mailbox)}"
            f"/messages/{urllib.parse.quote(message_id)}/attachments"
        )
        status, body = self._request(
            method="GET", url=url, headers=self._auth_headers(mailbox), body=None,
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
            out.extend(self._materialize_attachment_blob(
                name=name, content_type=ctype, size_bytes=size, blob=blob,
            ))
        return out

    def _materialize_attachment_blob(
        self,
        *,
        name: str,
        content_type: Optional[str],
        size_bytes: Optional[int],
        blob: bytes,
    ) -> list[MailAttachment]:
        """Persist an attachment, flattening signed-MIME wrappers.

        DATEV e-invoice mails can arrive from Graph as a single top-level
        ``smime.p7m`` / ``multipart/signed`` attachment. That object is itself
        a MIME document: the real invoice PDF is nested below a
        ``multipart/mixed`` part, followed by an ``smime.p7s`` signature. Treat
        such wrappers as containers so search/matching and ``send_beleg`` see
        the actual PDF attachment instead of an unsupported signature envelope.
        """
        nested = _extract_nested_file_attachments(
            blob, parent_name=name, parent_content_type=content_type,
        )
        if nested:
            return [
                self._persist_attachment(
                    name=n_name,
                    content_type=n_ctype,
                    size_bytes=len(n_blob),
                    blob=n_blob,
                )
                for n_name, n_ctype, n_blob in nested
            ]

        return [self._persist_attachment(
            name=name, content_type=content_type, size_bytes=size_bytes, blob=blob,
        )]

    def _persist_attachment(
        self,
        *,
        name: str,
        content_type: Optional[str],
        size_bytes: Optional[int],
        blob: bytes,
    ) -> MailAttachment:
        sha = _sha256_hex(blob)
        local = self.blob_root / f"{sha}.bin"
        try:
            if not local.exists():
                local.write_bytes(blob)
        except OSError as exc:
            return MailAttachment(
                name=name, content_type=content_type, size_bytes=size_bytes,
                sha256=sha, local_path=None, extracted_text=None,
                extract_error=f"local write failed: {exc}",
            )
        text, err = _extract_text(local, content_type)
        return MailAttachment(
            name=name, content_type=content_type, size_bytes=size_bytes,
            sha256=sha, local_path=str(local),
            extracted_text=text, extract_error=err,
        )


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
        token_provider: Any = None,
        sent_items_poll_seconds: float = 1.5,
        sent_items_max_poll: int = 8,
        request: Any = None,
    ) -> None:
        self.token_provider = token_provider or _default_token_provider()
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
            access = _sync_get_access_token(self.token_provider)
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
                access = _sync_get_access_token(self.token_provider)
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


def _extract_nested_file_attachments(
    blob: bytes,
    *,
    parent_name: str,
    parent_content_type: Optional[str],
) -> list[tuple[str, Optional[str], bytes]]:
    """Return real file attachments hidden inside MIME container attachments.

    Microsoft Graph sometimes exposes signed e-invoice messages as one
    ``smime.p7m`` fileAttachment whose bytes are a ``multipart/signed`` MIME
    document. The invoice PDF is a normal nested MIME attachment inside that
    document. If the top-level blob is not such a container, return ``[]`` so
    callers preserve the original attachment behavior.
    """
    ct = (parent_content_type or "").lower()
    name = (parent_name or "").lower()
    looks_like_mime_container = (
        ct.startswith("multipart/")
        or ct == "message/rfc822"
        or name.endswith((".p7m", ".eml"))
        or blob.lstrip().lower().startswith(b"content-type: multipart/")
    )
    if not looks_like_mime_container:
        return []

    try:
        msg = BytesParser(policy=policy.default).parsebytes(blob)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("could not parse nested MIME attachment %s: %s", parent_name, exc)
        return []
    if not msg.is_multipart():
        return []

    out: list[tuple[str, Optional[str], bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        content_disposition = (part.get_content_disposition() or "").lower()
        part_ct = part.get_content_type()
        if not filename and content_disposition != "attachment":
            continue
        if part_ct in {"application/pkcs7-signature", "application/x-pkcs7-signature"}:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        out.append((filename or "attachment", part_ct, payload))
    return out


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

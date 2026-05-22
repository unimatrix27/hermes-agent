"""Finance reconciliation receipt indexer (unimatrix27/ideas#21).

Pulls /messages/delta from one or more Microsoft Graph mailboxes, downloads
PDF attachments once (SHA-256 dedupe), extracts text once (pymupdf, with
marker-pdf as an opt-in fallback), and lands a row per attachment in
``bank.receipt_candidates``. Notification-only mails from a configured
portal-required allowlist land with ``parse_status='portal_required'``.

Two entry points share one code path:

* **CLI** for cron:
  ``python -m finance.indexer [--mailbox X] [--since YYYY-MM-DD] [--limit N]``

* **Library** for #23's `run_indexer()` wrapper:
  ``finance.indexer.run(...) -> RunSummary``

The indexer never writes to ``bank.transactions``, ``bank.belege_sent``, or
``bank.belege_to_send`` (read-only against them for dedupe). It does not call
the matcher (#22) — that's #23's job — and never invokes an LLM.

See unimatrix27/ideas#21 for the spec and unimatrix27/ideas#27 for the
parent architecture (Mode-B reconcile agent).
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
from email import policy
from email.parser import BytesParser
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

# ──────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────

DEFAULT_MAILBOXES: tuple[str, ...] = (
    "rechnung@lineo.finance",
    "marketing@lineo.finance",
)

# Folders to walk per mailbox. Graph rejects ``/users/{mb}/messages/delta``
# with "Change tracking is not supported against 'microsoft.graph.message'"
# — delta is folder-scoped only. ``inbox`` is a Graph well-known name that
# resolves regardless of mailbox locale (Posteingang vs Inbox). Operators
# can extend this with custom display names or well-known IDs via the
# ``IndexerConfig.folders`` field.
DEFAULT_FOLDERS: tuple[str, ...] = ("inbox",)

# Senders whose mail is treated as a portal-required notification when the
# message has no PDF attachment. Match is on the sender's address suffix.
# Per #24: the real Vodafone notification ships from
# nicht.antworten@kundenservice.vodafone.com.
DEFAULT_PORTAL_REQUIRED_SENDERS: tuple[str, ...] = (
    "kundenservice.vodafone.com",
    "vodafone.com",
    "vodafone.de",
)

DEFAULT_BACKFILL_MONTHS = 6
DEFAULT_BLOB_ROOT = Path.home() / ".hermes" / "finance" / "blobs"
DEFAULT_TOKEN_FILE = Path.home() / ".hermes" / "lineo-ms-tokens" / "sebastian.json"
DEFAULT_ENV_FILE = Path.home() / ".hermes" / ".env"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "User.Read Mail.Read Mail.Read.Shared offline_access"

# pymupdf "~no content" gate. Below this length we attempt the marker-pdf
# fallback (opt-in via env flag).
PYMUPDF_MIN_CHARS = 50

# Threshold for treating an existing message as "already indexed elsewhere"
# during delta-token recovery. Driven by internet_message_id which is stable
# across Outlook forwards.

# ──────────────────────────────────────────────────────────────────────────
# Domain objects
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class IndexerState:
    mailbox: str
    delta_token: Optional[str]
    last_run_at: Optional[datetime] = None
    last_summary: Optional[Mapping[str, Any]] = None
    last_error: Optional[str] = None


@dataclass
class RunSummary:
    """Per-mailbox counters. Stored verbatim in bank.indexer_state.last_summary."""
    mailbox: str = ""
    scanned: int = 0           # messages walked from delta
    new: int = 0               # candidates inserted (any parse_status)
    dedup_skipped: int = 0     # attachments skipped because SHA already present
    portal_required: int = 0   # candidates with parse_status='portal_required'
    parse_failed: int = 0      # candidates with parse_status='failed'
    delta_reset: bool = False
    delta_reset_reason: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        return d


@dataclass
class AggregateSummary:
    """Roll-up across mailboxes when the CLI runs multiple."""
    mailboxes: list[RunSummary] = field(default_factory=list)

    @property
    def scanned(self) -> int:
        return sum(m.scanned for m in self.mailboxes)

    @property
    def new(self) -> int:
        return sum(m.new for m in self.mailboxes)

    @property
    def dedup_skipped(self) -> int:
        return sum(m.dedup_skipped for m in self.mailboxes)

    @property
    def portal_required(self) -> int:
        return sum(m.portal_required for m in self.mailboxes)

    @property
    def parse_failed(self) -> int:
        return sum(m.parse_failed for m in self.mailboxes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "new": self.new,
            "dedup_skipped": self.dedup_skipped,
            "portal_required": self.portal_required,
            "parse_failed": self.parse_failed,
            "per_mailbox": [m.to_dict() for m in self.mailboxes],
        }


@dataclass
class AttachmentBlob:
    """Where a downloaded PDF lives + its SHA-256."""
    sha256: str
    local_path: Path
    size_bytes: int
    content_type: Optional[str]


# ──────────────────────────────────────────────────────────────────────────
# Adapter protocol — the persistence boundary, mockable in tests
# (mirrors finance/matcher.py's MatcherAdapter pattern)
# ──────────────────────────────────────────────────────────────────────────


class IndexerAdapter(Protocol):
    """Reads/writes to bank.indexer_state and bank.receipt_candidates,
    plus read-only dedupe lookups against bank.belege_sent.
    """

    def get_state(self, mailbox: str) -> Optional[IndexerState]: ...

    def upsert_state(
        self,
        mailbox: str,
        *,
        delta_token: Optional[str],
        last_run_at: datetime,
        last_summary: Mapping[str, Any],
        last_error: Optional[str] = None,
    ) -> None: ...

    def candidate_by_sha256(self, sha256: str) -> Optional[int]: ...

    def candidate_exists_for_message(
        self,
        *,
        mailbox: str,
        internet_message_id: Optional[str],
        outlook_message_id: Optional[str],
    ) -> bool: ...

    def belege_sent_has_message(
        self,
        *,
        internet_message_id: Optional[str],
        outlook_message_id: Optional[str],
    ) -> bool: ...

    def insert_candidate(
        self,
        *,
        source_system: str,
        mailbox: str,
        outlook_message_id: Optional[str],
        internet_message_id: Optional[str],
        received_at: Optional[datetime],
        from_email: Optional[str],
        subject: Optional[str],
        attachment_name: Optional[str],
        attachment_sha256: Optional[str],
        local_blob_path: Optional[str],
        text_sha256: Optional[str],
        extracted_text: Optional[str],
        extracted_json: Mapping[str, Any],
        parse_status: str,
        parse_error: Optional[str],
    ) -> int: ...


# ──────────────────────────────────────────────────────────────────────────
# Graph client — uses tools/microsoft_graph_client.py for pagination /
# streaming / retry/backoff. Auth is delegated (refresh-token grant) to
# match the operator's token bundle; this mirrors PR #2's documented gap
# between the upstream app-only client and the only credentials available
# on-host.
# ──────────────────────────────────────────────────────────────────────────


class DelegatedTokenProvider:
    """Refresh-token-grant cache that quacks like ``MicrosoftGraphTokenProvider``.

    Exposes ``get_access_token(force_refresh=False)`` and ``clear_cache()`` so
    it drops into ``tools.microsoft_graph_client.MicrosoftGraphClient`` without
    modification. Persists the refreshed bundle back to the token file so
    subsequent runs start from a current refresh token.
    """

    def __init__(
        self,
        token_file: Path = DEFAULT_TOKEN_FILE,
        *,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        scope: str = GRAPH_SCOPE,
        skew_seconds: int = 120,
    ) -> None:
        self.token_file = Path(token_file)
        self.tenant_id = tenant_id or os.environ.get("LINEO_MS_TENANT_ID") or ""
        self.client_id = client_id or os.environ.get("LINEO_MS_CLIENT_ID") or ""
        self.scope = scope
        self.skew_seconds = max(0, int(skew_seconds))
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        if (
            not force_refresh
            and self._access_token is not None
            and time.time() + self.skew_seconds < self._expires_at
        ):
            return self._access_token
        async with self._lock:
            if (
                not force_refresh
                and self._access_token is not None
                and time.time() + self.skew_seconds < self._expires_at
            ):
                return self._access_token
            await asyncio.to_thread(self._refresh_blocking)
            assert self._access_token is not None
            return self._access_token

    def clear_cache(self) -> None:
        self._access_token = None
        self._expires_at = 0.0

    def _refresh_blocking(self) -> None:
        if not self.tenant_id or not self.client_id:
            raise RuntimeError(
                "DelegatedTokenProvider needs LINEO_MS_TENANT_ID / LINEO_MS_CLIENT_ID "
                "(load via ~/.hermes/.env)."
            )
        bundle = json.loads(self.token_file.read_text())
        body = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": bundle["refresh_token"],
                "scope": self.scope,
            }
        ).encode()
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
        os.chmod(self.token_file, 0o600)
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600))


# ──────────────────────────────────────────────────────────────────────────
# Blob backend — pluggable storage for downloaded PDFs.
# Local default writes under ~/.hermes/finance/blobs/<sha256>.pdf.
# ──────────────────────────────────────────────────────────────────────────


class BlobBackend(Protocol):
    def path_for(self, *, sha256: str, original_name: Optional[str]) -> str: ...
    def store(self, *, src: Path, sha256: str, original_name: Optional[str]) -> str: ...


@dataclass
class LocalBlobBackend:
    root: Path = DEFAULT_BLOB_ROOT

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def path_for(self, *, sha256: str, original_name: Optional[str]) -> str:
        # Two-level fan-out to keep directory size sane.
        return str(self.root / sha256[:2] / f"{sha256}.pdf")

    def store(self, *, src: Path, sha256: str, original_name: Optional[str]) -> str:
        import shutil  # noqa: WPS433 — local import keeps the import surface tight

        dst = Path(self.path_for(sha256=sha256, original_name=original_name))
        dst.parent.mkdir(parents=True, exist_ok=True)
        src_path = Path(src)
        if dst.exists():
            # SHA collision means same content — keep existing file.
            if src_path.exists() and src_path.resolve() != dst.resolve():
                src_path.unlink(missing_ok=True)
            return str(dst)
        # shutil.move falls back to copy+remove across filesystems
        # (/tmp → $HOME often crosses a tmpfs / persistent boundary).
        shutil.move(str(src_path), str(dst))
        return str(dst)


# ──────────────────────────────────────────────────────────────────────────
# PDF text extraction
# ──────────────────────────────────────────────────────────────────────────


def extract_pdf_text(pdf_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Return (text, error). text is None on irrecoverable failure.

    Strategy: pymupdf default. If the extracted text is below
    ``PYMUPDF_MIN_CHARS``, try marker-pdf if explicitly enabled via the
    ``FINANCE_INDEXER_ALLOW_MARKER`` env flag — marker-pdf is a 3–5 GB
    install so it must not be a hard dependency. When marker-pdf is not
    enabled and pymupdf returned ~no content, return (None, reason) per
    #27's honest-tool-boundaries rule rather than a confident guess.
    """
    try:
        import pymupdf  # type: ignore[import]
    except ImportError as exc:  # pragma: no cover - environment guard
        return None, f"pymupdf import failed: {exc}"

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:  # noqa: BLE001 - vendor library may raise anything
        return None, f"pymupdf open failed: {exc}"

    try:
        text = "\n".join(page.get_text() for page in doc).strip()
    finally:
        doc.close()

    if len(text) >= PYMUPDF_MIN_CHARS:
        return text, None

    if os.environ.get("FINANCE_INDEXER_ALLOW_MARKER", "").lower() in ("1", "true", "yes"):
        try:
            # marker-pdf is heavy; only imported when the flag is set.
            from marker.convert import convert_single_pdf  # type: ignore[import]
            from marker.models import load_all_models  # type: ignore[import]
        except ImportError as exc:
            return None, (
                f"pymupdf yielded {len(text)} chars; marker-pdf requested but unavailable: {exc}"
            )
        try:
            models = load_all_models()
            full_text, _images, _meta = convert_single_pdf(str(pdf_path), models)
            full_text = (full_text or "").strip()
            if len(full_text) >= PYMUPDF_MIN_CHARS:
                return full_text, None
            return None, f"marker-pdf returned only {len(full_text)} chars"
        except Exception as exc:  # noqa: BLE001
            return None, f"marker-pdf extraction failed: {exc}"

    return None, f"pymupdf yielded {len(text)} chars (below {PYMUPDF_MIN_CHARS}); marker-pdf gated"


# ──────────────────────────────────────────────────────────────────────────
# HTML → plain text (for body inspection on portal-only notifications)
# ──────────────────────────────────────────────────────────────────────────

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&#\d+;|&[a-zA-Z]+;")
_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&quot;": '"',
    "&lt;": "<",
    "&gt;": ">",
    "&apos;": "'",
}


def html_to_text(html: str) -> str:
    txt = _HTML_TAG_RE.sub("\n", html or "")
    for entity, replacement in _HTML_ENTITIES.items():
        txt = txt.replace(entity, replacement)
    txt = _HTML_ENTITY_RE.sub("", txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n[ \t]*", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip() + "\n"


# ──────────────────────────────────────────────────────────────────────────
# Delta-token expiry recovery
# ──────────────────────────────────────────────────────────────────────────


class DeltaTokenExpired(Exception):
    """Raised when Graph rejects the persisted delta token."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"delta token rejected: {status} {reason}")
        self.status = status
        self.reason = reason


def _is_delta_token_expired(status: int, body: str) -> bool:
    """Graph signals an expired sync state with 410 Gone or an invalid token
    400-class response. Body codes seen in the wild include 'syncStateNotFound'
    and 'invalidRequest' with a message that mentions delta.
    """
    if status == 410:
        return True
    if status in (400, 404):
        lower = (body or "").lower()
        return any(
            tok in lower
            for tok in (
                "syncstatenotfound",
                "syncstatereset",
                "deltatoken",
                "delta token",
                "invalid token",
                "resync",
            )
        )
    return False


# ──────────────────────────────────────────────────────────────────────────
# Graph fetching
# ──────────────────────────────────────────────────────────────────────────


class GraphFetcher:
    """Thin wrapper around tools.microsoft_graph_client.MicrosoftGraphClient.

    Centralises the calls the indexer needs (delta pagination, attachment
    enumeration, message body fetch, streaming download) so the in-memory
    test adapter has a single surface to fake.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    async def iter_delta_messages(
        self,
        mailbox: str,
        *,
        folder: str,
        since: Optional[datetime],
        delta_token: Optional[str],
    ) -> tuple[list[dict[str, Any]], Optional[str], bool, Optional[str]]:
        """Pull every message page; return (messages, new_delta_token,
        reset_occurred, reset_reason).

        Pagination follows ``@odata.nextLink`` and yields the final
        ``@odata.deltaLink`` as the new token. On a 410 / invalid-token
        response (per ``_is_delta_token_expired``) the call retries with a
        fresh delta call bounded by ``since``; ``reset_occurred=True`` and
        ``reset_reason`` capture the recovery so the caller can re-dedupe
        via internet_message_id.
        """
        messages: list[dict[str, Any]] = []
        reset_occurred = False
        reset_reason: Optional[str] = None
        select = (
            "id,subject,from,receivedDateTime,hasAttachments,internetMessageId,"
            "parentFolderId,bodyPreview"
        )

        first_path, first_params = self._initial_delta(
            mailbox, folder=folder, since=since, delta_token=delta_token, select=select
        )
        path: Optional[str] = first_path
        params: Optional[dict[str, Any]] = first_params

        while path:
            try:
                page = await self.client.get_json(path, params=params)
            except Exception as exc:  # noqa: BLE001 — inspect for delta-expiry shape
                status, body = _exception_status_and_body(exc)
                if status is not None and _is_delta_token_expired(status, body) and not reset_occurred:
                    reset_occurred = True
                    reset_reason = f"http {status}: {body[:200]}"
                    path, params = self._initial_delta(
                        mailbox, folder=folder, since=since, delta_token=None, select=select
                    )
                    messages = []
                    continue
                raise

            messages.extend(page.get("value", []) or [])
            next_link = page.get("@odata.nextLink")
            delta_link = page.get("@odata.deltaLink")
            if next_link:
                path, params = next_link, None
                continue
            new_token = self._extract_delta_token(delta_link) if delta_link else delta_token
            return messages, new_token, reset_occurred, reset_reason

        return messages, delta_token, reset_occurred, reset_reason

    def _initial_delta(
        self,
        mailbox: str,
        *,
        folder: str,
        since: Optional[datetime],
        delta_token: Optional[str],
        select: str,
    ) -> tuple[str, dict[str, Any]]:
        # Folder-scoped delta is the only supported shape; mailbox-wide
        # /messages/delta returns "Change tracking is not supported".
        path = f"/users/{mailbox}/mailFolders/{folder}/messages/delta"
        params: dict[str, Any] = {"$select": select}
        if delta_token:
            params["$deltatoken"] = delta_token
        elif since is not None:
            since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            params["$filter"] = f"receivedDateTime ge {since_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        return path, params

    @staticmethod
    def _extract_delta_token(delta_link: str) -> Optional[str]:
        parsed = urllib.parse.urlparse(delta_link)
        qs = urllib.parse.parse_qs(parsed.query)
        vals = qs.get("$deltatoken") or qs.get("$deltaToken")
        if vals:
            return vals[0]
        return delta_link  # keep the URL as the cursor if no parse

    async def fetch_message(self, mailbox: str, message_id: str) -> dict[str, Any]:
        select = (
            "id,subject,from,receivedDateTime,hasAttachments,internetMessageId,"
            "parentFolderId,body"
        )
        return await self.client.get_json(
            f"/users/{mailbox}/messages/{message_id}",
            params={"$select": select},
        )

    async def list_attachments(self, mailbox: str, message_id: str) -> list[dict[str, Any]]:
        return await self.client.collect_paginated(
            f"/users/{mailbox}/messages/{message_id}/attachments"
        )

    async def download_attachment(
        self,
        mailbox: str,
        message_id: str,
        attachment_id: str,
        destination: Path,
    ) -> dict[str, Any]:
        return await self.client.download_to_file(
            f"/users/{mailbox}/messages/{message_id}/attachments/{attachment_id}/$value",
            destination,
        )


def _exception_status_and_body(exc: Exception) -> tuple[Optional[int], str]:
    status = getattr(exc, "status_code", None)
    payload = getattr(exc, "payload", None)
    body = ""
    if isinstance(payload, dict):
        body = json.dumps(payload)
    else:
        body = str(exc)
    return (int(status) if status is not None else None), body


# ──────────────────────────────────────────────────────────────────────────
# Core run logic — mailbox-scoped, adapter-driven
# ──────────────────────────────────────────────────────────────────────────


def _sender_domain(msg: Mapping[str, Any]) -> Optional[str]:
    addr = (((msg.get("from") or {}).get("emailAddress") or {}).get("address") or "").strip()
    if "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1].lower()


def _sender_address(msg: Mapping[str, Any]) -> Optional[str]:
    addr = (((msg.get("from") or {}).get("emailAddress") or {}).get("address") or "").strip()
    return addr or None


def _parse_received_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_pdf_attachment(att: Mapping[str, Any]) -> bool:
    content_type = (att.get("contentType") or "").lower()
    name = (att.get("name") or "").lower()
    if content_type.startswith("application/pdf"):
        return True
    if name.endswith(".pdf"):
        return True
    return False


def _is_mime_container_attachment(att: Mapping[str, Any]) -> bool:
    """True for Graph fileAttachments that may wrap real PDF files.

    DATEV e-invoice mail often appears in Graph as one top-level
    ``smime.p7m`` / ``multipart/signed`` attachment. The actual invoice PDF is
    nested inside that MIME object, so the indexer must treat it as a container
    rather than skipping it as "not a PDF".
    """
    content_type = (att.get("contentType") or "").lower()
    name = (att.get("name") or "").lower()
    return (
        content_type.startswith("multipart/")
        or content_type == "message/rfc822"
        or name.endswith((".p7m", ".eml"))
    )


def _extract_nested_file_attachments(
    blob: bytes,
    *,
    parent_name: Optional[str],
    parent_content_type: Optional[str],
) -> list[tuple[str, Optional[str], bytes]]:
    """Extract real file attachments from MIME container attachments.

    If ``blob`` is not a parseable multipart MIME container, return [] so the
    caller can preserve the original attachment behavior.
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
    except Exception:  # noqa: BLE001
        return []
    if not msg.is_multipart():
        return []

    out: list[tuple[str, Optional[str], bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()
        part_ct = part.get_content_type()
        if not filename and disposition != "attachment":
            continue
        if part_ct in {"application/pkcs7-signature", "application/x-pkcs7-signature"}:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            out.append((filename or "attachment", part_ct, payload))
    return out


def _looks_portal_required(sender_address: Optional[str], allowlist: Sequence[str]) -> bool:
    if not sender_address:
        return False
    needle = sender_address.lower()
    for entry in allowlist:
        if entry.lower() in needle:
            return True
    return False


async def _process_message(
    *,
    msg_summary: Mapping[str, Any],
    mailbox: str,
    adapter: IndexerAdapter,
    fetcher: GraphFetcher,
    blob_backend: BlobBackend,
    portal_required_senders: Sequence[str],
    parser: Any,
    summary: RunSummary,
    blob_tmp_root: Path,
) -> None:
    """Index one Graph message. Inserts 0..N receipt_candidates rows."""
    summary.scanned += 1

    msg_id = msg_summary.get("id")
    internet_message_id = msg_summary.get("internetMessageId")
    outlook_message_id = msg_id

    # Coexistence with the legacy outlook_auto_rule: never re-index a message
    # already in belege_sent (server-side forward audit).
    if adapter.belege_sent_has_message(
        internet_message_id=internet_message_id,
        outlook_message_id=outlook_message_id,
    ):
        return

    # Dedupe candidates already inserted by a previous run (covers delta
    # reset re-walking the same message).
    already_indexed = adapter.candidate_exists_for_message(
        mailbox=mailbox,
        internet_message_id=internet_message_id,
        outlook_message_id=outlook_message_id,
    )

    has_attachments = bool(msg_summary.get("hasAttachments"))
    sender = _sender_address(msg_summary)
    is_portal_required = _looks_portal_required(sender, portal_required_senders)

    if not has_attachments and not is_portal_required:
        return  # noise; not a receipt-shaped message

    received_at = _parse_received_at(msg_summary.get("receivedDateTime"))

    if has_attachments:
        attachments = await fetcher.list_attachments(mailbox, msg_id)
        candidate_atts = [
            a for a in attachments
            if _is_pdf_attachment(a) or _is_mime_container_attachment(a)
        ]
        if not candidate_atts and is_portal_required:
            # Has non-PDF/non-container attachments + portal sender: treat as notification-only.
            await _emit_portal_notification(
                mailbox=mailbox,
                msg_summary=msg_summary,
                adapter=adapter,
                fetcher=fetcher,
                received_at=received_at,
                summary=summary,
                already_indexed=already_indexed,
            )
            return
        for att in candidate_atts:
            await _emit_attachment_candidate(
                mailbox=mailbox,
                msg_summary=msg_summary,
                attachment=att,
                adapter=adapter,
                fetcher=fetcher,
                blob_backend=blob_backend,
                blob_tmp_root=blob_tmp_root,
                parser=parser,
                received_at=received_at,
                summary=summary,
            )
        return

    if is_portal_required:
        await _emit_portal_notification(
            mailbox=mailbox,
            msg_summary=msg_summary,
            adapter=adapter,
            fetcher=fetcher,
            received_at=received_at,
            summary=summary,
            already_indexed=already_indexed,
        )


async def _emit_attachment_candidate(
    *,
    mailbox: str,
    msg_summary: Mapping[str, Any],
    attachment: Mapping[str, Any],
    adapter: IndexerAdapter,
    fetcher: GraphFetcher,
    blob_backend: BlobBackend,
    blob_tmp_root: Path,
    parser: Any,
    received_at: Optional[datetime],
    summary: RunSummary,
) -> None:
    msg_id = msg_summary.get("id")
    att_id = attachment.get("id")
    att_name = attachment.get("name")
    content_type = attachment.get("contentType")

    # Inline attachments arrive with contentBytes in the original page.
    inline_bytes = attachment.get("contentBytes")
    if inline_bytes:
        import base64

        data = base64.b64decode(inline_bytes)
    else:
        blob_tmp_root.mkdir(parents=True, exist_ok=True)
        suffix = ".pdf" if _is_pdf_attachment(attachment) else ".bin"
        tmp_download = blob_tmp_root / f"{att_id}{suffix}"
        await fetcher.download_attachment(mailbox, msg_id, att_id, tmp_download)
        data = tmp_download.read_bytes()
        tmp_download.unlink(missing_ok=True)

    if _is_mime_container_attachment(attachment) and not _is_pdf_attachment(attachment):
        nested = _extract_nested_file_attachments(
            data,
            parent_name=att_name,
            parent_content_type=content_type,
        )
        nested_pdfs = [
            (name, ctype, blob)
            for name, ctype, blob in nested
            if (ctype or "").lower().startswith("application/pdf")
            or (name or "").lower().endswith(".pdf")
        ]
        for nested_name, nested_ctype, nested_blob in nested_pdfs:
            _emit_attachment_bytes_candidate(
                mailbox=mailbox,
                msg_summary=msg_summary,
                attachment_name=nested_name,
                content_type=nested_ctype,
                data=nested_blob,
                adapter=adapter,
                blob_backend=blob_backend,
                blob_tmp_root=blob_tmp_root,
                parser=parser,
                received_at=received_at,
                summary=summary,
            )
        if not nested_pdfs:
            summary.parse_failed += 1
        return

    _emit_attachment_bytes_candidate(
        mailbox=mailbox,
        msg_summary=msg_summary,
        attachment_name=att_name,
        content_type=content_type,
        data=data,
        adapter=adapter,
        blob_backend=blob_backend,
        blob_tmp_root=blob_tmp_root,
        parser=parser,
        received_at=received_at,
        summary=summary,
    )


def _emit_attachment_bytes_candidate(
    *,
    mailbox: str,
    msg_summary: Mapping[str, Any],
    attachment_name: Optional[str],
    content_type: Optional[str],
    data: bytes,
    adapter: IndexerAdapter,
    blob_backend: BlobBackend,
    blob_tmp_root: Path,
    parser: Any,
    received_at: Optional[datetime],
    summary: RunSummary,
) -> None:
    att_name = attachment_name
    msg_id = msg_summary.get("id")
    sha = hashlib.sha256(data).hexdigest()
    existing = adapter.candidate_by_sha256(sha)
    if existing is not None:
        summary.dedup_skipped += 1
        return
    blob_tmp_root.mkdir(parents=True, exist_ok=True)
    tmp_path = blob_tmp_root / f"{sha}.pdf"
    tmp_path.write_bytes(data)
    size_bytes = len(data)

    blob_path = blob_backend.store(src=tmp_path, sha256=sha, original_name=att_name)

    extracted_text, text_error = extract_pdf_text(Path(blob_path))
    text_sha = (
        hashlib.sha256(extracted_text.encode("utf-8")).hexdigest()
        if extracted_text is not None
        else None
    )

    parse_status = "pending"
    parse_error: Optional[str] = None
    extracted_json: dict[str, Any] = {}

    if extracted_text is None:
        parse_status = "failed"
        parse_error = text_error or "pdf text extraction returned no content"
    else:
        candidate_view = {
            "extracted_text": extracted_text,
            "from_email": _sender_address(msg_summary),
            "subject": msg_summary.get("subject"),
            "attachment_name": att_name,
            "internet_message_id": msg_summary.get("internetMessageId"),
            "received_at": msg_summary.get("receivedDateTime"),
        }
        try:
            parsed = parser(candidate_view) if parser is not None else None
        except Exception as exc:  # noqa: BLE001
            parsed = None
            parse_error = f"parser raised: {exc!r}"
        if parsed is None:
            parse_status = "failed"
            if parse_error is None:
                parse_error = "no vendor parser matched"
        else:
            extracted_json = dict(parsed)
            parse_status = extracted_json.get("parse_status", "pending") or "pending"
            parse_error = extracted_json.get("parse_error")

    adapter.insert_candidate(
        source_system="graph",
        mailbox=mailbox,
        outlook_message_id=msg_id,
        internet_message_id=msg_summary.get("internetMessageId"),
        received_at=received_at,
        from_email=_sender_address(msg_summary),
        subject=msg_summary.get("subject"),
        attachment_name=att_name,
        attachment_sha256=sha,
        local_blob_path=blob_path,
        text_sha256=text_sha,
        extracted_text=extracted_text,
        extracted_json=extracted_json,
        parse_status=parse_status,
        parse_error=parse_error,
    )
    summary.new += 1
    if parse_status == "portal_required":
        summary.portal_required += 1
    elif parse_status == "failed":
        summary.parse_failed += 1


async def _emit_portal_notification(
    *,
    mailbox: str,
    msg_summary: Mapping[str, Any],
    adapter: IndexerAdapter,
    fetcher: GraphFetcher,
    received_at: Optional[datetime],
    summary: RunSummary,
    already_indexed: bool,
) -> None:
    """Insert a parse_status='portal_required' row for a sender on the allowlist."""
    if already_indexed:
        return

    msg_id = msg_summary.get("id")
    body_text: Optional[str] = None
    try:
        full = await fetcher.fetch_message(mailbox, msg_id)
        body_html = (full.get("body") or {}).get("content") or ""
        body_text = html_to_text(body_html)
    except Exception:  # noqa: BLE001 — body is informational, not load-bearing
        body_text = None

    extracted_json = {
        "vendor": "vodafone",  # only vendor on the default allowlist
        "parse_status": "portal_required",
        "parse_error": "portal notification only (no PDF attachment)",
        "source_subject": msg_summary.get("subject"),
    }

    adapter.insert_candidate(
        source_system="graph",
        mailbox=mailbox,
        outlook_message_id=msg_id,
        internet_message_id=msg_summary.get("internetMessageId"),
        received_at=received_at,
        from_email=_sender_address(msg_summary),
        subject=msg_summary.get("subject"),
        attachment_name=None,
        attachment_sha256=None,
        local_blob_path=None,
        text_sha256=(
            hashlib.sha256(body_text.encode("utf-8")).hexdigest() if body_text else None
        ),
        extracted_text=body_text,
        extracted_json=extracted_json,
        parse_status="portal_required",
        parse_error=extracted_json["parse_error"],
    )
    summary.new += 1
    summary.portal_required += 1


# ──────────────────────────────────────────────────────────────────────────
# Public entry points
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class IndexerConfig:
    mailboxes: Sequence[str] = field(default_factory=lambda: list(DEFAULT_MAILBOXES))
    folders: Sequence[str] = field(default_factory=lambda: list(DEFAULT_FOLDERS))
    portal_required_senders: Sequence[str] = field(
        default_factory=lambda: list(DEFAULT_PORTAL_REQUIRED_SENDERS)
    )
    backfill_months: int = DEFAULT_BACKFILL_MONTHS
    blob_backend: BlobBackend = field(default_factory=LocalBlobBackend)
    blob_tmp_root: Path = field(default_factory=lambda: Path("/tmp/finance-indexer"))
    limit_per_mailbox: Optional[int] = None
    since: Optional[datetime] = None  # explicit override; else backfill_months


def _state_key(mailbox: str, folder: str) -> str:
    """Composite key stored in bank.indexer_state.mailbox.

    The schema is keyed on a single text column; folder-scoped delta needs a
    second dimension. Encoding ``mailbox::folder`` keeps the schema flat and
    makes the meaning grep-able. ``folder="inbox"`` is normalised lowercase.
    """
    return f"{mailbox}::{folder.lower()}"


async def _run_async(
    config: IndexerConfig,
    adapter: IndexerAdapter,
    fetcher: GraphFetcher,
    parser: Any,
) -> AggregateSummary:
    agg = AggregateSummary()
    for mailbox in config.mailboxes:
        for folder in config.folders:
            per = RunSummary(mailbox=f"{mailbox}::{folder}")
            agg.mailboxes.append(per)
            try:
                await _run_one_mailbox(
                    mailbox=mailbox,
                    folder=folder,
                    config=config,
                    adapter=adapter,
                    fetcher=fetcher,
                    parser=parser,
                    summary=per,
                )
            except Exception as exc:  # noqa: BLE001 — surface, don't crash other folders
                per.error = repr(exc)
                adapter.upsert_state(
                    _state_key(mailbox, folder),
                    delta_token=None,
                    last_run_at=datetime.now(timezone.utc),
                    last_summary=per.to_dict(),
                    last_error=per.error,
                )
    return agg


async def _run_one_mailbox(
    *,
    mailbox: str,
    folder: str,
    config: IndexerConfig,
    adapter: IndexerAdapter,
    fetcher: GraphFetcher,
    parser: Any,
    summary: RunSummary,
) -> None:
    state_key = _state_key(mailbox, folder)
    state = adapter.get_state(state_key)
    delta_token = state.delta_token if state else None
    since: Optional[datetime] = config.since
    if not delta_token and since is None:
        since = datetime.now(timezone.utc) - timedelta(days=30 * config.backfill_months)

    messages, new_token, reset_occurred, reset_reason = await fetcher.iter_delta_messages(
        mailbox, folder=folder, since=since, delta_token=delta_token
    )
    summary.delta_reset = reset_occurred
    summary.delta_reset_reason = reset_reason

    if config.limit_per_mailbox is not None:
        messages = messages[: config.limit_per_mailbox]

    for msg in messages:
        # Skip "deleted" stub items Graph emits in delta pages
        if "@removed" in msg:
            continue
        try:
            await _process_message(
                msg_summary=msg,
                mailbox=mailbox,
                adapter=adapter,
                fetcher=fetcher,
                blob_backend=config.blob_backend,
                portal_required_senders=config.portal_required_senders,
                parser=parser,
                summary=summary,
                blob_tmp_root=config.blob_tmp_root,
            )
        except Exception as exc:  # noqa: BLE001 — one bad message must not poison the run
            summary.parse_failed += 1
            # surface in the run summary; the message is left un-indexed and
            # will retry on the next run with the same internet_message_id.
            if summary.error is None:
                summary.error = repr(exc)

    adapter.upsert_state(
        state_key,
        delta_token=new_token,
        last_run_at=datetime.now(timezone.utc),
        last_summary=summary.to_dict(),
        last_error=summary.error,
    )


def run(
    *,
    config: Optional[IndexerConfig] = None,
    adapter: Optional[IndexerAdapter] = None,
    fetcher: Optional[GraphFetcher] = None,
    parser: Any = None,
) -> AggregateSummary:
    """Synchronous entry point used by both the CLI and #23's `run_indexer()`.

    Caller supplies the IndexerAdapter (Postgres in prod, in-memory in tests)
    and a GraphFetcher (real Graph in prod, a fake in tests). The `parser`
    argument is the vendor-dispatch callable from #22 — passed in so the
    indexer doesn't statically import a module that may not exist in test
    environments.
    """
    if config is None:
        config = IndexerConfig()
    if adapter is None:
        raise ValueError("indexer.run() needs an adapter")
    if fetcher is None:
        raise ValueError("indexer.run() needs a fetcher")
    if parser is None:
        # Lazy import so tests can inject a fake parser without triggering
        # the real one.
        from finance.parsers import parse as parser  # type: ignore[import]
    return asyncio.run(_run_async(config, adapter, fetcher, parser))


# ──────────────────────────────────────────────────────────────────────────
# Postgres adapter — used by the CLI; mirrors finance/matcher.py style
# ──────────────────────────────────────────────────────────────────────────


class PostgresIndexerAdapter:
    """psycopg2-backed adapter. Reads `bank.belege_sent` (dedupe only),
    reads/writes `bank.indexer_state`, writes `bank.receipt_candidates`.
    Never writes to bank.transactions or bank.belege_sent.
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    def _cursor(self):
        import psycopg2.extras  # noqa: WPS433
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def get_state(self, mailbox: str) -> Optional[IndexerState]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT mailbox, delta_token, last_run_at, last_summary, last_error "
                "FROM bank.indexer_state WHERE mailbox = %s",
                (mailbox,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return IndexerState(
                mailbox=row["mailbox"],
                delta_token=row["delta_token"],
                last_run_at=row["last_run_at"],
                last_summary=row["last_summary"],
                last_error=row["last_error"],
            )

    def upsert_state(
        self,
        mailbox: str,
        *,
        delta_token: Optional[str],
        last_run_at: datetime,
        last_summary: Mapping[str, Any],
        last_error: Optional[str] = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO bank.indexer_state (mailbox, delta_token, last_run_at, last_summary, last_error)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (mailbox) DO UPDATE
                  SET delta_token = EXCLUDED.delta_token,
                      last_run_at = EXCLUDED.last_run_at,
                      last_summary = EXCLUDED.last_summary,
                      last_error = EXCLUDED.last_error,
                      updated_at = now()
                """,
                (mailbox, delta_token, last_run_at, json.dumps(last_summary), last_error),
            )
        self.conn.commit()

    def candidate_by_sha256(self, sha256: str) -> Optional[int]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT id FROM bank.receipt_candidates WHERE attachment_sha256 = %s LIMIT 1",
                (sha256,),
            )
            row = cur.fetchone()
            return row["id"] if row else None

    def candidate_exists_for_message(
        self,
        *,
        mailbox: str,
        internet_message_id: Optional[str],
        outlook_message_id: Optional[str],
    ) -> bool:
        if not internet_message_id and not outlook_message_id:
            return False
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM bank.receipt_candidates
                WHERE mailbox = %s
                  AND (
                    (%s IS NOT NULL AND internet_message_id = %s)
                    OR (%s IS NOT NULL AND outlook_message_id = %s)
                  )
                LIMIT 1
                """,
                (
                    mailbox,
                    internet_message_id,
                    internet_message_id,
                    outlook_message_id,
                    outlook_message_id,
                ),
            )
            return cur.fetchone() is not None

    def belege_sent_has_message(
        self,
        *,
        internet_message_id: Optional[str],
        outlook_message_id: Optional[str],
    ) -> bool:
        if not internet_message_id and not outlook_message_id:
            return False
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM bank.belege_sent
                WHERE (%s IS NOT NULL AND internet_message_id = %s)
                   OR (%s IS NOT NULL AND outlook_message_id  = %s)
                LIMIT 1
                """,
                (
                    internet_message_id,
                    internet_message_id,
                    outlook_message_id,
                    outlook_message_id,
                ),
            )
            return cur.fetchone() is not None

    def insert_candidate(
        self,
        *,
        source_system: str,
        mailbox: str,
        outlook_message_id: Optional[str],
        internet_message_id: Optional[str],
        received_at: Optional[datetime],
        from_email: Optional[str],
        subject: Optional[str],
        attachment_name: Optional[str],
        attachment_sha256: Optional[str],
        local_blob_path: Optional[str],
        text_sha256: Optional[str],
        extracted_text: Optional[str],
        extracted_json: Mapping[str, Any],
        parse_status: str,
        parse_error: Optional[str],
    ) -> int:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO bank.receipt_candidates (
                    source_system, mailbox, outlook_message_id, internet_message_id,
                    received_at, from_email, subject, attachment_name,
                    attachment_sha256, local_blob_path,
                    text_sha256, extracted_text, extracted_json,
                    parse_status, parse_error
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                RETURNING id
                """,
                (
                    source_system,
                    mailbox,
                    outlook_message_id,
                    internet_message_id,
                    received_at,
                    from_email,
                    subject,
                    attachment_name,
                    attachment_sha256,
                    local_blob_path,
                    text_sha256,
                    extracted_text,
                    json.dumps(dict(extracted_json)),
                    parse_status,
                    parse_error,
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        return row["id"]


# ──────────────────────────────────────────────────────────────────────────
# In-memory adapter — for tests + dry runs
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class InMemoryIndexerAdapter:
    state_by_mailbox: dict[str, IndexerState] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    belege_sent_imids: set[str] = field(default_factory=set)
    belege_sent_omids: set[str] = field(default_factory=set)
    _next_id: int = 1

    def get_state(self, mailbox: str) -> Optional[IndexerState]:
        return self.state_by_mailbox.get(mailbox)

    def upsert_state(
        self,
        mailbox: str,
        *,
        delta_token: Optional[str],
        last_run_at: datetime,
        last_summary: Mapping[str, Any],
        last_error: Optional[str] = None,
    ) -> None:
        self.state_by_mailbox[mailbox] = IndexerState(
            mailbox=mailbox,
            delta_token=delta_token,
            last_run_at=last_run_at,
            last_summary=dict(last_summary),
            last_error=last_error,
        )

    def candidate_by_sha256(self, sha256: str) -> Optional[int]:
        for c in self.candidates:
            if c.get("attachment_sha256") == sha256:
                return c["id"]
        return None

    def candidate_exists_for_message(
        self,
        *,
        mailbox: str,
        internet_message_id: Optional[str],
        outlook_message_id: Optional[str],
    ) -> bool:
        for c in self.candidates:
            if c.get("mailbox") != mailbox:
                continue
            if internet_message_id and c.get("internet_message_id") == internet_message_id:
                return True
            if outlook_message_id and c.get("outlook_message_id") == outlook_message_id:
                return True
        return False

    def belege_sent_has_message(
        self,
        *,
        internet_message_id: Optional[str],
        outlook_message_id: Optional[str],
    ) -> bool:
        if internet_message_id and internet_message_id in self.belege_sent_imids:
            return True
        if outlook_message_id and outlook_message_id in self.belege_sent_omids:
            return True
        return False

    def insert_candidate(self, **fields: Any) -> int:
        new_id = self._next_id
        self._next_id += 1
        row = dict(fields)
        row["id"] = new_id
        self.candidates.append(row)
        return new_id


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────


def _load_env_file(path: Path = DEFAULT_ENV_FILE) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _build_cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m finance.indexer",
        description=__doc__,
    )
    p.add_argument("--mailbox", action="append", help="Mailbox to index (repeatable)")
    p.add_argument(
        "--folder",
        action="append",
        help="Folder per mailbox (well-known ID or displayName; default: inbox)",
    )
    p.add_argument("--since", help="Lower bound (YYYY-MM-DD); overrides the 6-month default")
    p.add_argument("--limit", type=int, help="Max messages per mailbox/folder (smoke testing)")
    p.add_argument(
        "--blob-root",
        default=str(DEFAULT_BLOB_ROOT),
        help=f"Local blob directory (default: {DEFAULT_BLOB_ROOT})",
    )
    p.add_argument(
        "--token-file",
        default=str(DEFAULT_TOKEN_FILE),
        help=f"Delegated MS token bundle (default: {DEFAULT_TOKEN_FILE})",
    )
    return p


def _build_graph_fetcher(token_file: Path) -> GraphFetcher:
    # Local import: tools.microsoft_graph_client requires httpx, which is in
    # the project's venv but not always in /usr/bin/python3. Importing here
    # keeps `python -m finance.indexer --help` working in either env.
    from tools.microsoft_graph_client import MicrosoftGraphClient

    provider = DelegatedTokenProvider(token_file=token_file)
    client = MicrosoftGraphClient(provider, user_agent="Hermes-Agent/finance-indexer")
    return GraphFetcher(client)


def _parse_since(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(argv)

    _load_env_file()

    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        print("SUPABASE_DB_URL not set (load via ~/.hermes/.env)", file=sys.stderr)
        return 2

    mailboxes = args.mailbox or list(DEFAULT_MAILBOXES)
    folders = args.folder or list(DEFAULT_FOLDERS)
    config = IndexerConfig(
        mailboxes=mailboxes,
        folders=folders,
        limit_per_mailbox=args.limit,
        since=_parse_since(args.since),
        blob_backend=LocalBlobBackend(Path(args.blob_root)),
    )

    fetcher = _build_graph_fetcher(Path(args.token_file))

    import psycopg2  # noqa: WPS433 — local so --help works without psycopg2

    conn = psycopg2.connect(url)
    try:
        adapter = PostgresIndexerAdapter(conn)
        agg = run(config=config, adapter=adapter, fetcher=fetcher)
    finally:
        conn.close()

    print(json.dumps(agg.to_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

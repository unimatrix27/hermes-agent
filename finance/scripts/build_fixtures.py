#!/usr/bin/env python3
"""Extract the public-fork-safe finance reconciliation fixture pack.

Implements the one-shot extraction defined in unimatrix27/ideas#24:

    1. PDF text fixtures per vendor (committed as .txt, never the PDF binary).
       For each vendor / invoice number listed in TARGETS the script searches
       the operator's Microsoft Graph mailboxes, downloads the attachment,
       runs pymupdf for text extraction, and writes:
           tests/fixtures/finance/<vendor>/<invoice>.txt
           tests/fixtures/finance/<vendor>/<invoice>.meta.json
       A "notification only" body is fetched for Vodafone (portal-only
       receipts) and written as <invoice>.txt with hasAttachments=false.

    2. transactions.jsonl  — one line per bank.transactions row for the 11
       named TX ids plus the Google Ads kanban-task row. Counterparty IBAN
       is redacted to "DE**" before commit (public fork safety).

    3. beleg_match_samples.jsonl  — a handful of representative beleg_match
       jsonb shapes, with all three via='manual_review' rows verbatim.

    4. belege_sent_samples.jsonl — 5-10 rows covering each `via` value,
       including at least 2 rows with bank_tx_id IS NULL and 2 with non-empty
       attachment_filenames.

The script does NOT run in CI. It needs:
    SUPABASE_DB_URL                (read-only is sufficient)
    LINEO_MS_TENANT_ID / CLIENT_ID (public client for delegated refresh)
    ~/.hermes/lineo-ms-tokens/sebastian.json  (refresh-token bundle)

Run:
    python3 finance/scripts/build_fixtures.py
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import psycopg2
import psycopg2.extras
import pymupdf

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "finance"

TOKEN_FILE = Path.home() / ".hermes" / "lineo-ms-tokens" / "sebastian.json"
ENV_FILE = Path.home() / ".hermes" / ".env"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "User.Read Mail.Read Mail.Read.Shared offline_access"


# ---------- Mailbox / PDF targets --------------------------------------------

@dataclass
class PdfTarget:
    vendor: str
    invoice: str
    query: str               # the $search literal that finds the right mail
    mailbox: str             # rechnung@ or marketing@
    sender_match: str        # substring to filter $search hits to the right vendor
    received_year_hint: int | None = None  # optional disambiguator


@dataclass
class NotificationTarget:
    vendor: str
    fixture_name: str        # used as <fixture_name>.txt
    query: str
    mailbox: str
    sender_match: str


PDF_TARGETS: list[PdfTarget] = [
    PdfTarget("sipgate", "B4373121",
              query='"B4373121"',
              mailbox="marketing@lineo.finance",
              sender_match="team@sipgate.de"),
    PdfTarget("sipgate", "B4411208",
              query='"B4411208"',
              mailbox="marketing@lineo.finance",
              sender_match="team@sipgate.de"),
    PdfTarget("sipgate", "B4459838",
              query='"B4459838"',
              mailbox="marketing@lineo.finance",
              sender_match="team@sipgate.de"),
    PdfTarget("notion", "ZWLWGPDN-0002",
              query='"ZWLWGPDN-0002"',
              mailbox="rechnung@lineo.finance",
              sender_match=""),  # forwarded into rechnung@; sender X400, allow any
    PdfTarget("lucky_penny", "6945-10683",
              query='"6945-10683"',
              mailbox="rechnung@lineo.finance",
              sender_match=""),
    PdfTarget("lucky_penny", "CN-6945-10021",
              query='"CN-6945-10021"',
              mailbox="rechnung@lineo.finance",
              sender_match=""),
    # Vodafone: any actual PDF attachment available. The "subject empty" rechnung@
    # inbox messages around 2026-05-10 carry a forwarded Vodafone PDF. Match by
    # body containing "Vodafone-Nr.".
    PdfTarget("vodafone", "122203440401",
              query='"122203440401"',
              mailbox="rechnung@lineo.finance",
              sender_match=""),
]

NOTIFICATION_TARGETS: list[NotificationTarget] = [
    NotificationTarget("vodafone", "portal_notification_2026_04",
                       query='"Mobilfunk-Rechnung vom 14.04.2026"',
                       mailbox="rechnung@lineo.finance",
                       sender_match="nicht.antworten@kundenservice.vodafone.com"),
]


# ---------- DB targets --------------------------------------------------------

NAMED_TX_IDS = [1, 5, 20, 27, 31, 39, 53, 56, 66, 68, 88]
KANBAN_TX_TAG = "t_51751302"   # Google Ads row, identified via beleg_match->>'kanban_task'


# ---------- Env helpers -------------------------------------------------------

def load_env_file(path: Path = ENV_FILE) -> None:
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


# ---------- Delegated Graph token cache ---------------------------------------

class DelegatedTokenCache:
    """Refresh-token-grant cache for the delegated lineo-ms-tokens bundle.

    Caches a single access token in memory and refreshes via the refresh_token
    grant when it nears expiry. Persists the refreshed bundle back to TOKEN_FILE
    so subsequent runs of the script start from a fresh refresh token.
    """

    def __init__(self, token_file: Path = TOKEN_FILE, *, skew_seconds: int = 120):
        self.token_file = token_file
        self.skew_seconds = skew_seconds
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def _refresh(self) -> None:
        tenant = os.environ["LINEO_MS_TENANT_ID"]
        client = os.environ["LINEO_MS_CLIENT_ID"]
        bundle = json.loads(self.token_file.read_text())
        body = urllib.parse.urlencode({
            "client_id": client,
            "grant_type": "refresh_token",
            "refresh_token": bundle["refresh_token"],
            "scope": GRAPH_SCOPE,
        }).encode()
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        req = urllib.request.Request(
            url, data=body,
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
        self._expires_at = time.time() + int(payload["expires_in"])

    def get(self) -> str:
        if self._access_token is None or time.time() + self.skew_seconds >= self._expires_at:
            self._refresh()
        assert self._access_token is not None
        return self._access_token


# ---------- Minimal Graph client (uses the cache above) -----------------------
#
# This script does NOT use tools/microsoft_graph_client.py because that client
# is wired for app-only client_credentials auth (MSGRAPH_TENANT_ID + secret).
# The operator's only access path is the delegated user token bundle, so we
# do raw urllib calls authenticated from the refresh-token cache. The bytes
# this script ships off to disk are the same shape we'd get from the upstream
# client; future tooling (#22 onward) can swap in either auth path.

def graph_get(path: str, cache: DelegatedTokenCache) -> dict:
    url = path if path.startswith("http") else GRAPH_BASE + path
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": "Bearer " + cache.get(),
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                cache._access_token = None
                continue
            if e.code in (429, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Graph {url} -> {e.code}: {e.read()[:300].decode(errors='replace')}")
    raise RuntimeError(f"Graph {url}: retries exhausted")


def search_messages(mailbox: str, query: str, cache: DelegatedTokenCache,
                    *, top: int = 10) -> list[dict]:
    select = "id,subject,from,receivedDateTime,hasAttachments,internetMessageId"
    ep = (f"/users/{mailbox}/messages?$top={top}"
          f"&$select={select}&$search={urllib.parse.quote(query)}")
    return graph_get(ep, cache).get("value", [])


def fetch_attachments(mailbox: str, msg_id: str, cache: DelegatedTokenCache) -> list[dict]:
    ep = f"/users/{mailbox}/messages/{msg_id}/attachments"
    return graph_get(ep, cache).get("value", [])


def fetch_message_body(mailbox: str, msg_id: str, cache: DelegatedTokenCache) -> dict:
    ep = (f"/users/{mailbox}/messages/{msg_id}"
          f"?$select=id,subject,from,receivedDateTime,hasAttachments,internetMessageId,body")
    return graph_get(ep, cache)


# ---------- PDF + body helpers ------------------------------------------------

def extract_pdf_text(pdf_bytes: bytes) -> str:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&#\d+;|&[a-zA-Z]+;")
_HTML_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&quot;": '"',
    "&lt;": "<", "&gt;": ">", "&apos;": "'",
}


def html_to_text(html: str) -> str:
    txt = _HTML_TAG_RE.sub("\n", html)
    for entity, replacement in _HTML_ENTITIES.items():
        txt = txt.replace(entity, replacement)
    txt = _HTML_ENTITY_RE.sub("", txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n[ \t]*", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip() + "\n"


def message_meta(msg: dict, attachment_name: str | None) -> dict:
    from_addr = (msg.get("from") or {}).get("emailAddress") or {}
    return {
        "from": from_addr.get("address") or from_addr.get("name") or "",
        "subject": msg.get("subject") or "",
        "received_at": msg.get("receivedDateTime") or "",
        "attachment_name": attachment_name or "",
        "internet_message_id": msg.get("internetMessageId") or "",
    }


# ---------- PDF fixture extraction --------------------------------------------

def pick_message(target: PdfTarget, hits: list[dict]) -> dict | None:
    """Pick the message most likely to be the vendor's original invoice mail.

    Prefer hits whose `from` address contains target.sender_match (case-insensitive),
    fall back to the earliest-received hit (the original, not a later forward).
    """
    if not hits:
        return None
    if target.sender_match:
        matched = [
            m for m in hits
            if target.sender_match.lower() in (
                ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
            ).lower()
        ]
        if matched:
            return sorted(matched, key=lambda m: m.get("receivedDateTime") or "")[0]
    return sorted(hits, key=lambda m: m.get("receivedDateTime") or "")[0]


def extract_pdf_fixture(target: PdfTarget, cache: DelegatedTokenCache) -> str:
    hits = search_messages(target.mailbox, target.query, cache, top=10)
    msg = pick_message(target, hits)
    if msg is None:
        return f"  [skip] {target.vendor}/{target.invoice}: no message matched query {target.query!r}"

    atts = fetch_attachments(target.mailbox, msg["id"], cache)
    pdf_atts = [a for a in atts if (a.get("contentType") or "").lower().startswith("application/pdf")]
    if not pdf_atts:
        return (f"  [skip] {target.vendor}/{target.invoice}: matched mail "
                f"{msg['id']} has no application/pdf attachment")

    chosen, chosen_text = None, ""
    for att in pdf_atts:
        if "contentBytes" not in att:
            continue
        pdf_bytes = base64.b64decode(att["contentBytes"])
        text = extract_pdf_text(pdf_bytes).strip()
        if target.invoice in text:
            chosen, chosen_text = att, text
            break
    if chosen is None:
        chosen = pdf_atts[0]
        chosen_text = extract_pdf_text(base64.b64decode(chosen["contentBytes"])).strip()

    if len(chosen_text) < 50:
        return (f"  [skip] {target.vendor}/{target.invoice}: pymupdf returned "
                f"~empty text ({len(chosen_text)} chars) for {chosen.get('name')}")

    vendor_dir = FIXTURE_ROOT / target.vendor
    vendor_dir.mkdir(parents=True, exist_ok=True)
    txt_path = vendor_dir / f"{target.invoice}.txt"
    meta_path = vendor_dir / f"{target.invoice}.meta.json"
    txt_path.write_text(chosen_text + "\n", encoding="utf-8")
    meta_path.write_text(
        json.dumps(message_meta(msg, chosen.get("name")), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return (f"  [ok]   {target.vendor}/{target.invoice}: "
            f"{len(chosen_text)} chars from {chosen.get('name')}")


def extract_notification_fixture(target: NotificationTarget, cache: DelegatedTokenCache) -> str:
    hits = search_messages(target.mailbox, target.query, cache, top=10)
    if target.sender_match:
        hits = [
            m for m in hits
            if target.sender_match.lower() in (
                ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
            ).lower()
        ]
    if not hits:
        return f"  [skip] {target.vendor}/{target.fixture_name}: no message matched"
    msg_summary = sorted(hits, key=lambda m: m.get("receivedDateTime") or "")[-1]
    msg = fetch_message_body(target.mailbox, msg_summary["id"], cache)
    body_html = (msg.get("body") or {}).get("content") or ""
    body_text = html_to_text(body_html)
    if "Vodafone" not in body_text and "vodafone" not in body_text:
        return (f"  [skip] {target.vendor}/{target.fixture_name}: body did not look "
                f"like a Vodafone notification (len={len(body_text)})")

    vendor_dir = FIXTURE_ROOT / target.vendor
    vendor_dir.mkdir(parents=True, exist_ok=True)
    txt_path = vendor_dir / f"{target.fixture_name}.txt"
    meta_path = vendor_dir / f"{target.fixture_name}.meta.json"
    txt_path.write_text(body_text, encoding="utf-8")
    meta_path.write_text(
        json.dumps(message_meta(msg, None), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return f"  [ok]   {target.vendor}/{target.fixture_name}: notification body, {len(body_text)} chars"


# ---------- DB fixture extraction ---------------------------------------------

def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"cannot json-encode {type(obj).__name__}")


def _redact_iban(value: str | None) -> str | None:
    if not value:
        return value
    return "DE**"


def dump_transactions(conn) -> str:
    ids = sorted(NAMED_TX_IDS)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM bank.transactions WHERE id = ANY(%s) "
            "OR beleg_match->>'kanban_task' = %s ORDER BY id",
            (ids, KANBAN_TX_TAG),
        )
        rows = cur.fetchall()
    out_path = FIXTURE_ROOT / "transactions.jsonl"
    lines = []
    for row in rows:
        row = dict(row)
        row["counterparty_iban"] = _redact_iban(row.get("counterparty_iban"))
        lines.append(json.dumps(row, default=_json_default, sort_keys=True))
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"  [ok]   transactions.jsonl: {len(rows)} rows ({len(NAMED_TX_IDS)} named + kanban)"


def dump_beleg_match_samples(conn) -> str:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, counterparty_name, signed_amount, booking_date, beleg_match "
            "FROM bank.transactions "
            "WHERE beleg_match IS NOT NULL "
            "ORDER BY (beleg_match->>'via'), id"
        )
        rows = cur.fetchall()

    # Bucket by via and pick a representative pack: all 3 manual_review verbatim,
    # then up to 2 of each other via to keep the file small.
    by_via: dict[str | None, list[dict]] = {}
    for r in rows:
        via = (r["beleg_match"] or {}).get("via") if isinstance(r["beleg_match"], dict) else None
        by_via.setdefault(via, []).append(r)

    chosen: list[dict] = []
    chosen.extend(by_via.get("manual_review", []))
    for via in ("outlook_auto_rule", "manual_inbox_match", "agent_match"):
        chosen.extend(by_via.get(via, [])[:2])

    out_path = FIXTURE_ROOT / "beleg_match_samples.jsonl"
    lines = [json.dumps(dict(r), default=_json_default, sort_keys=True) for r in chosen]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manual = sum(1 for r in chosen if (r["beleg_match"] or {}).get("via") == "manual_review")
    return (f"  [ok]   beleg_match_samples.jsonl: {len(chosen)} rows "
            f"({manual} manual_review verbatim)")


def dump_belege_sent_samples(conn) -> str:
    # Per #24: 5-10 rows covering each `via`, >=2 with bank_tx_id IS NULL,
    # >=2 with non-empty attachment_filenames.
    queries = [
        # 2 outlook_auto_rule with bank_tx_id IS NULL — covers the "null bank_tx" requirement
        ("outlook_auto_rule_null",
         "SELECT * FROM bank.belege_sent WHERE via='outlook_auto_rule' "
         "AND bank_tx_id IS NULL "
         "ORDER BY id LIMIT 2"),
        # 1 outlook_auto_rule with bank_tx_id AND attachments
        ("outlook_auto_rule_matched",
         "SELECT * FROM bank.belege_sent WHERE via='outlook_auto_rule' "
         "AND bank_tx_id IS NOT NULL "
         "AND coalesce(array_length(attachment_filenames,1),0) > 0 "
         "ORDER BY id LIMIT 1"),
        # 2 manual_inbox_match (always has attachments, always bank_tx_id)
        ("manual_inbox_match",
         "SELECT * FROM bank.belege_sent WHERE via='manual_inbox_match' "
         "ORDER BY id LIMIT 2"),
        # 2 agent_match
        ("agent_match",
         "SELECT * FROM bank.belege_sent WHERE via='agent_match' "
         "ORDER BY id LIMIT 2"),
        # 2 manual
        ("manual",
         "SELECT * FROM bank.belege_sent WHERE via='manual' ORDER BY id LIMIT 2"),
    ]
    chosen: list[dict] = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for _label, sql in queries:
            cur.execute(sql)
            chosen.extend(cur.fetchall())

    out_path = FIXTURE_ROOT / "belege_sent_samples.jsonl"
    chosen.sort(key=lambda r: r["id"])
    lines = [json.dumps(dict(r), default=_json_default, sort_keys=True) for r in chosen]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    null_tx = sum(1 for r in chosen if r.get("bank_tx_id") is None)
    with_att = sum(
        1 for r in chosen
        if (r.get("attachment_filenames") or []) and len(r["attachment_filenames"]) > 0
    )
    return (f"  [ok]   belege_sent_samples.jsonl: {len(chosen)} rows "
            f"(via counts cover all 4 values; null bank_tx_id={null_tx}; with_att={with_att})")


# ---------- Driver ------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-pdfs", action="store_true",
                        help="skip mailbox extraction; only refresh DB-side fixtures")
    parser.add_argument("--skip-db", action="store_true",
                        help="skip DB extraction; only refresh PDF fixtures")
    args = parser.parse_args()

    load_env_file()
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)

    print(f"Writing fixtures under {FIXTURE_ROOT.relative_to(REPO_ROOT)}")

    if not args.skip_pdfs:
        for var in ("LINEO_MS_TENANT_ID", "LINEO_MS_CLIENT_ID"):
            if not os.environ.get(var):
                sys.exit(f"{var} not set (load via ~/.hermes/.env)")
        if not TOKEN_FILE.exists():
            sys.exit(f"token bundle missing: {TOKEN_FILE}")
        cache = DelegatedTokenCache()
        print("PDF / notification fixtures:")
        for target in PDF_TARGETS:
            try:
                print(extract_pdf_fixture(target, cache))
            except Exception as e:  # noqa: BLE001 — fixture-builder, log + continue
                print(f"  [err]  {target.vendor}/{target.invoice}: {e}")
        for nt in NOTIFICATION_TARGETS:
            try:
                print(extract_notification_fixture(nt, cache))
            except Exception as e:  # noqa: BLE001
                print(f"  [err]  {nt.vendor}/{nt.fixture_name}: {e}")

    if not args.skip_db:
        url = os.environ.get("SUPABASE_DB_URL")
        if not url:
            sys.exit("SUPABASE_DB_URL not set")
        print("Database fixtures:")
        with psycopg2.connect(url) as conn:
            conn.set_session(readonly=True)
            print(dump_transactions(conn))
            print(dump_beleg_match_samples(conn))
            print(dump_belege_sent_samples(conn))

    return 0


if __name__ == "__main__":
    sys.exit(main())

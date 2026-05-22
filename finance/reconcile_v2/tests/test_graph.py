from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from pathlib import Path

import pytest

from finance.reconcile_v2.graph import LiveInboxClient


class _TokenProvider:
    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        return "test-token"


def _make_pdf_bytes() -> bytes:
    pytest.importorskip("pymupdf")
    import pymupdf  # type: ignore[import]

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "VM Finovia Rechnung 2026/1285 Betrag 11.923,80 EUR")
    data = doc.tobytes()
    doc.close()
    return data


def _make_signed_mime_with_pdf(pdf_bytes: bytes) -> bytes:
    mixed = EmailMessage()
    mixed.set_content("Guten Tag, anbei die Rechnung.")
    mixed.add_alternative("<p>Guten Tag, anbei die Rechnung.</p>", subtype="html")
    mixed.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename="RE_2026-1285.pdf",
    )

    signed = EmailMessage()
    signed.set_type("multipart/signed")
    signed.set_param("protocol", "application/pkcs7-signature")
    signed.set_param("micalg", "sha-256")
    signed.attach(mixed)

    sig = EmailMessage()
    sig.set_content(b"fake-signature", maintype="application", subtype="pkcs7-signature")
    sig.add_header("Content-Disposition", "attachment", filename="smime.p7s")
    signed.attach(sig)
    return signed.as_bytes()


def test_live_inbox_flattens_smime_p7m_and_extracts_inner_pdf(tmp_path: Path):
    smime_blob = _make_signed_mime_with_pdf(_make_pdf_bytes())

    def request(*, method: str, url: str, headers: dict[str, str], body: bytes | None):
        assert method == "GET"
        assert url.endswith("/messages/MSG-1/attachments")
        payload = {
            "value": [
                {
                    "name": "smime.p7m",
                    "contentType": "multipart/signed",
                    "size": len(smime_blob),
                    "contentBytes": base64.b64encode(smime_blob).decode("ascii"),
                }
            ]
        }
        return 200, json.dumps(payload).encode("utf-8")

    client = LiveInboxClient(
        token_provider=_TokenProvider(),
        blob_root=tmp_path,
        request=request,
    )

    attachments = client._fetch_attachments("catrin.stuecker@lineo.finance", "MSG-1")

    assert [a.name for a in attachments] == ["RE_2026-1285.pdf"]
    pdf = attachments[0]
    assert pdf.content_type == "application/pdf"
    assert pdf.local_path is not None
    assert Path(pdf.local_path).read_bytes().startswith(b"%PDF")
    assert pdf.extract_error is None
    assert "2026/1285" in (pdf.extracted_text or "")
    assert "11.923,80" in (pdf.extracted_text or "")

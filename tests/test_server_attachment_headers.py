"""Security headers on /attachments/{path} downloads."""
from __future__ import annotations

import httpx
import pytest

from obsidian_mcp import server
from obsidian_mcp.tools.attachments import attachment_download_headers


def _client():
    transport = httpx.ASGITransport(app=server.mcp.http_app())
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_download_sets_security_headers(method, tmp_path, vault_factory, monkeypatch):
    vault_factory({})
    monkeypatch.setenv("API_KEY", "test-key")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.txt").write_bytes(b"<script>alert(1)</script>")

    async with _client() as client:
        resp = await client.request(
            method, "/attachments/docs/page.txt", headers={"Authorization": "Bearer test-key"}
        )

    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == "sandbox"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert 'filename="page.txt"' in disposition
    assert "docs" not in disposition


def test_download_headers_sanitize_filename():
    headers = attachment_download_headers('dir/we"ird\\na;me\r\nX-Evil: 1 é.png')
    disposition = headers["Content-Disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    fallback = disposition.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in fallback and "\\" not in fallback and ";" not in fallback
    assert all(0x20 <= ord(ch) < 0x7F for ch in fallback)
    # the backslash is treated as a separator, so only the last segment remains
    assert fallback.startswith("na_me")
    assert "filename*=UTF-8''na%3Bme%0D%0AX-Evil%3A%201%20%C3%A9.png" in disposition


def test_download_headers_basename_only():
    headers = attachment_download_headers("a/b/c/file.pdf")
    assert headers["Content-Disposition"] == (
        "attachment; filename=\"file.pdf\"; filename*=UTF-8''file.pdf"
    )

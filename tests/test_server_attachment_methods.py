"""HTTP method handling for the /attachments/{path} route.

Starlette auto-adds HEAD to every GET route. The handler used to treat every
non-GET method as a write, so ``HEAD /attachments/x`` with a valid bearer
truncated the file to 0 bytes. HEAD must behave like GET without a body, and
every method other than GET/HEAD/PUT must be refused with 405.
"""
from __future__ import annotations

import httpx
import pytest
from starlette.requests import Request

from obsidian_mcp import server
from obsidian_mcp.tools.attachments import create_attachment_token


def _client():
    transport = httpx.ASGITransport(app=server.mcp.http_app())
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
async def test_head_does_not_modify_file(tmp_path, vault_factory, monkeypatch):
    vault_factory({})
    monkeypatch.setenv("API_KEY", "test-key")
    (tmp_path / "file.png").write_bytes(b"image-bytes")

    async with _client() as client:
        resp = await client.head(
            "/attachments/file.png", headers={"Authorization": "Bearer test-key"}
        )

    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-length"] == str(len(b"image-bytes"))
    assert (tmp_path / "file.png").read_bytes() == b"image-bytes"


@pytest.mark.asyncio
async def test_head_requires_auth(tmp_path, vault_factory, monkeypatch):
    vault_factory({})
    monkeypatch.setenv("API_KEY", "test-key")
    (tmp_path / "file.png").write_bytes(b"image-bytes")

    async with _client() as client:
        resp = await client.head("/attachments/file.png")

    assert resp.status_code == 401
    assert (tmp_path / "file.png").read_bytes() == b"image-bytes"


@pytest.mark.asyncio
async def test_head_missing_file_is_404_and_creates_nothing(tmp_path, vault_factory, monkeypatch):
    vault_factory({})
    monkeypatch.setenv("API_KEY", "test-key")

    async with _client() as client:
        resp = await client.head(
            "/attachments/missing.png", headers={"Authorization": "Bearer test-key"}
        )

    assert resp.status_code == 404
    assert not (tmp_path / "missing.png").exists()


@pytest.mark.asyncio
async def test_head_with_put_scoped_token_is_rejected(tmp_path, vault_factory, monkeypatch):
    """HEAD is a read: a scoped token minted for PUT must not authorize it."""
    monkeypatch.setenv("API_KEY", "master-key")
    vault_factory({})
    (tmp_path / "file.png").write_bytes(b"image-bytes")
    token = create_attachment_token(
        "file.png", signing_key="master-key", vault="default", method="PUT", expires_in=60
    )

    async with _client() as client:
        resp = await client.head(
            f"/attachments/file.png?exp={token['expires_at']}&sig={token['sig']}"
        )

    assert resp.status_code == 401
    assert (tmp_path / "file.png").read_bytes() == b"image-bytes"


@pytest.mark.asyncio
async def test_head_with_get_scoped_token_is_allowed(tmp_path, vault_factory, monkeypatch):
    monkeypatch.setenv("API_KEY", "master-key")
    vault_factory({})
    (tmp_path / "file.png").write_bytes(b"image-bytes")
    token = create_attachment_token(
        "file.png", signing_key="master-key", vault="default", method="GET", expires_in=60
    )

    async with _client() as client:
        resp = await client.head(
            f"/attachments/file.png?exp={token['expires_at']}&sig={token['sig']}"
        )

    assert resp.status_code == 200
    assert (tmp_path / "file.png").read_bytes() == b"image-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "DELETE", "PATCH", "OPTIONS"])
async def test_other_methods_are_405(method, tmp_path, vault_factory, monkeypatch):
    vault_factory({})
    monkeypatch.setenv("API_KEY", "test-key")
    (tmp_path / "file.png").write_bytes(b"image-bytes")

    async with _client() as client:
        resp = await client.request(
            method,
            "/attachments/file.png",
            content=b"",
            headers={"Authorization": "Bearer test-key"},
        )

    assert resp.status_code == 405
    assert (tmp_path / "file.png").read_bytes() == b"image-bytes"


@pytest.mark.asyncio
async def test_handler_rejects_unexpected_method_directly(tmp_path, vault_factory, monkeypatch):
    """Defence in depth: even if routing let another method through, the
    handler itself must refuse it rather than treat it as a write."""
    vault_factory({})
    monkeypatch.setenv("API_KEY", "test-key")
    (tmp_path / "file.png").write_bytes(b"image-bytes")

    scope = {
        "type": "http",
        "method": "DELETE",
        "path": "/attachments/file.png",
        "path_params": {"path": "file.png"},
        "headers": [(b"authorization", b"Bearer test-key")],
        "query_string": b"",
    }
    resp = await server.attachment_route(Request(scope))

    assert resp.status_code == 405
    assert (tmp_path / "file.png").read_bytes() == b"image-bytes"

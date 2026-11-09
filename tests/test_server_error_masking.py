"""mask_error_details: internals hidden, user-facing tool errors preserved."""
from __future__ import annotations

import errno

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from obsidian_mcp import server
from obsidian_mcp.config import ConfigError, mask_error_details_from_env
from obsidian_mcp.domain.index import IndexBuildingError
from obsidian_mcp.errors import UserFacingErrorMiddleware, user_facing_message
from obsidian_mcp.storage.policy import VaultPathError, WritePermissionError


def _probe_server() -> FastMCP:
    app = FastMCP(name="probe", mask_error_details=True)
    app.add_middleware(UserFacingErrorMiddleware())

    @app.tool()
    def denied() -> str:
        raise WritePermissionError("Write access denied for path 'private/x.md'")

    @app.tool()
    def missing() -> str:
        raise FileNotFoundError("Note not found: 'x.md'")

    @app.tool()
    def os_level() -> str:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", "/srv/vault/secret.md")

    @app.tool()
    def internal() -> str:
        raise RuntimeError("internal detail /srv/vault")

    @app.tool()
    def explicit() -> str:
        raise ToolError("explicit tool error")

    return app


def test_default_is_masked(monkeypatch):
    monkeypatch.delenv("MASK_ERROR_DETAILS", raising=False)
    assert mask_error_details_from_env() is True
    assert server.mcp._mask_error_details is True


@pytest.mark.parametrize(("value", "expected"), [("false", False), ("0", False), ("true", True)])
def test_env_override(monkeypatch, value, expected):
    monkeypatch.setenv("MASK_ERROR_DETAILS", value)
    assert mask_error_details_from_env() is expected


def test_invalid_env_value_raises(monkeypatch):
    monkeypatch.setenv("MASK_ERROR_DETAILS", "sometimes")
    with pytest.raises(ConfigError, match="MASK_ERROR_DETAILS"):
        mask_error_details_from_env()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "message"),
    [
        ("denied", "Write access denied for path 'private/x.md'"),
        ("missing", "Note not found: 'x.md'"),
        ("explicit", "explicit tool error"),
    ],
)
async def test_user_facing_errors_reach_client(tool, message):
    async with Client(_probe_server()) as client:
        with pytest.raises(ToolError, match=message):
            await client.call_tool(tool, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["internal"])
async def test_internal_errors_are_masked(tool):
    async with Client(_probe_server()) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(tool, {})
    assert "/srv/vault" not in str(excinfo.value)
    assert str(excinfo.value) == f"Error calling tool {tool!r}"


@pytest.mark.asyncio
async def test_os_errors_lose_their_filename():
    async with Client(_probe_server()) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("os_level", {})
    assert str(excinfo.value) == (
        "Error calling tool 'os_level': FileNotFoundError: No such file or directory"
    )


@pytest.mark.asyncio
async def test_unmasked_server_keeps_full_detail():
    app = FastMCP(name="probe-unmasked", mask_error_details=False)
    app.add_middleware(UserFacingErrorMiddleware())

    @app.tool()
    def internal() -> str:
        raise RuntimeError("internal detail")

    async with Client(app) as client:
        with pytest.raises(ToolError, match="internal detail"):
            await client.call_tool("internal", {})


def test_user_facing_message_classification():
    assert user_facing_message(ValueError("bad")) == "bad"
    assert user_facing_message(VaultPathError("bad path")) == "bad path"
    assert user_facing_message(PermissionError("denied")) == "denied"
    assert user_facing_message(IndexBuildingError("building")) == "building"
    # OS-generated: reduced to type + strerror, filename dropped
    assert (
        user_facing_message(PermissionError(errno.EACCES, "Permission denied", "/abs/x"))
        == "PermissionError: Permission denied"
    )
    assert user_facing_message(OSError(errno.EIO, "I/O error")) is None
    assert user_facing_message(RuntimeError("x")) is None
    assert user_facing_message(KeyError("x")) is None


@pytest.mark.asyncio
async def test_real_tool_policy_error_reaches_client(tmp_path, vault_factory, monkeypatch):
    """End to end on the real server: a policy denial keeps its message."""
    monkeypatch.setenv("WRITE_PATHS", "inbox/")
    index = vault_factory({})
    monkeypatch.setattr(server, "_index", index)

    async with Client(server.mcp) as client:
        with pytest.raises(ToolError, match="Write access denied"):
            await client.call_tool("write_note_tool", {"path": "other/x.md", "content": "hi"})
        with pytest.raises(ToolError, match="Note not found"):
            await client.call_tool("read_note_tool", {"path": "inbox/missing.md"})

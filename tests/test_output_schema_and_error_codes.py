"""One envelope output schema on every tool, and a stable `code` on every error."""
from __future__ import annotations

import errno
import importlib

import jsonschema
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

import obsidian_mcp.config as cfg_mod
import obsidian_mcp.server as server_mod
from obsidian_mcp.domain.index import IndexBuildingError
from obsidian_mcp.domain.models import PreconditionRequiredError, RevisionConflictError
from obsidian_mcp.envelope import ENVELOPE_SCHEMA, ERROR_CODES, envelope_output_schema
from obsidian_mcp.errors import UserFacingErrorMiddleware, error_code
from obsidian_mcp.ob import register_ob_tools
from obsidian_mcp.ob.settings import ObFlags
from obsidian_mcp.storage.policy import (
    InvalidFileTypeError,
    PermanentDeleteDisabledError,
    ProtectedPathError,
    ReadPermissionError,
    VaultPathError,
    WritePermissionError,
)

_FLAGS = (
    "ENABLE_CANVAS", "ENABLE_EXCALIDRAW", "ENABLE_KANBAN", "ENABLE_BASES",
    "ENABLE_MOVE", "ENABLE_FOLDER_RENAME", "ENABLE_BULK_REPLACE", "ENABLE_DELETE",
)
_STALE = "sha256:" + "0" * 64


def _validate(payload: dict) -> None:
    jsonschema.validate(payload, ENVELOPE_SCHEMA)


@pytest.fixture
def served(vault_factory, monkeypatch):
    def _make(files: dict[str, str] | None = None):
        index = vault_factory(files)
        monkeypatch.setattr(server_mod, "_index", index)
        return index

    return _make


async def _error(client: Client, tool: str, args: dict):
    result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.is_error is True
    _validate(result.structured_content)
    assert result.structured_content["success"] is False
    assert result.structured_content["data"] == {}
    assert result.structured_content["message"] == result.content[0].text
    return result


# ── A: output schema ─────────────────────────────────────────────────────────

def test_schema_is_an_object_envelope_with_every_code():
    assert ENVELOPE_SCHEMA["type"] == "object"
    assert ENVELOPE_SCHEMA["required"] == ["success", "data"]
    assert ENVELOPE_SCHEMA["properties"]["code"]["enum"] == list(ERROR_CODES)
    copy = envelope_output_schema()
    copy["properties"].clear()
    assert ENVELOPE_SCHEMA["properties"], "registrations get their own copy"


@pytest.mark.asyncio
async def test_every_note_tool_advertises_the_envelope_schema(monkeypatch):
    for name in _FLAGS:
        monkeypatch.setenv(name, "true")
    cfg_mod._config = None
    try:
        server = importlib.reload(server_mod)
        tools = await server.mcp.list_tools()
        assert len(tools) > 60
        for tool in tools:
            assert tool.output_schema == ENVELOPE_SCHEMA, tool.name
        async with Client(server.mcp) as client:
            listed = await client.list_tools()
        for tool in listed:
            assert tool.outputSchema == ENVELOPE_SCHEMA, tool.name
        titled = {t.name: t.title for t in tools}
        assert titled["patch_note_tool"] == "Edit note section"
        assert titled["move_note_tool"] == "Move or rename note"
    finally:
        for name in _FLAGS:
            monkeypatch.delenv(name, raising=False)
        cfg_mod._config = None
        importlib.reload(server_mod)


@pytest.mark.asyncio
async def test_ob_tools_advertise_the_envelope_schema():
    app = FastMCP(name="ob-probe")
    names = register_ob_tools(app, ObFlags(enable_ob=True, enable_ops=True, enable_admin=True))
    assert names
    for tool in await app.list_tools():
        assert tool.output_schema == ENVELOPE_SCHEMA, tool.name


@pytest.mark.asyncio
async def test_successful_calls_validate_against_the_schema(served):
    served({"a.md": "# A\nbody [[b]]\n", "b.md": "bee\n"})
    async with Client(server_mod.mcp) as client:
        schemas = {t.name: t.outputSchema for t in await client.list_tools()}
        calls = [
            ("read_note_tool", {"path": "a.md"}),
            ("list_notes_tool", {}),
            ("get_outgoing_links_tool", {"path": "a.md"}),
            ("get_vault_stats_tool", {}),
            ("write_note_tool", {"path": "c.md", "content": "new"}),
            ("append_to_note_tool", {"path": "b.md", "content": "more"}),
            ("batch_edit_tool", {"edits": [{"path": "b.md", "op": "append", "content": "x"}]}),
        ]
        for tool, args in calls:
            result = await client.call_tool(tool, args)
            assert result.is_error is False
            jsonschema.validate(result.structured_content, schemas[tool])
            assert result.structured_content["success"] is True
            assert "code" not in result.structured_content


# ── B: error codes ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (RevisionConflictError("n.md", _STALE, None), "revision_conflict"),
        (PreconditionRequiredError("n.md"), "precondition_required"),
        (PermanentDeleteDisabledError("off"), "permanent_delete_disabled"),
        (WritePermissionError("no"), "permission_denied"),
        (ReadPermissionError("no"), "permission_denied"),
        (ProtectedPathError("no"), "permission_denied"),
        (PermissionError(errno.EACCES, "Permission denied", "/x"), "permission_denied"),
        (FileNotFoundError("gone"), "not_found"),
        (FileExistsError("taken"), "already_exists"),
        (IsADirectoryError("dir"), "invalid_argument"),
        (VaultPathError("bad"), "invalid_argument"),
        (InvalidFileTypeError("bad"), "invalid_argument"),
        (ValueError("bad"), "invalid_argument"),
        (IndexBuildingError("building"), "unavailable"),
        (RuntimeError("boom"), "internal"),
        (None, "internal"),
    ],
)
def test_error_code_mapping(exc, code):
    assert error_code(exc) == code
    assert code in ERROR_CODES


@pytest.mark.asyncio
async def test_real_server_errors_carry_codes(served, monkeypatch):
    monkeypatch.setenv("WRITE_PATHS", "inbox/")
    served({"inbox/a.md": "a\n", "inbox/b.md": "b\n"})
    async with Client(server_mod.mcp) as client:
        missing = await _error(client, "read_note_tool", {"path": "inbox/missing.md"})
        assert missing.structured_content["code"] == "not_found"
        assert "Note not found" in missing.structured_content["message"]

        denied = await _error(client, "write_note_tool", {"path": "other/x.md", "content": "hi"})
        assert denied.structured_content["code"] == "permission_denied"
        assert "Write access denied" in denied.content[0].text

        taken = await _error(
            client, "copy_note_tool", {"from_path": "inbox/a.md", "to_path": "inbox/b.md"}
        )
        assert taken.structured_content["code"] == "already_exists"

        bad = await _error(client, "read_note_tool", {"path": "../outside.md"})
        assert bad.structured_content["code"] == "invalid_argument"

        # Argument validation by FastMCP itself.
        wrong_type = await _error(client, "list_notes_tool", {"include_meta": {"x": 1}})
        assert wrong_type.structured_content["code"] == "invalid_argument"

        # Raising clients still get the same message as before.
        with pytest.raises(ToolError, match="Note not found"):
            await client.call_tool("read_note_tool", {"path": "inbox/missing.md"})


@pytest.mark.asyncio
async def test_revision_conflict_keeps_its_payload_and_adds_a_code(served):
    served({"n.md": "first\n"})
    async with Client(server_mod.mcp) as client:
        result = await _error(
            client,
            "append_to_note_tool",
            {"path": "n.md", "content": "x", "expected_revision": _STALE},
        )
    payload = result.structured_content
    assert payload["code"] == "revision_conflict"
    assert payload["error"] == "revision_conflict"
    assert payload["path"] == "n.md"
    assert payload["expected_revision"] == _STALE
    assert payload["actual_revision"].startswith("sha256:")


@pytest.mark.asyncio
async def test_precondition_required_carries_its_code(served, monkeypatch):
    monkeypatch.setenv("REQUIRE_WRITE_PRECONDITIONS", "true")
    served({"n.md": "first\n"})
    async with Client(server_mod.mcp) as client:
        result = await _error(client, "write_note_tool", {"path": "n.md", "content": "x"})
    assert result.structured_content["code"] == "precondition_required"
    assert result.structured_content["error"] == "precondition_required"
    assert result.structured_content["path"] == "n.md"


@pytest.mark.asyncio
async def test_permanent_delete_disabled_carries_its_code(served):
    served({"n.md": "x\n"})
    app = FastMCP(name="delete-probe", mask_error_details=True)
    app.add_middleware(UserFacingErrorMiddleware())
    app.tool(output_schema=envelope_output_schema())(server_mod.delete_tool)
    async with Client(app) as client:
        result = await _error(client, "delete_tool", {"path": "n.md", "trash": False})
    assert result.structured_content["code"] == "permanent_delete_disabled"
    assert "Permanent deletion is disabled" in result.structured_content["message"]


class _RaisingMiddleware(Middleware):
    """Stands in for VaultResolutionMiddleware raising before the tool runs."""

    async def on_call_tool(self, context, call_next):
        if context.message.name == "middleware_denied":
            raise PermissionError("This identity does not have access to vault 'x'")
        if context.message.name == "middleware_internal":
            raise RuntimeError("secret /srv/vault")
        return await call_next(context)


def _probe(mask: bool) -> FastMCP:
    app = FastMCP(name="probe", mask_error_details=mask)
    app.add_middleware(UserFacingErrorMiddleware(mask_error_details=mask))
    app.add_middleware(_RaisingMiddleware())

    def tool(fn):
        return app.tool(output_schema=envelope_output_schema())(fn)

    @tool
    def internal() -> dict:
        raise RuntimeError("internal detail /srv/vault")

    @tool
    def os_level() -> dict:
        raise OSError(errno.EIO, "I/O error", "/srv/vault/x.md")

    @tool
    def building() -> dict:
        raise IndexBuildingError("index is still building")

    @tool
    def explicit() -> dict:
        raise ToolError("explicit tool error")

    @tool
    def explicit_from_value() -> dict:
        raise ToolError("bad filter") from ValueError("bad filter")

    @tool
    def middleware_denied() -> dict:
        return {"success": True, "data": {}}

    @tool
    def middleware_internal() -> dict:
        return {"success": True, "data": {}}

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["internal", "os_level", "middleware_internal"])
async def test_masked_errors_still_carry_a_code(tool):
    async with Client(_probe(mask=True)) as client:
        result = await _error(client, tool, {})
    assert result.structured_content["code"] == "internal"
    assert result.structured_content["message"] == f"Error calling tool {tool!r}"
    assert "/srv/vault" not in result.content[0].text


@pytest.mark.asyncio
async def test_unmasked_errors_keep_detail_and_code():
    async with Client(_probe(mask=False)) as client:
        internal = await _error(client, "internal", {})
        leaked = await _error(client, "middleware_internal", {})
    assert internal.structured_content["code"] == "internal"
    assert "internal detail" in internal.structured_content["message"]
    assert leaked.structured_content["code"] == "internal"
    assert "secret" in leaked.structured_content["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "code", "text"),
    [
        ("building", "unavailable", "Error calling tool 'building': index is still building"),
        ("explicit", "tool_error", "explicit tool error"),
        ("explicit_from_value", "invalid_argument", "bad filter"),
        ("middleware_denied", "permission_denied", "does not have access to vault"),
    ],
)
async def test_probe_errors_map_to_codes(tool, code, text):
    async with Client(_probe(mask=True)) as client:
        result = await _error(client, tool, {})
    assert result.structured_content["code"] == code
    assert text in result.content[0].text


@pytest.mark.asyncio
async def test_unknown_tool_is_not_turned_into_a_coded_result():
    async with Client(_probe(mask=True)) as client:
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("no_such_tool", {})

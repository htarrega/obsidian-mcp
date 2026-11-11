"""TOOLS_ALLOWLIST / TOOLS_DENYLIST and ENABLE_PROMPTS / ENABLE_RESOURCES."""
from __future__ import annotations

import importlib
import logging

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import obsidian_mcp.config as cfg_mod
import obsidian_mcp.server as server_mod

_ENV = (
    "TOOLS_ALLOWLIST",
    "TOOLS_DENYLIST",
    "ENABLE_PROMPTS",
    "ENABLE_RESOURCES",
    "ENABLE_OB",
    "ENABLE_OB_OPS",
    "ENABLE_OB_ADMIN",
    "ENABLE_CANVAS",
    "ENABLE_EXCALIDRAW",
    "ENABLE_KANBAN",
    "ENABLE_BASES",
    "ENABLE_MOVE",
    "ENABLE_FOLDER_RENAME",
    "ENABLE_BULK_REPLACE",
    "ENABLE_DELETE",
    "READ_ONLY",
)

# The default surface (no ENABLE_* flags, no TOOLS_* filters). Adding a tool
# is a deliberate API change: update this snapshot together with the docs.
DEFAULT_TOOLS = frozenset({
    "add_attachment_tool",
    "append_to_note_tool",
    "create_attachment_token_tool",
    "create_folder_tool",
    "create_from_template_tool",
    "find_similar_notes_tool",
    "get_audit_log_tool",
    "get_backlinks_tool",
    "get_broken_links_tool",
    "get_link_graph_tool",
    "get_orphans_tool",
    "get_periodic_note_tool",
    "get_tasks_tool",
    "get_vault_conventions_tool",
    "get_vault_stats_tool",
    "lint_schema_tool",
    "list_all_tags_tool",
    "list_attachments_tool",
    "list_files_tool",
    "list_folder_tool",
    "list_notes_tool",
    "list_templates_tool",
    "list_vaults_tool",
    "manage_tags_tool",
    "patch_frontmatter_tool",
    "patch_note_text_tool",
    "patch_note_tool",
    "query_notes_tool",
    "read_attachment_tool",
    "read_note_tool",
    "resolve_alias_tool",
    "search_notes_tool",
    "write_note_tool",
    # Added together with the exposure filters.
    "get_outline_tool",
    "get_outgoing_links_tool",
    "get_recent_notes_tool",
    "prepend_to_note_tool",
    "set_task_status_tool",
    # Copy and all-or-nothing batch edits.
    "batch_edit_tool",
    "copy_note_tool",
})


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    yield
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    cfg_mod._config = None
    importlib.reload(server_mod)


def _reload(monkeypatch, **env: str):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cfg_mod._config = None
    return importlib.reload(server_mod)


async def _surface(server) -> tuple[set[str], set[str], set[str]]:
    tools = {tool.name for tool in await server.mcp.list_tools()}
    prompts = {prompt.name for prompt in await server.mcp.list_prompts()}
    resources = {str(r.uri) for r in await server.mcp.list_resources()} | {
        t.uri_template for t in await server.mcp.list_resource_templates()
    }
    return tools, prompts, resources


async def test_without_filters_the_surface_is_unchanged(monkeypatch):
    server = _reload(monkeypatch)
    tools, prompts, resources = await _surface(server)

    assert tools == DEFAULT_TOOLS
    assert prompts == {"weekly_review", "daily_note"}
    assert resources == {"vault://stats", "vault://tags", "vault://notes/{path}"}
    assert server._exposure["removed_tools"] == []


async def test_allowlist_keeps_only_named_tools(monkeypatch):
    server = _reload(
        monkeypatch, TOOLS_ALLOWLIST=" read_note_tool, search_notes_tool ,get_outline_tool"
    )
    tools, prompts, _ = await _surface(server)

    assert tools == {"read_note_tool", "search_notes_tool", "get_outline_tool"}
    assert prompts == {"weekly_review", "daily_note"}, "tool filters leave prompts alone"


async def test_hidden_tools_cannot_be_called(monkeypatch, vault_factory):
    server = _reload(monkeypatch, TOOLS_ALLOWLIST="read_note_tool")
    vault_factory({"a.md": "x"})
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("write_note_tool", {"path": "b.md", "content": "y"})


async def test_denylist_applies_after_allowlist(monkeypatch):
    server = _reload(
        monkeypatch,
        TOOLS_ALLOWLIST="read_note_tool,write_note_tool",
        TOOLS_DENYLIST="write_note_tool",
    )
    tools, _, _ = await _surface(server)
    assert tools == {"read_note_tool"}


async def test_denylist_alone_removes_only_named_tools(monkeypatch):
    server = _reload(monkeypatch, TOOLS_DENYLIST="lint_schema_tool,manage_tags_tool")
    tools, _, _ = await _surface(server)
    assert tools == DEFAULT_TOOLS - {"lint_schema_tool", "manage_tags_tool"}


async def test_unknown_names_warn_but_do_not_crash(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="obsidian_mcp.server"):
        server = _reload(
            monkeypatch,
            TOOLS_ALLOWLIST="read_note_tool,no_such_tool,move_note_tool",
            TOOLS_DENYLIST="also_missing",
        )
    tools, _, _ = await _surface(server)

    assert tools == {"read_note_tool"}
    assert server._exposure["unknown_allowlist"] == ["move_note_tool", "no_such_tool"]
    assert server._exposure["unknown_denylist"] == ["also_missing"]
    assert "no_such_tool" in caplog.text
    assert "also_missing" in caplog.text


async def test_allowlist_covers_optional_groups_when_enabled(monkeypatch):
    server = _reload(
        monkeypatch, ENABLE_MOVE="true", TOOLS_ALLOWLIST="read_note_tool,move_note_tool"
    )
    tools, _, _ = await _surface(server)
    assert tools == {"read_note_tool", "move_note_tool"}


async def test_allowlist_filters_ob_tools(monkeypatch):
    server = _reload(
        monkeypatch,
        ENABLE_OB="true",
        ENABLE_OB_OPS="true",
        TOOLS_ALLOWLIST="read_note_tool,sync_status",
    )
    tools, _, _ = await _surface(server)

    assert tools == {"read_note_tool", "sync_status"}
    assert "sync_restart" in server._exposure["removed_tools"]
    assert server._ob_enabled is True, "the supervisor is still started by main()"


async def test_denylist_filters_ob_tools(monkeypatch):
    server = _reload(monkeypatch, ENABLE_OB="true", TOOLS_DENYLIST="sync_logs,ob_info")
    tools, _, _ = await _surface(server)
    assert {"sync_status", "list_remote_vaults"} <= tools
    assert tools.isdisjoint({"sync_logs", "ob_info"})


async def test_prompts_can_be_disabled(monkeypatch):
    server = _reload(monkeypatch, ENABLE_PROMPTS="false")
    tools, prompts, resources = await _surface(server)

    assert prompts == set()
    assert tools == DEFAULT_TOOLS
    assert resources, "resources are a separate switch"
    assert sorted(server._exposure["removed_prompts"]) == ["daily_note", "weekly_review"]


async def test_resources_can_be_disabled(monkeypatch):
    server = _reload(monkeypatch, ENABLE_RESOURCES="false")
    tools, prompts, resources = await _surface(server)

    assert resources == set()
    assert prompts == {"weekly_review", "daily_note"}
    assert tools == DEFAULT_TOOLS


def test_invalid_boolean_fails_loudly(monkeypatch):
    with pytest.raises(cfg_mod.ConfigError, match="ENABLE_PROMPTS"):
        _reload(monkeypatch, ENABLE_PROMPTS="maybe")


def _deploy_env() -> dict[str, str]:
    from pathlib import Path

    env: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / "deploy" / "env.example"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key] = value.strip().strip('"')
    return env


async def test_deploy_env_exposes_every_tool(monkeypatch):
    deploy = _deploy_env()
    flags = {name: deploy[name] for name in _ENV if name in deploy}
    server = _reload(monkeypatch, **flags)
    tools, prompts, resources = await _surface(server)

    assert server._exposure["unknown_allowlist"] == []
    assert server._exposure["removed_tools"] == []
    # Every optional group is on: delete, move, folder rename, bulk replace,
    # the plugin formats and every ob tier.
    assert {
        "delete_tool", "restore_tool", "list_trash_tool", "move_note_tool",
        "rename_folder_tool", "find_replace_in_vault_tool", "read_canvas_tool",
        "read_excalidraw_tool", "read_kanban_tool", "read_base_tool",
        "sync_status", "sync_restart", "sync_set_conflict_strategy",
    } <= tools
    assert tools >= DEFAULT_TOOLS
    assert prompts and resources


async def test_every_tool_declares_annotations(monkeypatch):
    server = _reload(
        monkeypatch,
        ENABLE_DELETE="true",
        ENABLE_MOVE="true",
        ENABLE_FOLDER_RENAME="true",
        ENABLE_BULK_REPLACE="true",
        ENABLE_CANVAS="true",
        ENABLE_EXCALIDRAW="true",
        ENABLE_KANBAN="true",
        ENABLE_BASES="true",
    )
    tools = await server.mcp.list_tools()
    assert len(tools) >= 60
    missing = [tool.name for tool in tools if tool.annotations is None]
    assert missing == []
    read_only = {tool.name for tool in tools if tool.annotations.readOnlyHint}
    assert {"read_note_tool", "search_notes_tool", "list_trash_tool"} <= read_only
    assert read_only.isdisjoint({"write_note_tool", "delete_tool", "append_to_note_tool"})
    assert all(tool.annotations.openWorldHint is False for tool in tools)
    destructive = {tool.name for tool in tools if tool.annotations.destructiveHint}
    assert {"delete_tool", "write_note_tool", "patch_note_text_tool"} <= destructive
    assert destructive.isdisjoint({"append_to_note_tool", "copy_note_tool", "restore_tool"})

"""The consolidated tool surface: merged read/tag tools, batch and delete
dispatch, and the guarantee that nothing advertises a tool that isn't there."""

from __future__ import annotations

import pytest

import obsidian_mcp.config as cfg_mod
from obsidian_mcp import server


@pytest.fixture
def served_vault(vault_factory, monkeypatch):
    """A vault whose index is registered with the server, so the module-level
    `_index` proxy resolves for tools that update it after a write."""
    def _make(files: dict[str, str] | None = None):
        index = vault_factory(files)
        monkeypatch.setattr(
            server, "_indices", {cfg_mod.get_config().default_vault_name: index}
        )
        return index

    return _make


# ── read_note_tool(mode=...) ──────────────────────────────────────────────

_NOTE = """---
status: active
tags: [alpha]
---
Body text here.

## Section One
more body ^block-a
"""


def test_read_note_full_mode_returns_body_and_revision(vault_factory):
    vault_factory({"note.md": _NOTE})
    result = server.read_note_tool("note.md")

    assert result["meta"]["mode"] == "full"
    assert result["path"] == "note.md"
    assert result["revision"].startswith("sha256:")
    assert "Body text here." in result["data"]["content"]
    assert result["data"]["frontmatter"]["status"] == "active"


def test_read_note_outline_mode_omits_body_but_keeps_structure(vault_factory):
    vault_factory({"note.md": _NOTE})
    result = server.read_note_tool("note.md", mode="outline")

    assert result["meta"]["mode"] == "outline"
    assert result["revision"].startswith("sha256:")
    assert "content" not in result["data"], "outline must not carry body text"
    assert [h["text"] for h in result["data"]["headings"]] == ["Section One"]
    assert result["data"]["frontmatter_keys"] == ["status", "tags"]
    assert result["data"]["line_count"] > 0


def test_read_note_rendered_mode_resolves_embeds(vault_factory):
    vault_factory({
        "target.md": "Embedded body",
        "note.md": "Before\n![[target]]\nAfter",
    })
    result = server.read_note_tool("note.md", mode="rendered")

    assert result["meta"] == {"mode": "rendered", "depth": 1}
    assert "Embedded body" in result["data"]["rendered"]


def test_read_note_rendered_depth_zero_leaves_embeds_alone(vault_factory):
    vault_factory({
        "target.md": "Embedded body",
        "note.md": "Before\n![[target]]\nAfter",
    })
    result = server.read_note_tool("note.md", mode="rendered", depth=0)

    assert "![[target]]" in result["data"]["rendered"]
    assert "Embedded body" not in result["data"]["rendered"]


def test_read_note_rejects_an_unknown_mode(vault_factory):
    vault_factory({"note.md": _NOTE})
    with pytest.raises(ValueError, match="Unknown mode"):
        server.read_note_tool("note.md", mode="outlin")


def test_the_merged_read_tool_replaced_its_three_registrations():
    assert not hasattr(server, "render_note_tool")
    assert not hasattr(server, "get_note_outline_tool")


# ── list_all_tags_tool(mode=...) ──────────────────────────────────────────

def test_list_all_tags_flat_mode_counts_notes(served_vault):
    served_vault({
        "a.md": "---\ntags: [konzept/python]\n---\nA",
        "b.md": "---\ntags: [konzept/python, ki]\n---\nB",
    })
    result = server.list_all_tags_tool()

    assert result["meta"]["mode"] == "flat"
    counts = {item["tag"]: item["count"] for item in result["data"]["items"]}
    assert counts == {"konzept/python": 2, "ki": 1}
    assert result["meta"]["count"] == 2


def test_list_all_tags_tree_mode_nests_by_slash(served_vault):
    served_vault({"a.md": "---\ntags: [konzept/python]\n---\nA"})
    result = server.list_all_tags_tool(mode="tree")

    assert result["meta"]["mode"] == "tree"
    assert "python" in result["data"]["tree"]["konzept"]
    assert "path" not in result, "a vault-wide read addresses no single path"


def test_list_all_tags_rejects_an_unknown_mode(served_vault):
    served_vault({})
    with pytest.raises(ValueError, match="Unknown mode"):
        server.list_all_tags_tool(mode="nested")


def test_the_merged_tag_tool_replaced_get_tag_tree_tool():
    assert not hasattr(server, "get_tag_tree_tool")


# ── the alias tools Phase 2 deleted ───────────────────────────────────────

@pytest.mark.parametrize(
    "removed",
    ["get_notes_by_tag_tool", "get_daily_note_tool", "get_note_history_tool"],
)
def test_alias_tools_are_gone(removed):
    assert not hasattr(server, removed)


# ── the instructions must describe the tools that actually exist ──────────

# ── patch_frontmatter_tool(path | paths) ──────────────────────────────────

def test_patch_frontmatter_single_path_returns_a_single_envelope(served_vault):
    served_vault({"a.md": "---\nstatus: inbox\n---\nA"})
    result = server.patch_frontmatter_tool("a.md", {"status": "active"})

    assert result["path"] == "a.md"
    assert result["revision"].startswith("sha256:")
    assert result["data"]["updated_keys"] == ["status"]
    assert "results" not in result["data"], "a single patch is not a batch"


def test_patch_frontmatter_paths_applies_the_same_updates_to_each(tmp_path, served_vault):
    served_vault({
        "a.md": "---\nstatus: inbox\n---\nA",
        "b.md": "---\nstatus: inbox\n---\nB",
    })
    result = server.patch_frontmatter_tool(
        paths=["a.md", "b.md"], updates={"status": "active"}
    )

    assert result["data"]["summary"] == {"total": 2, "succeeded": 2, "failed": 0}
    assert [item["path"] for item in result["data"]["results"]] == ["a.md", "b.md"]
    assert "status: active" in (tmp_path / "a.md").read_text()
    assert "status: active" in (tmp_path / "b.md").read_text()


def test_patch_frontmatter_paths_reports_partial_failure(tmp_path, served_vault):
    served_vault({"a.md": "---\nstatus: inbox\n---\nA"})
    result = server.patch_frontmatter_tool(
        paths=["a.md", "missing.md"], updates={"status": "active"}
    )

    assert result["success"] is True, "the batch ran; the failure is per item"
    assert result["data"]["summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    ok, failure = result["data"]["results"]
    assert ok["success"] is True
    assert failure == {
        "success": False,
        "path": "missing.md",
        "error": {"type": "FileNotFoundError", "message": failure["error"]["message"]},
    }
    # The good note was still written.
    assert "status: active" in (tmp_path / "a.md").read_text()


def test_patch_frontmatter_paths_honours_dry_run(tmp_path, served_vault):
    served_vault({"a.md": "---\nstatus: inbox\n---\nA"})
    result = server.patch_frontmatter_tool(
        paths=["a.md"], updates={"status": "active"}, dry_run=True
    )

    assert "status: active" in result["data"]["results"][0]["data"]["preview"]
    assert "status: inbox" in (tmp_path / "a.md").read_text()


def test_patch_frontmatter_requires_exactly_one_of_path_or_paths(served_vault):
    served_vault({"a.md": "---\nstatus: inbox\n---\nA"})
    with pytest.raises(ValueError, match="exactly one"):
        server.patch_frontmatter_tool(updates={"status": "active"})
    with pytest.raises(ValueError, match="exactly one"):
        server.patch_frontmatter_tool("a.md", paths=["a.md"], updates={"status": "x"})


def test_patch_frontmatter_rejects_expected_revision_with_paths(served_vault):
    served_vault({"a.md": "---\nstatus: inbox\n---\nA"})
    with pytest.raises(ValueError, match="expected_revision"):
        server.patch_frontmatter_tool(
            paths=["a.md"], updates={"status": "x"}, expected_revision="sha256:" + "a" * 64
        )


# ── delete_tool / restore_tool dispatch ───────────────────────────────────

def test_delete_tool_trashes_a_note(tmp_path, served_vault):
    served_vault({"a.md": "body"})
    result = server.delete_tool("a.md")

    assert result["meta"]["kind"] == "note"
    assert result["meta"]["action"] == "deleted"
    assert result["path"] == "a.md"
    assert result["data"]["trash"] is True
    assert not (tmp_path / "a.md").exists()
    assert (tmp_path / ".trash" / "a.md").exists()


def test_delete_tool_trashes_a_folder_with_its_subtree(tmp_path, served_vault):
    served_vault({"folder/a.md": "body", "folder/sub/b.md": "body"})
    result = server.delete_tool("folder")

    assert result["meta"]["kind"] == "folder"
    assert result["path"] == "folder"
    assert not (tmp_path / "folder").exists()
    assert (tmp_path / ".trash" / "folder" / "sub" / "b.md").exists()


def test_delete_mode_permanent_deletes_without_trash(tmp_path, served_vault, monkeypatch):
    monkeypatch.setenv("ALLOW_PERMANENT_DELETE", "true")
    monkeypatch.setenv("DELETE_MODE", "permanent")
    served_vault({"a.md": "body", "folder/b.md": "body"})

    assert server.delete_tool("a.md")["data"]["trash"] is False
    server.delete_tool("folder")
    assert not (tmp_path / "a.md").exists() and not (tmp_path / "folder").exists()
    assert not (tmp_path / ".trash").exists()


def test_delete_mode_permanent_still_honours_explicit_trash(tmp_path, served_vault, monkeypatch):
    monkeypatch.setenv("ALLOW_PERMANENT_DELETE", "true")
    monkeypatch.setenv("DELETE_MODE", "permanent")
    served_vault({"a.md": "body"})
    server.delete_tool("a.md", trash=True)
    assert (tmp_path / ".trash" / "a.md").exists()


def test_delete_mode_permanent_needs_allow_permanent_delete(tmp_path, served_vault, monkeypatch):
    monkeypatch.setenv("DELETE_MODE", "permanent")
    served_vault({"a.md": "body"})
    with pytest.raises(Exception, match="[Pp]ermanent deletion is disabled"):
        server.delete_tool("a.md")
    assert (tmp_path / "a.md").exists()


def test_delete_mode_rejects_unknown_values(served_vault, monkeypatch):
    monkeypatch.setenv("DELETE_MODE", "shred")
    served_vault({"a.md": "body"})
    with pytest.raises(ValueError, match="DELETE_MODE"):
        server.delete_tool("a.md")


def test_delete_tool_rejects_expected_revision_for_a_folder(served_vault):
    served_vault({"folder/a.md": "body"})
    with pytest.raises(ValueError, match="folder has no single"):
        server.delete_tool("folder", expected_revision="sha256:" + "a" * 64)


def test_delete_tool_honours_expected_revision_for_a_note(tmp_path, served_vault):
    served_vault({"a.md": "body"})
    revision = server.read_note_tool("a.md")["revision"]

    stale = "sha256:" + "0" * 64
    conflict = server.delete_tool("a.md", expected_revision=stale)
    assert conflict.is_error, "a stale revision must not delete the note"
    assert (tmp_path / "a.md").exists()

    assert server.delete_tool("a.md", expected_revision=revision)["success"] is True
    assert not (tmp_path / "a.md").exists()


def test_delete_tool_reports_a_missing_path(served_vault):
    served_vault({})
    with pytest.raises(FileNotFoundError):
        server.delete_tool("nope.md")


def test_restore_tool_round_trips_a_note(tmp_path, served_vault):
    served_vault({"a.md": "body"})
    server.delete_tool("a.md")

    result = server.restore_tool("a.md", "restored/a.md")

    assert result["meta"]["kind"] == "note"
    assert result["path"] == "restored/a.md"
    assert (tmp_path / "restored" / "a.md").read_text() == "body"


def test_restore_tool_round_trips_a_folder(tmp_path, served_vault):
    served_vault({"folder/a.md": "body"})
    server.delete_tool("folder")

    result = server.restore_tool("folder", "back")

    assert result["meta"]["kind"] == "folder"
    assert result["path"] == "back"
    assert (tmp_path / "back" / "a.md").read_text() == "body"


REMOVED_TOOLS = (
    # Phase 2 — true aliases.
    "get_notes_by_tag_tool",
    "get_daily_note_tool",
    "get_note_history_tool",
    # Phase 3 — folded into read_note_tool / list_all_tags_tool.
    "render_note_tool",
    "get_note_outline_tool",
    "get_tag_tree_tool",
    # Phase 4 — folded into patch_frontmatter_tool / delete_tool / restore_tool.
    "patch_frontmatter_batch_tool",
    "delete_note_tool",
    "delete_folder_tool",
    "restore_note_tool",
    "restore_folder_tool",
)


def _documented_tool_names() -> set[str]:
    import re
    return set(re.findall(r"`(\w+_tool)[(`]", server._DEFAULT_INSTRUCTIONS))


@pytest.mark.parametrize("removed", REMOVED_TOOLS)
def test_instructions_never_mention_a_removed_tool(removed):
    assert removed not in server._DEFAULT_INSTRUCTIONS


def test_instructions_only_mention_tools_that_exist():
    """The instructions are deliberately short (tool docstrings carry the
    details), but every tool they do name must be a real tool."""
    documented = _documented_tool_names()
    assert documented, "sanity: the instructions name a few starting tools"
    missing = {name for name in documented if not callable(getattr(server, name, None))}
    assert not missing, f"instructions mention unknown tools: {sorted(missing)}"


def test_instructions_are_short_and_convention_neutral():
    text = server._DEFAULT_INSTRUCTIONS
    assert len(text) < 4000
    assert "get_vault_conventions_tool" in text
    assert "expected_revision" in text
    for opinion in ("tags: [", "status: active", "Journal/", "category/sub"):
        assert opinion not in text


def test_every_registered_tool_has_a_docstring():
    registered = [
        getattr(server, name)
        for name in dir(server)
        if name.endswith("_tool") and callable(getattr(server, name))
    ]
    assert registered
    assert all((tool.__doc__ or "").strip() for tool in registered)


def test_layout_instructions_only_mention_configured_periodic_and_templates():
    from obsidian_mcp.layout import layout_from_env

    assert server._layout_instructions(layout_from_env({})) == ""
    configured = layout_from_env({
        "PERIODIC_DAILY_PATH": "Daily/{{date:YYYY-MM-DD}}.md",
        "TEMPLATES_DIR": "Meta/Templates",
    })
    text = server._layout_instructions(configured)
    assert "Daily/{{date:YYYY-MM-DD}}.md" in text
    assert "get_periodic_note_tool" in text
    assert "Meta/Templates/" in text
    assert "weekly" not in text

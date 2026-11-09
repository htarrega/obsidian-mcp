"""Verb-style tools: outline, outgoing links, prepend, task status, recent
notes and reading a note by name."""
from __future__ import annotations

import os
import time

import pytest
from fastmcp import Client

import obsidian_mcp.config as cfg_mod
from obsidian_mcp import server
from obsidian_mcp.storage.policy import WritePermissionError


@pytest.fixture
def served_vault(vault_factory, monkeypatch):
    def _make(files: dict[str, str] | None = None):
        index = vault_factory(files)
        monkeypatch.setattr(
            server, "_indices", {cfg_mod.get_config().default_vault_name: index}
        )
        return index

    return _make


# ── read_note_tool by name ────────────────────────────────────────────────

def test_read_note_by_bare_stem_case_insensitive(served_vault):
    served_vault({"(200) Notes/Docker Tips.md": "body"})
    result = server.read_note_tool("docker tips")

    assert result["path"] == "(200) Notes/Docker Tips.md"
    assert result["meta"]["resolved_from"] == "docker tips"
    assert result["data"]["content"] == "body"
    assert result["revision"].startswith("sha256:")


def test_read_note_by_exact_path_has_no_resolution_meta(served_vault):
    served_vault({"a/Note.md": "body"})
    result = server.read_note_tool("a/Note.md")

    assert result["path"] == "a/Note.md"
    assert "resolved_from" not in result["meta"]


def test_read_note_by_name_prefers_shortest_path_and_lists_others(served_vault):
    served_vault({"Same.md": "root", "deep/Same.md": "deep"})
    result = server.read_note_tool("Same")

    assert result["path"] == "Same.md"
    assert result["meta"]["candidates"] == ["deep/Same.md"]


def test_read_note_by_alias(served_vault):
    served_vault({"x/Long Name.md": "---\naliases: [LN]\n---\nbody"})
    assert server.read_note_tool("LN")["path"] == "x/Long Name.md"


def test_read_note_by_name_never_returns_an_attachment(served_vault, tmp_path):
    served_vault({})
    (tmp_path / "diagram.png").write_bytes(b"\x89PNG")
    with pytest.raises(FileNotFoundError):
        server.read_note_tool("diagram.png")


def test_read_note_unknown_name_raises(served_vault):
    served_vault({"a.md": "x"})
    with pytest.raises(FileNotFoundError, match="Note not found"):
        server.read_note_tool("nothing like it")


def test_read_note_by_name_respects_read_denials(served_vault, monkeypatch):
    monkeypatch.setenv("DENY_READ_PATHS", "secret/")
    served_vault({"secret/Hidden.md": "x"})
    with pytest.raises(FileNotFoundError):
        server.read_note_tool("Hidden")


# ── get_outline_tool ──────────────────────────────────────────────────────

def test_outline_lists_headings_with_lines_and_ignores_code(served_vault):
    served_vault({
        "w.md": "## 6 Nov\n- [ ] a\n```\n# not a heading\n```\n## 5 Nov\n### Detail\n",
    })
    result = server.get_outline_tool("w")

    assert result["path"] == "w.md"
    assert result["data"]["items"] == [
        {"level": 2, "text": "6 Nov", "line": 1},
        {"level": 2, "text": "5 Nov", "line": 6},
        {"level": 3, "text": "Detail", "line": 7},
    ]
    assert result["meta"]["count"] == 3
    assert result["revision"].startswith("sha256:")


def test_read_note_outline_mode_also_ignores_code_headings(served_vault):
    served_vault({"n.md": "```\n# code\n```\n## Real\n"})
    headings = server.read_note_tool("n.md", mode="outline")["data"]["headings"]
    assert [h["text"] for h in headings] == ["Real"]


# ── get_outgoing_links_tool ───────────────────────────────────────────────

def test_outgoing_links_resolve_and_report_broken_and_embeds(served_vault, tmp_path):
    served_vault({
        "Root.md": (
            "See [[Target]] and [[target|again]] and [[Target#Part]].\n"
            "![[pic.png]] [[Missing note]] ![[gone.pdf]]\n"
            "`[[In code]]`\n"
        ),
        "sub/Target.md": "t",
    })
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "pic.png").write_bytes(b"\x89PNG")

    result = server.get_outgoing_links_tool("Root.md")
    items = {(i["target"], i["heading"], i["embed"]): i for i in result["data"]["items"]}

    assert items[("Target", None, False)]["resolved"] == "sub/Target.md"
    assert items[("Target", None, False)]["count"] == 2
    assert items[("Target", "Part", False)]["kind"] == "note"
    assert items[("pic.png", None, True)] == {
        "target": "pic.png", "heading": None, "embed": True,
        "kind": "attachment", "resolved": "img/pic.png", "count": 1,
    }
    assert items[("Missing note", None, False)]["resolved"] is None
    assert items[("gone.pdf", None, True)]["kind"] == "attachment"
    assert ("In code", None, False) not in items
    assert result["meta"] == {"count": 5, "resolved": 3, "unresolved": 2}


def test_outgoing_links_hide_targets_outside_read_scope(served_vault, monkeypatch):
    monkeypatch.setenv("DENY_READ_PATHS", "secret/")
    served_vault({"a.md": "[[Hidden]]", "secret/Hidden.md": "x"})
    item = server.get_outgoing_links_tool("a.md")["data"]["items"][0]
    assert item["resolved"] is None


def test_backlinks_accept_a_note_name(served_vault):
    served_vault({"a.md": "[[Target]]", "x/Target.md": "t"})
    result = server.get_backlinks_tool("target")
    assert result["data"]["items"] == ["a.md"]
    assert result["meta"]["resolved_from"] == "target"


def test_backlinks_of_a_missing_note_still_work(served_vault):
    served_vault({"a.md": "[[Ghost]]"})
    assert server.get_backlinks_tool("Ghost")["data"]["items"] == ["a.md"]


# ── prepend_to_note_tool ──────────────────────────────────────────────────

def test_prepend_goes_to_top_of_a_plain_note(served_vault, tmp_path):
    served_vault({"log.md": "## 6 Nov\n- [x] old\n"})
    result = server.prepend_to_note_tool("log.md", "## 9 Nov\n- [ ] new")

    assert result["meta"]["action"] == "prepended"
    assert result["revision"].startswith("sha256:")
    assert (tmp_path / "log.md").read_text() == "## 9 Nov\n- [ ] new\n## 6 Nov\n- [x] old\n"


def test_prepend_goes_after_frontmatter(served_vault, tmp_path):
    served_vault({"n.md": "---\nstatus: x\n---\n\nBody\n"})
    server.prepend_to_note_tool("n.md", "First\n")
    assert (tmp_path / "n.md").read_text() == "---\nstatus: x\n---\n\nFirst\nBody\n"


def test_prepend_goes_after_a_leading_h1(served_vault, tmp_path):
    served_vault({"n.md": "---\na: 1\n---\n# Title\nBody\n"})
    server.prepend_to_note_tool("n.md", "New\n")
    assert (tmp_path / "n.md").read_text() == "---\na: 1\n---\n# Title\nNew\nBody\n"


def test_prepend_creates_missing_note_unless_disabled(served_vault, tmp_path):
    served_vault({})
    server.prepend_to_note_tool("new.md", "hello")
    assert (tmp_path / "new.md").read_text() == "hello\n"
    with pytest.raises(FileNotFoundError):
        server.prepend_to_note_tool("other.md", "x", create=False)


def test_prepend_with_stale_revision_is_refused(served_vault, tmp_path):
    served_vault({"n.md": "Body\n"})
    result = server.prepend_to_note_tool("n.md", "x", expected_revision="sha256:" + "0" * 64)
    assert result.is_error
    assert result.structured_content["error"] == "revision_conflict"
    assert (tmp_path / "n.md").read_text() == "Body\n"


def test_prepend_with_current_revision_succeeds_in_strict_mode(served_vault, monkeypatch, tmp_path):
    monkeypatch.setenv("REQUIRE_WRITE_PRECONDITIONS", "true")
    served_vault({"n.md": "Body\n"})
    revision = server.read_note_tool("n.md")["revision"]
    server.prepend_to_note_tool("n.md", "Top", expected_revision=revision)
    assert (tmp_path / "n.md").read_text() == "Top\nBody\n"


def test_prepend_respects_write_denials(served_vault, monkeypatch):
    monkeypatch.setenv("DENY_WRITE_PATHS", "sys/")
    served_vault({"sys/rules.md": "x"})
    with pytest.raises(WritePermissionError):
        server.prepend_to_note_tool("sys/rules.md", "y")


def test_prepend_is_audited(served_vault, tmp_path):
    served_vault({"n.md": "Body\n"})
    server.prepend_to_note_tool("n.md", "Top")
    entries = server.get_audit_log_tool(path="n.md")["data"]["items"]
    assert entries[0]["tool"] == "prepend_to_note_tool"


# ── set_task_status_tool ──────────────────────────────────────────────────

_TASKS = "---\na: 1\n---\n## 6 Nov\n- [ ] write report 📅 2026-11-10\n- [x] done thing\n```\n- [ ] code\n```\n"


def test_set_task_status_checks_a_task_by_file_line(served_vault, tmp_path):
    served_vault({"t.md": _TASKS})
    task = server.get_tasks_tool(status="open")["data"]["items"][0]
    assert task["line"] == 5

    result = server.set_task_status_tool("t.md", task["line"], True, task["text"])

    assert result["meta"]["action"] == "task_updated"
    assert result["data"] == {"line": 5, "text": "- [x] write report 📅 2026-11-10", "done": True}
    lines = (tmp_path / "t.md").read_text().split("\n")
    assert lines[4] == "- [x] write report 📅 2026-11-10"
    assert lines[5] == "- [x] done thing"
    entries = server.get_audit_log_tool(path="t.md")["data"]["items"]
    assert entries[0]["tool"] == "set_task_status_tool"


def test_set_task_status_unchecks(served_vault, tmp_path):
    served_vault({"t.md": _TASKS})
    server.set_task_status_tool("t.md", 6, False, "done thing")
    assert "- [ ] done thing" in (tmp_path / "t.md").read_text()


def test_set_task_status_already_in_state_writes_nothing(served_vault, tmp_path):
    served_vault({"t.md": _TASKS})
    before = (tmp_path / "t.md").stat().st_mtime_ns
    result = server.set_task_status_tool("t.md", 6, True, "done thing")
    assert result["meta"]["action"] == "unchanged"
    assert (tmp_path / "t.md").stat().st_mtime_ns == before


def test_set_task_status_refuses_text_mismatch(served_vault, tmp_path):
    served_vault({"t.md": _TASKS})
    with pytest.raises(ValueError, match="does not match expected_text"):
        server.set_task_status_tool("t.md", 5, True, "something else")
    assert (tmp_path / "t.md").read_text() == _TASKS


def test_set_task_status_refuses_non_task_and_code_lines(served_vault):
    served_vault({"t.md": _TASKS})
    with pytest.raises(ValueError, match="is not a task"):
        server.set_task_status_tool("t.md", 4, True, "6 Nov")
    with pytest.raises(ValueError, match="is not a task"):
        server.set_task_status_tool("t.md", 8, True, "code")
    with pytest.raises(ValueError, match="does not exist"):
        server.set_task_status_tool("t.md", 99, True, "x")
    with pytest.raises(ValueError, match="positive integer"):
        server.set_task_status_tool("t.md", 0, True, "x")


def test_set_task_status_stale_revision_is_refused(served_vault):
    served_vault({"t.md": _TASKS})
    result = server.set_task_status_tool(
        "t.md", 5, True, "write report", expected_revision="sha256:" + "0" * 64
    )
    assert result.is_error


def test_set_task_status_respects_write_denials(served_vault, monkeypatch):
    monkeypatch.setenv("READ_ONLY", "true")
    served_vault({"t.md": _TASKS})
    with pytest.raises(WritePermissionError):
        server.set_task_status_tool("t.md", 5, True, "write report")


def test_set_task_status_keeps_crlf_line_endings(served_vault, tmp_path):
    served_vault({})
    (tmp_path / "c.md").write_bytes(b"- [ ] a\r\n- [ ] b\r\n")
    server.set_task_status_tool("c.md", 2, True, "b")
    assert (tmp_path / "c.md").read_bytes() == b"- [ ] a\r\n- [x] b\r\n"


# ── get_recent_notes_tool ─────────────────────────────────────────────────

def test_recent_notes_sorted_by_mtime_and_skip_hidden(served_vault, tmp_path):
    served_vault({
        "old.md": "o",
        "new.md": "n",
        "sub/mid.md": "m",
        ".trash/t.md": "t",
        ".obsidian/x.md": "x",
        "pic.png": "not a note",
    })
    now = time.time()
    for rel, age in (("old.md", 300), ("sub/mid.md", 200), ("new.md", 100)):
        os.utime(tmp_path / rel, (now - age, now - age))

    result = server.get_recent_notes_tool()
    assert [i["path"] for i in result["data"]["items"]] == ["new.md", "sub/mid.md", "old.md"]
    assert result["data"]["items"][0]["mtime"].endswith("+00:00")

    assert [i["path"] for i in server.get_recent_notes_tool(limit=1)["data"]["items"]] == ["new.md"]
    assert [i["path"] for i in server.get_recent_notes_tool(folder="sub")["data"]["items"]] == [
        "sub/mid.md"
    ]
    with pytest.raises(ValueError):
        server.get_recent_notes_tool(limit=0)


# ── end to end through the MCP client ─────────────────────────────────────

@pytest.mark.asyncio
async def test_new_tools_are_callable_over_mcp(served_vault):
    served_vault({"Note.md": "## H\n- [ ] t\n"})
    async with Client(server.mcp) as client:
        outline = await client.call_tool("get_outline_tool", {"path": "note"})
        assert outline.structured_content["data"]["items"][0]["text"] == "H"
        done = await client.call_tool(
            "set_task_status_tool",
            {"path": "Note.md", "line": 2, "done": True, "expected_text": "t"},
        )
        assert done.structured_content["data"]["done"] is True

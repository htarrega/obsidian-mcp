"""copy_note, batch_edit and the all-or-nothing move_note."""
from __future__ import annotations

import pytest

import obsidian_mcp.config as cfg_mod
from obsidian_mcp.domain.models import PreconditionRequiredError, RevisionConflictError
from obsidian_mcp.storage.filesystem import VaultStorage
from obsidian_mcp.tools.write import batch_edit, copy_note, move_note, patch_note_text

# ── copy_note ─────────────────────────────────────────────────────────────


def test_copy_note_copies_bytes_and_keeps_source(tmp_path, vault_factory):
    idx = vault_factory({"a.md": "---\nk: v\n---\nBody [[x]]\n"})
    result = copy_note("a.md", "Archive/b.md", idx)
    assert result["status"] == "copied"
    assert (tmp_path / "Archive/b.md").read_text() == "---\nk: v\n---\nBody [[x]]\n"
    assert (tmp_path / "a.md").exists()
    assert "Archive/b.md" in idx.get_all_notes()
    assert result["name_conflicts"] == []


def test_copy_note_never_overwrites(tmp_path, vault_factory):
    vault_factory({"a.md": "new", "b.md": "keep"})
    with pytest.raises(FileExistsError):
        copy_note("a.md", "b.md")
    assert (tmp_path / "b.md").read_text() == "keep"


def test_copy_note_reports_same_name_and_dry_run_writes_nothing(tmp_path, vault_factory):
    vault_factory({"Notes/a.md": "x"})
    result = copy_note("Notes/a.md", "Old/a.md", dry_run=True)
    assert result["status"] == "dry_run"
    assert result["name_conflicts"] == ["Notes/a.md"]
    assert not (tmp_path / "Old/a.md").exists()


# ── batch_edit ────────────────────────────────────────────────────────────

NOTE = "# Title\n\n## Ideas\n\nold idea\n\n## Log\n\nentry\n"


def test_batch_edit_applies_ordered_edits_across_notes(tmp_path, vault_factory):
    vault_factory({"a.md": NOTE, "b.md": "hello world\n"})
    result = batch_edit([
        {"path": "a.md", "op": "section", "section": "## Ideas", "new_content": "new idea"},
        {"path": "a.md", "op": "append", "section": "Log", "content": "entry 2"},
        {"path": "a.md", "op": "replace", "find": "new idea", "replace": "newer idea"},
        {"path": "b.md", "op": "replace", "find": "world", "replace": "C:\\path"},
        {"path": "c.md", "op": "write", "content": "fresh\n"},
    ])
    assert result["status"] == "applied"
    a = (tmp_path / "a.md").read_text()
    assert "newer idea" in a and "old idea" not in a
    assert a.rstrip().endswith("entry 2")
    assert (tmp_path / "b.md").read_text() == "hello C:\\path\n"
    assert (tmp_path / "c.md").read_text() == "fresh\n"
    notes = {n["path"]: n for n in result["notes"]}
    assert notes["c.md"]["created"] is True and notes["c.md"]["revision"]


def test_batch_edit_failure_writes_nothing(tmp_path, vault_factory):
    vault_factory({"a.md": NOTE, "b.md": "b\n"})
    with pytest.raises(ValueError, match=r"edits\[2\].*nothing was written"):
        batch_edit([
            {"path": "a.md", "op": "replace", "find": "old idea", "replace": "x"},
            {"path": "new.md", "op": "write", "content": "n"},
            {"path": "b.md", "op": "section", "section": "Missing", "new_content": "y"},
        ])
    assert (tmp_path / "a.md").read_text() == NOTE
    assert (tmp_path / "b.md").read_text() == "b\n"
    assert not (tmp_path / "new.md").exists()


def test_batch_edit_rolls_back_when_a_write_fails(tmp_path, vault_factory, monkeypatch):
    vault_factory({"a.md": "a\n", "b.md": "b\n"})
    real = VaultStorage.write_text_atomic
    calls = {"n": 0}

    def flaky(self, path, content, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(self, path, content, **kwargs)

    monkeypatch.setattr(VaultStorage, "write_text_atomic", flaky)
    with pytest.raises(OSError):
        batch_edit([
            {"path": "a.md", "op": "write", "content": "A\n"},
            {"path": "b.md", "op": "write", "content": "B\n"},
        ])
    assert (tmp_path / "a.md").read_text() == "a\n"
    assert (tmp_path / "b.md").read_text() == "b\n"


def test_batch_edit_dry_run_and_revision_pin(tmp_path, vault_factory):
    vault_factory({"a.md": NOTE})
    preview = batch_edit(
        [{"path": "a.md", "op": "prepend", "content": "top"}], dry_run=True
    )
    assert preview["status"] == "dry_run"
    assert (tmp_path / "a.md").read_text() == NOTE
    revision = preview["notes"][0]["revision"]
    with pytest.raises(RevisionConflictError):
        batch_edit([{"path": "a.md", "op": "prepend", "content": "x", "expected_revision": "sha256:" + "0" * 64}])
    batch_edit([{"path": "a.md", "op": "prepend", "content": "top", "expected_revision": revision}])
    assert (tmp_path / "a.md").read_text().startswith("# Title\ntop\n")


def test_batch_edit_write_over_existing_needs_revision_in_strict_mode(vault_factory, monkeypatch):
    vault_factory({"a.md": "a"})
    monkeypatch.setenv("REQUIRE_WRITE_PRECONDITIONS", "true")
    cfg_mod._config = None
    with pytest.raises(PreconditionRequiredError):
        batch_edit([{"path": "a.md", "op": "write", "content": "b"}])


@pytest.mark.parametrize("edits", [[], [{"path": "a.md", "op": "nope"}], [{"op": "write"}]])
def test_batch_edit_rejects_bad_input(vault_factory, edits):
    vault_factory({"a.md": "a"})
    with pytest.raises(ValueError):
        batch_edit(edits)


# ── move_note ─────────────────────────────────────────────────────────────


def test_move_note_rewrites_folder_qualified_links(tmp_path, vault_factory):
    vault_factory({
        "Notes/old.md": "x",
        "l.md": "[[Notes/old]] and [[Notes/old.md|alias]] and ![[old#H]]",
    })
    result = move_note("Notes/old.md", "Archive/new.md")
    assert (tmp_path / "l.md").read_text() == "[[new]] and [[new|alias]] and ![[new#H]]"
    assert result["updated_links_in"] == ["l.md"]


def test_move_note_skips_notes_whose_links_do_not_change(tmp_path, vault_factory):
    vault_factory({"Notes/a.md": "x", "l.md": "[[a]]"})
    result = move_note("Notes/a.md", "Archive/a.md")
    assert result["updated_links_in"] == []


def test_move_note_dry_run_changes_nothing(tmp_path, vault_factory):
    vault_factory({"old.md": "x", "l.md": "[[old]] [[old]]"})
    result = move_note("old.md", "new.md", dry_run=True)
    assert result["links"] == [{"path": "l.md", "count": 2}]
    assert (tmp_path / "old.md").exists() and (tmp_path / "l.md").read_text() == "[[old]] [[old]]"


def test_move_note_moves_back_when_link_rewrite_fails(tmp_path, vault_factory, monkeypatch):
    vault_factory({"old.md": "x", "l1.md": "[[old]]", "l2.md": "[[old]]"})
    real = VaultStorage.write_text_atomic
    calls = {"n": 0}

    def flaky(self, path, content, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("boom")
        return real(self, path, content, **kwargs)

    monkeypatch.setattr(VaultStorage, "write_text_atomic", flaky)
    with pytest.raises(OSError):
        move_note("old.md", "new.md")
    assert (tmp_path / "old.md").exists() and not (tmp_path / "new.md").exists()
    assert (tmp_path / "l1.md").read_text() == "[[old]]"
    assert (tmp_path / "l2.md").read_text() == "[[old]]"


# ── literal replacement in exact mode ─────────────────────────────────────


def test_patch_note_text_exact_replacement_is_literal(tmp_path, vault_factory):
    vault_factory({"a.md": "path: X"})
    patch_note_text("a.md", "X", r"C:\new\1")
    assert (tmp_path / "a.md").read_text() == r"path: C:\new\1"


# ── second-review fixes ───────────────────────────────────────────────────


def test_move_note_refuses_a_name_already_in_use(tmp_path, vault_factory):
    vault_factory({"a.md": "x", "Other/b.md": "y"})
    with pytest.raises(FileExistsError, match="Other/b.md"):
        move_note("a.md", "Archive/b.md")
    assert (tmp_path / "a.md").exists()


def test_move_note_refuses_when_source_name_is_shared(tmp_path, vault_factory):
    vault_factory({"One/a.md": "x", "Two/a.md": "y", "l.md": "[[a]]"})
    with pytest.raises(ValueError, match="ambiguous"):
        move_note("One/a.md", "Archive/c.md")
    assert (tmp_path / "l.md").read_text() == "[[a]]"


def test_move_note_rewrites_self_links_and_more_link_forms(tmp_path, vault_factory):
    vault_factory({
        "old.md": "see [[old#Top]]",
        "l.md": "| [[old\\|alias]] | [[old.md]] |",
    })
    result = move_note("old.md", "new.md")
    assert (tmp_path / "new.md").read_text() == "see [[new#Top]]"
    assert (tmp_path / "l.md").read_text() == "| [[new\\|alias]] | [[new]] |"
    assert result["updated_links_in"] == ["l.md"]


def test_batch_edit_can_create_then_write_in_strict_mode(tmp_path, vault_factory, monkeypatch):
    vault_factory({})
    monkeypatch.setenv("REQUIRE_WRITE_PRECONDITIONS", "true")
    cfg_mod._config = None
    batch_edit([
        {"path": "n.md", "op": "append", "content": "one"},
        {"path": "n.md", "op": "write", "content": "two\n"},
    ])
    assert (tmp_path / "n.md").read_text() == "two\n"

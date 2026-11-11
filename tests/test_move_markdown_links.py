"""move_note rewrites Markdown links [text](dest) / ![alt](dest) too."""
from __future__ import annotations

import pytest

from obsidian_mcp.domain.markdown_links import iter_markdown_links
from obsidian_mcp.storage.filesystem import VaultStorage
from obsidian_mcp.tools.write import move_note

SRC = "(200) Notes/My note.md"
DST = "(300) Archive/Old note.md"


def _move(tmp_path, vault_factory, linker: str, *, at: str = "Linker.md", extra=None):
    files = {SRC: "body", at: linker, **(extra or {})}
    vault_factory(files)
    result = move_note(SRC, DST)
    return (tmp_path / at).read_text(), result


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # percent-encoded, full path, with .md
        ("[a](%28200%29%20Notes/My%20note.md)", "[a](%28300%29%20Archive/Old%20note.md)"),
        ("[a]((200)%20Notes/My%20note.md)", "[a]((300)%20Archive/Old%20note.md)"),
        ("[a](<(200) Notes/My note.md>)", "[a](<(300) Archive/Old note.md>)"),
        # without .md stays without .md
        ("[a]((200)%20Notes/My%20note)", "[a]((300)%20Archive/Old%20note)"),
        # bare name stays a bare name
        ("[a](My%20note.md)", "[a](Old%20note.md)"),
        # subpaths and titles are kept as written
        ("[a](My%20note.md#Some%20heading)", "[a](Old%20note.md#Some%20heading)"),
        ("[a](My%20note.md#^blk)", "[a](Old%20note.md#^blk)"),
        ('[a](My%20note.md "Title")', '[a](Old%20note.md "Title")'),
        # embeds, case-insensitive match, two links on a line, tables
        ("![img](my%20NOTE.md)", "![img](Old%20note.md)"),
        ("[a](My%20note.md) and [b](<My note.md>)", "[a](Old%20note.md) and [b](<Old note.md>)"),
        ("| [a](My%20note.md) | x |", "| [a](Old%20note.md) | x |"),
    ],
)
def test_markdown_link_forms(tmp_path, vault_factory, before, after):
    text, result = _move(tmp_path, vault_factory, before)
    assert text == after
    assert result["updated_links_in"] == ["Linker.md"]


def test_relative_link_from_another_folder(tmp_path, vault_factory):
    text, _ = _move(
        tmp_path, vault_factory, "[a](../(200)%20Notes/My%20note.md)", at="(400) Personal/L.md"
    )
    assert text == "[a](../(300)%20Archive/Old%20note.md)"


@pytest.mark.parametrize(
    "untouched",
    [
        "[a](My%20note%202.md)",  # a different note with the same prefix
        "[a](https://example.com/My%20note.md)",
        "[a](mailto:x@example.com)",
        "[a](#My%20note)",
        "```\n[a](My%20note.md)\n```",
        "`[a](My%20note.md)`",
        "%% [a](My%20note.md) %%",
        "---\nsee: \"[a](My%20note.md)\"\n---\nbody",
        "[a]: My%20note.md",
    ],
)
def test_things_that_are_not_rewritten(tmp_path, vault_factory, untouched):
    text, _ = _move(
        tmp_path, vault_factory, untouched, extra={"(200) Notes/My note 2.md": "other"}
    )
    assert text == untouched


def test_wikilinks_in_code_are_left_alone(tmp_path, vault_factory):
    text, _ = _move(tmp_path, vault_factory, "```\n[[My note]]\n```\n[[My note]]")
    assert text == "```\n[[My note]]\n```\n[[Old note]]"


def test_reference_definitions_are_reported(tmp_path, vault_factory):
    _, result = _move(tmp_path, vault_factory, "[x][r]\n\n[r]: My%20note.md")
    assert result["reference_links_not_updated"] == ["Linker.md"]


def test_moved_note_relative_links_follow_the_new_folder(tmp_path, vault_factory):
    vault_factory({
        SRC: "![i](../img/a.png) [s](./Sibling.md) [self](./My%20note.md#H) [w](https://x.org/a)",
        "img/a.png": "png",
        "(200) Notes/Sibling.md": "s",
    })
    move_note(SRC, DST)
    assert (tmp_path / DST).read_text() == (
        "![i](../img/a.png) [s](../(200)%20Notes/Sibling.md) "
        "[self](./Old%20note.md#H) [w](https://x.org/a)"
    )


def test_rename_in_place_keeps_own_relative_links(tmp_path, vault_factory):
    vault_factory({SRC: "[s](./Sibling.md)", "(200) Notes/Sibling.md": "s"})
    move_note(SRC, "(200) Notes/Renamed.md")
    assert (tmp_path / "(200) Notes/Renamed.md").read_text() == "[s](./Sibling.md)"


def test_dry_run_counts_both_kinds(tmp_path, vault_factory):
    vault_factory({SRC: "x", "L.md": "[[My note]] [a](My%20note.md) [b](<My note.md>)"})
    result = move_note(SRC, DST, dry_run=True)
    assert result["links"] == [{"path": "L.md", "count": 3, "wikilinks": 1, "markdown": 2}]
    assert (tmp_path / SRC).exists()


def test_rollback_restores_markdown_links(tmp_path, vault_factory, monkeypatch):
    vault_factory({SRC: "x", "A.md": "[a](My%20note.md)", "B.md": "[b](<My note.md>)"})
    real = VaultStorage.write_text_atomic
    calls = {"n": 0}

    def flaky(self, path, content, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("boom")
        return real(self, path, content, **kwargs)

    monkeypatch.setattr(VaultStorage, "write_text_atomic", flaky)
    with pytest.raises(OSError):
        move_note(SRC, DST)
    assert (tmp_path / SRC).exists() and not (tmp_path / DST).exists()
    assert (tmp_path / "A.md").read_text() == "[a](My%20note.md)"
    assert (tmp_path / "B.md").read_text() == "[b](<My note.md>)"


def test_scanner_spans_and_flags():
    text = 'x ![e](<a b.md> "t") [l]((1)%20c.md#^k)'
    links = list(iter_markdown_links(text))
    assert [(text[link.start:link.end], link.path, link.subpath, link.angle, link.embed) for link in links] == [
        ("<a b.md>", "a b.md", "", True, True),
        ("(1)%20c.md#^k", "(1) c.md", "#^k", False, False),
    ]


@pytest.mark.parametrize(
    "text",
    [
        # Shapes found in the real vault: none of them is an internal link.
        "this [document]([Objetivos_2026_Core.xlsx](https://a.sharepoint.com/x?e=1)) w",
        "[link]([Status ONNX Grabación.mp4](https://a.sharepoint.com/Status%20ONNX))",
        "[here](C:\\Users\\hugo\\OneDrive - FACEPHI\\Docs). Nacho",
        "[this one](thttps://www.bigocheatsheet.com/) .",
        "ranges::sort(v, [](auto& x, auto& y) {",
    ],
)
def test_real_vault_shapes_are_not_internal_links(text):
    assert list(iter_markdown_links(text)) == []

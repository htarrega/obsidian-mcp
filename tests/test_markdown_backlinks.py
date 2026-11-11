"""Inline Markdown links [text](dest) / ![alt](dest) count as links for
backlinks, outgoing links, orphans and broken-link reports."""
from __future__ import annotations

from obsidian_mcp.domain.index import markdown_link_keys
from obsidian_mcp.tools.query import (
    get_backlinks,
    get_broken_links,
    get_orphans,
    get_outgoing_links,
)


def test_backlink_via_percent_encoded_link(vault_factory):
    idx = vault_factory({
        "(200) Notes/My note.md": "Target.",
        "Source.md": "See [it](%28200%29%20Notes/My%20note.md).",
    })
    assert get_backlinks("(200) Notes/My note.md", idx) == ["Source.md"]


def test_backlink_via_angle_bracket_link(vault_factory):
    idx = vault_factory({
        "Folder/My note.md": "Target.",
        "Source.md": "See [it](<Folder/My note.md>).",
    })
    assert get_backlinks("Folder/My note.md", idx) == ["Source.md"]


def test_backlink_via_parent_relative_link(vault_factory):
    idx = vault_factory({
        "A/Target.md": "Target.",
        "B/Sub/Source.md": "Up: [t](../../A/Target.md)",
        "C/Target.md": "Same name, different folder.",
    })
    assert get_backlinks("A/Target.md", idx) == ["B/Sub/Source.md"]
    items = get_outgoing_links("B/Sub/Source.md", idx)
    assert len(items) == 1
    assert items[0]["resolved"] == "A/Target.md"
    assert items[0]["format"] == "markdown"


def test_backlink_via_relative_link_without_dot_prefix(vault_factory):
    idx = vault_factory({
        "Docs/Sub/Page.md": "Target.",
        "Docs/Index.md": "[page](Sub/Page.md)",
    })
    assert get_backlinks("Docs/Sub/Page.md", idx) == ["Docs/Index.md"]


def test_outgoing_counts_markdown_attachment_embed(vault_factory):
    idx = vault_factory({
        "images/diagram one.png": "png",
        "Note.md": "[[Other]]\n\n![diagram](images/diagram%20one.png)",
        "Other.md": "x",
    })
    items = get_outgoing_links("Note.md", idx)
    by_format = {item["format"]: item for item in items}
    assert set(by_format) == {"wikilink", "markdown"}
    embed = by_format["markdown"]
    assert embed["target"] == "images/diagram one.png"
    assert embed["embed"] is True
    assert embed["kind"] == "attachment"
    assert embed["resolved"] == "images/diagram one.png"
    # Existing fields of wikilink items are unchanged.
    assert by_format["wikilink"]["target"] == "Other"
    assert by_format["wikilink"]["resolved"] == "Other.md"


def test_outgoing_markdown_heading_and_count(vault_factory):
    idx = vault_factory({
        "Other.md": "# Part",
        "Note.md": "[a](Other.md#Part) and [b](Other.md#Part)",
    })
    (item,) = get_outgoing_links("Note.md", idx)
    assert item["heading"] == "Part"
    assert item["count"] == 2


def test_url_is_not_a_link(vault_factory):
    idx = vault_factory({
        "Note.md": "[site](https://example.com/Other.md) [mail](mailto:a@b.c)",
        "Other.md": "x",
    })
    assert get_outgoing_links("Note.md", idx) == []
    assert get_backlinks("Other.md", idx) == []
    assert get_broken_links(idx) == []


def test_link_in_code_is_not_a_link(vault_factory):
    idx = vault_factory({
        "Note.md": "`[a](Other.md)`\n\n```\n[b](Missing.md)\n```\n",
        "Other.md": "x",
    })
    assert get_outgoing_links("Note.md", idx) == []
    assert get_backlinks("Other.md", idx) == []
    assert get_broken_links(idx) == []


def test_markdown_only_link_is_not_orphan(vault_factory):
    idx = vault_factory({
        "Hub.md": "[child](Child.md)",
        "Child.md": "Only linked via Markdown.",
        "Lonely.md": "Nobody links here.",
    })
    orphans = get_orphans(idx)
    assert "Child.md" not in orphans
    assert "Lonely.md" in orphans


def test_broken_markdown_link_reported(vault_factory):
    idx = vault_factory({
        "Note.md": "[[Exists]] [gone](Missing%20note.md) [ok](Exists.md)",
        "Exists.md": "x",
    })
    broken = get_broken_links(idx)
    assert broken == [{
        "source": "Note.md",
        "link": "Missing note.md",
        "kind": "note",
        "suggestion": None,
        "format": "markdown",
    }]


def test_broken_wikilink_items_keep_their_fields(vault_factory):
    idx = vault_factory({"Note.md": "[[Missing]]"})
    (item,) = get_broken_links(idx)
    assert item["link"] == "Missing"
    assert item["format"] == "wikilink"


def test_backlinks_update_when_markdown_link_removed(vault_factory, tmp_path):
    idx = vault_factory({
        "Source.md": "[t](Target.md)",
        "Target.md": "x",
    })
    assert get_backlinks("Target.md", idx) == ["Source.md"]
    (tmp_path / "Source.md").write_text("no links", encoding="utf-8")
    idx.update("Source.md")
    assert get_backlinks("Target.md", idx) == []


def test_markdown_link_keys():
    assert markdown_link_keys("../A/Note.md", "B/C/Src.md") == {"B/A/Note"}
    assert markdown_link_keys("./Note.md", "B/Src.md") == {"B/Note"}
    assert markdown_link_keys("/A/Note.md", "B/Src.md") == {"A/Note"}
    assert markdown_link_keys("A/Note.md", "B/Src.md") == {"A/Note", "B/A/Note"}
    assert markdown_link_keys("img.png", "Src.md") == {"img.png"}

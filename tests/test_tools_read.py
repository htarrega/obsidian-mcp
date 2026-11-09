from __future__ import annotations

import pytest

from obsidian_mcp.tools.read import (
    get_note_outline,
    list_notes,
    read_note,
    render_note,
    search_notes,
)

# ── list_notes ──────────────────────────────────────────────────────────────

def test_list_notes_with_meta(vault_factory):
    vault_factory({"note.md": "---\ntitle: My Note\ntags: [foo]\nstatus: active\n---\nContent"})
    results = list_notes(include_meta=True)
    assert len(results) == 1
    assert results[0]["path"] == "note.md"
    assert results[0]["title"] == "My Note"
    assert "foo" in results[0]["tags"]
    assert results[0]["status"] == "active"


# ── read_note ─────────────────────────────────────────────────────────────

def test_read_note_returns_structured_data(vault_factory):
    vault_factory({
        "note.md": (
            "---\ntitle: My Note\ntags: [foo, bar]\naliases: [MN]\n---\n"
            "# My Note\n\nSome body text.\n\n- [ ] A task\n\nLinks to [[other]].\n"
        )
    })
    result = read_note("note.md")
    assert result["frontmatter"]["title"] == "My Note"
    assert "foo" in result["tags"]
    assert "bar" in result["tags"]
    assert any(wl["target"] == "other" for wl in result["wikilinks"])
    assert any(t["text"] == "A task" for t in result["tasks"])


def test_read_note_missing_raises(vault_factory):
    vault_factory({})
    with pytest.raises(FileNotFoundError):
        read_note("nonexistent.md")


def test_read_note_includes_inline_fields(vault_factory):
    vault_factory({"note.md": "# Title\n\nrating:: 8\nauthor:: Jane\n\nOther text."})
    result = read_note("note.md")
    assert result["inline_fields"]["rating"] == "8"
    assert result["inline_fields"]["author"] == "Jane"


def test_inline_fields_not_in_frontmatter(vault_factory):
    vault_factory({"note.md": "---\ntitle: Test\n---\nrating:: 9\n"})
    result = read_note("note.md")
    assert "rating" not in result["frontmatter"]
    assert result["inline_fields"]["rating"] == "9"


def test_inline_fields_empty_when_none(vault_factory):
    vault_factory({"note.md": "# Just a title\nNo inline fields here."})
    result = read_note("note.md")
    assert result["inline_fields"] == {}


def test_inline_fields_multi_word_key(vault_factory):
    vault_factory({"note.md": "due date:: 2026-07-30\n"})
    result = read_note("note.md")
    assert result["inline_fields"]["due date"] == "2026-07-30"


# ── search_notes ──────────────────────────────────────────────────────────

def test_search_returns_snippets(vault_factory):
    vault_factory({"note.md": "Line 1\nThis has the keyword here\nLine 3\n"})
    results = search_notes("keyword")
    assert len(results) == 1
    assert results[0]["path"] == "note.md"
    assert results[0]["score"] > 0
    assert len(results[0]["snippets"]) > 0
    assert results[0]["snippets"][0]["line"] == 2


def test_search_ranking(vault_factory):
    vault_factory({
        "exact.md": "python",
        "phrase.md": "I love python programming",
        "substring.md": "pythonista",
    })
    results = search_notes("python")
    scores = {r["path"]: r["score"] for r in results}
    assert scores.get("exact.md", 0) >= scores.get("substring.md", 0)


def test_search_regex_mode(vault_factory):
    vault_factory({"note.md": "foo123bar\nbaz456qux\n"})
    results = search_notes(r"foo\d+bar", mode="regex")
    assert len(results) == 1


def test_search_tag_filter(vault_factory):
    vault_factory({
        "tagged.md": "---\ntags: [python]\n---\nsome python content",
        "untagged.md": "some python content",
    })
    results = search_notes("python", tag="python")
    assert len(results) == 1
    assert results[0]["path"] == "tagged.md"


def test_search_fuzzy_finds_typo(vault_factory):
    vault_factory({"note.md": "Python is a great programming language."})
    results = search_notes("Pythn", mode="fuzzy")
    assert len(results) == 1
    assert results[0]["path"] == "note.md"


def test_search_fuzzy_no_false_positive(vault_factory):
    vault_factory({"note.md": "Completely unrelated text."})
    results = search_notes("quantum", mode="fuzzy")
    assert len(results) == 0


def test_search_fuzzy_multi_word(vault_factory):
    vault_factory({"note.md": "machine learning is powerful."})
    results = search_notes("machne lerning", mode="fuzzy")
    assert len(results) == 1


def test_search_fuzzy_threshold_stricter_excludes_loose_match(vault_factory):
    vault_factory({"note.md": "Warenkorb ist leer."})
    # "Warenträger" is loosely similar to "Warenkorb" — a low threshold
    # matches it, a high one shouldn't.
    assert len(search_notes("Warenträger", mode="fuzzy", threshold=0.5)) == 1
    assert len(search_notes("Warenträger", mode="fuzzy", threshold=0.95)) == 0


def test_search_field_filename_only_matches_md_files_by_name(vault_factory):
    vault_factory({
        "lockdown.md": "irrelevant body",
        "other.md": "the word lock appears here in the body",
    })
    results = search_notes("lock", field="filename")
    assert [r["path"] for r in results] == ["lockdown.md"]


def test_search_frontmatter_filter_combines_with_text(vault_factory):
    vault_factory({
        "a.md": "---\ntype: ticket\n---\nSortierfolge muss stimmen",
        "b.md": "---\ntype: note\n---\nSortierfolge auch hier erwähnt",
    })
    results = search_notes("Sortierfolge", frontmatter_filter={"type": "ticket"})
    assert [r["path"] for r in results] == ["a.md"]


def test_search_frontmatter_filter_operator(vault_factory):
    vault_factory({
        "a.md": "---\nstatus: active\n---\nkeyword here",
        "b.md": "---\nstatus: done\n---\nkeyword here too",
    })
    results = search_notes("keyword", frontmatter_filter={"status": {"$ne": "done"}})
    assert [r["path"] for r in results] == ["a.md"]


def test_search_is_accent_insensitive(vault_factory):
    vault_factory({"a.md": "Reunión con Siplán mañana\n", "b.md": "nothing here\n"})
    assert [r["path"] for r in search_notes("Siplan")] == ["a.md"]
    assert [r["path"] for r in search_notes("siplán")] == ["a.md"]
    assert [r["path"] for r in search_notes("reunion con siplan")] == ["a.md"]
    # Folding also applies the other way round and in fuzzy mode.
    vault_factory({"c.md": "Siplan sin tilde\n"})
    assert sorted(r["path"] for r in search_notes("Siplán")) == ["a.md", "c.md"]
    assert sorted(r["path"] for r in search_notes("Siplán", mode="fuzzy")) == ["a.md", "c.md"]


def test_search_accent_insensitive_keeps_original_text(vault_factory):
    vault_factory({"a.md": "Siplán\n"})
    snippet = search_notes("siplan")[0]["snippets"][0]
    assert snippet["context"][0]["text"] == "Siplán"
    assert snippet["context"][0]["match"] is True


def test_search_defaults_two_snippets_one_context_line(vault_factory):
    body = "\n".join(
        ["hit"] + ["filler"] * 5 + ["hit"] + ["filler"] * 5 + ["hit"] + ["filler"] * 5
    )
    vault_factory({"n.md": body})
    result = search_notes("hit")[0]
    assert len(result["snippets"]) == 2
    assert [c["line"] for c in result["snippets"][0]["context"]] == [1, 2]
    assert [c["line"] for c in result["snippets"][1]["context"]] == [6, 7, 8]
    # Score still counts every matching line, not only the returned ones.
    assert result["score"] == 9  # 3 exact-line matches x 3


def test_search_merges_overlapping_windows(vault_factory):
    vault_factory({"n.md": "a\nhit one\nb\nhit two\nc\n"})
    result = search_notes("hit", context_lines=1)[0]
    assert len(result["snippets"]) == 1
    ctx = result["snippets"][0]["context"]
    assert [c["line"] for c in ctx] == [1, 2, 3, 4, 5]
    assert [c["match"] for c in ctx] == [False, True, False, True, False]
    lines = [c["line"] for s in result["snippets"] for c in s["context"]]
    assert len(lines) == len(set(lines))  # no line appears twice


def test_search_caps_merged_window_height(vault_factory):
    vault_factory({"n.md": "\n".join(["hit"] * 50)})
    result = search_notes("hit", context_lines=1, max_snippets_per_note=2)[0]
    heights = [len(s["context"]) for s in result["snippets"]]
    assert len(heights) == 2
    assert all(h <= 2 * 1 + 5 for h in heights)


def test_search_snippet_params(vault_factory):
    vault_factory({"n.md": "\n".join(f"line {i} hit" for i in range(20))})
    result = search_notes("hit", context_lines=0, max_snippets_per_note=5)[0]
    assert len(result["snippets"]) >= 1
    assert all(c["match"] for s in result["snippets"] for c in s["context"])
    with pytest.raises(ValueError):
        search_notes("hit", max_snippets_per_note=0)
    with pytest.raises(ValueError):
        search_notes("hit", context_lines=99)


def test_search_clips_long_lines_around_match(vault_factory):
    from obsidian_mcp.tools.read import SNIPPET_LINE_MAX_CHARS

    long_line = "x" * 1000 + " needle " + "y" * 1000
    vault_factory({"n.md": "short\n" + long_line + "\n" + "z" * 1000 + "\n"})
    ctx = search_notes("needle")[0]["snippets"][0]["context"]
    by_line = {c["line"]: c["text"] for c in ctx}
    assert "needle" in by_line[2]
    assert len(by_line[2]) <= SNIPPET_LINE_MAX_CHARS
    assert by_line[2].startswith("…") and by_line[2].endswith("…")
    assert len(by_line[3]) <= SNIPPET_LINE_MAX_CHARS
    assert by_line[1] == "short"


# ── get_note_outline ──────────────────────────────────────────────────────

def test_get_note_outline_headings(vault_factory):
    vault_factory({"note.md": "# Title\n## Section A\ntext\n### Subsection\n## Section B\n"})
    outline = get_note_outline("note.md")
    headings = outline["headings"]
    assert len(headings) == 4
    assert headings[0]["level"] == 1
    assert headings[1]["text"] == "Section A"
    assert headings[2]["level"] == 3


def test_get_note_outline_block_refs(vault_factory):
    vault_factory({"note.md": "Important insight. ^key-insight\nOther text."})
    outline = get_note_outline("note.md")
    assert any(b["block_id"] == "key-insight" for b in outline["block_refs"])


def test_get_note_outline_stats(vault_factory):
    vault_factory({"note.md": "---\ntitle: T\n---\nOne two three"})
    outline = get_note_outline("note.md")
    assert outline["word_count"] > 0
    assert outline["line_count"] > 0
    assert "title" in outline["frontmatter_keys"]


def test_outline_includes_inline_fields(vault_factory):
    vault_factory({"note.md": "# Title\n\nrating:: 9\nauthor:: Alice\n"})
    result = get_note_outline("note.md")
    assert "inline_fields" in result
    assert result["inline_fields"]["rating"] == "9"
    assert result["inline_fields"]["author"] == "Alice"


def test_outline_inline_fields_empty_when_none(vault_factory):
    vault_factory({"note.md": "# Title\n\nJust text, no inline fields.\n"})
    result = get_note_outline("note.md")
    assert result["inline_fields"] == {}


def test_outline_inline_fields_independent_of_frontmatter(vault_factory):
    vault_factory({"note.md": "---\ntitle: Test\n---\npriority:: high\n"})
    result = get_note_outline("note.md")
    assert result["inline_fields"]["priority"] == "high"
    assert "priority" not in result["frontmatter_keys"]


# ── render_note ───────────────────────────────────────────────────────────

def test_render_note_no_embeds(vault_factory):
    vault_factory({"note.md": "# Hello\nNo embeds here."})
    result = render_note("note.md")
    assert "Hello" in result
    assert "No embeds here" in result


def test_render_note_resolves_embed(vault_factory):
    vault_factory({
        "main.md": "Intro\n![[embed]]\nOutro",
        "embed.md": "## Embedded Content\nThis is embedded.",
    })
    result = render_note("main.md")
    assert "Intro" in result
    assert "Embedded Content" in result
    assert "This is embedded" in result
    assert "Outro" in result


def test_render_note_embed_with_heading(vault_factory):
    vault_factory({
        "main.md": "![[source#Wichtig]]",
        "source.md": "## Unwichtig\nIgnore this.\n## Wichtig\nThis matters.\n## More\nSkip.",
    })
    result = render_note("main.md")
    assert "This matters" in result
    assert "Ignore this" not in result


def test_render_note_missing_embed_kept(vault_factory):
    vault_factory({"main.md": "![[nonexistent]]"})
    result = render_note("main.md")
    assert "![[nonexistent]]" in result


def test_render_note_depth_zero_no_resolve(vault_factory):
    vault_factory({
        "main.md": "![[embed]]",
        "embed.md": "SHOULD NOT APPEAR",
    })
    result = render_note("main.md", depth=0)
    assert "SHOULD NOT APPEAR" not in result
    assert "![[embed]]" in result

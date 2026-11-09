from __future__ import annotations

import pytest

import obsidian_mcp.config as cfg_mod
from obsidian_mcp.domain.parser import extract_inline_tags, extract_tags, parse_note
from obsidian_mcp.tools.query import list_all_tags
from obsidian_mcp.tools.write import _apply_tag_changes


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/o/r/pull/1#pullrequestreview-2308525538",
        "https://github.com/o/r/pull/1#discussion_r3558475131",
        "https://github.com/o/r/pull/1/files#diff-abc123def",
        "https://github.com/o/r/issues/1#event-14869330516",
        "https://github.com/o/r/blob/main/x.py#L79",
        "<https://example.com/page#section>",
        "www.example.com/page#frag",
        "[docs](https://example.com/a#anchor)",
        "[#notatag](https://example.com)",
        "![img](pic.png#center)",
    ],
)
def test_url_and_markdown_link_fragments_are_not_tags(text):
    assert extract_inline_tags(f"see {text} here") == []


@pytest.mark.parametrize("color", ["#007AFF", "#2147B1", "#fff0", "#a1b", "#2147B1cc"])
def test_hex_colors_with_digits_are_not_tags(color):
    tags = extract_inline_tags(f"color {color} used")
    if len(color) - 1 in (3, 6, 8):
        assert tags == []
    else:
        assert tags == [color[1:]]  # 4 hex digits is not a color code


def test_hex_lookalike_words_without_digits_stay_tags():
    assert extract_inline_tags("#add #facade #bee") == ["add", "facade", "bee"]


@pytest.mark.parametrize("text", ["#14/", "#155/", "#192/ x", "#2024", "#1/2"])
def test_numeric_tags_and_trailing_slash_are_not_tags(text):
    assert extract_inline_tags(text) == []


def test_tag_requires_start_or_whitespace_before_hash():
    assert extract_inline_tags("a#b c(#d) e/#f #ok\n#line") == ["ok", "line"]


def test_tag_charset_unicode_nested_and_trailing_punctuation():
    assert extract_inline_tags("#área/sub-tema_1, #ok. #año!") == [
        "área/sub-tema_1",
        "ok",
        "año",
    ]


def test_slack_like_channel_mentions_are_tags():
    assert extract_inline_tags("asked in #team-infra today") == ["team-infra"]


def test_tags_in_code_comments_and_wikilinks_ignored():
    body = "```\n#code\n```\n~~~\n#tilde\n~~~\n`#span` %% #comment %% [[N#head]] #real"
    assert extract_inline_tags(body) == ["real"]


def test_tags_source_modes():
    fm = {"tags": ["fm"]}
    body = "#inline"
    assert extract_tags(fm, body, source="both") == ["fm", "inline"]
    assert extract_tags(fm, body, source="frontmatter") == ["fm"]
    assert extract_tags(fm, body, source="inline") == ["inline"]


def test_tags_source_env_drives_parse_note(monkeypatch):
    raw = "---\ntags: [fm]\n---\n#inline"
    monkeypatch.setenv("TAGS_SOURCE", "frontmatter")
    assert parse_note(raw).tags == ["fm"]
    monkeypatch.setenv("TAGS_SOURCE", "inline")
    assert parse_note(raw).tags == ["inline"]
    monkeypatch.delenv("TAGS_SOURCE")
    assert parse_note(raw).tags == ["fm", "inline"]


def test_tags_source_config_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("TAGS_SOURCE", "Frontmatter")
    assert cfg_mod.Config().tags_source == "frontmatter"
    monkeypatch.setenv("TAGS_SOURCE", "everything")
    with pytest.raises(cfg_mod.ConfigError, match="TAGS_SOURCE"):
        cfg_mod.Config()


def test_list_all_tags_respects_tags_source(vault_factory, monkeypatch):
    monkeypatch.setenv("TAGS_SOURCE", "frontmatter")
    idx = vault_factory({"a.md": "---\ntags: [fm]\n---\nSee #channel and #007AFF"})
    assert [t["tag"] for t in list_all_tags(idx)] == ["fm"]


def test_list_all_tags_default_excludes_noise(vault_factory):
    idx = vault_factory({
        "a.md": (
            "#real https://x.io/p#L79 color #007AFF\n"
            "```\n#code\n```\n[[Note#Heading]] #14/ (#paren)\n"
        ),
    })
    assert [t["tag"] for t in list_all_tags(idx)] == ["real"]


def test_manage_tags_frontmatter_source_keeps_inline_tags():
    raw = "---\ntags: [a]\n---\nText #a here"
    out = _apply_tag_changes(raw, [], ["a"], source="frontmatter")
    assert "#a" in out


def test_manage_tags_strips_inline_but_not_in_code():
    raw = "---\ntags: [a]\n---\nText #a here\n```\n#a\n```\n  - nested #a\n"
    out = _apply_tag_changes(raw, [], ["a"], source="both")
    assert "Text here" in out
    assert "```\n#a\n```" in out
    assert "  - nested" in out


def test_manage_tags_inline_source_appends_inline_tag():
    raw = "Body #old"
    out = _apply_tag_changes(raw, ["new", "old"], [], source="inline")
    assert out.rstrip().endswith("#new")
    assert out.count("#old") == 1
    assert "tags:" not in out

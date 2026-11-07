"""ReDoS protection for caller-supplied regexes (length cap + match timeout)."""
from __future__ import annotations

import time

import pytest

import obsidian_mcp.config as cfg_mod
from obsidian_mcp.config import ConfigError, regex_limits_from_env
from obsidian_mcp.tools.read import search_notes
from obsidian_mcp.tools.safe_regex import (
    RegexTimeoutError,
    RegexTooLongError,
    compile_user_regex,
)
from obsidian_mcp.tools.write import find_replace_in_vault, patch_note_text

# (a|aa)+$ against "aaaa...b" backtracks exponentially even in the `regex`
# engine (unlike (a+)+$, which it optimizes away).
EVIL = r"(a|aa)+$"
EVIL_LINE = "a" * 60 + "b"


@pytest.fixture
def short_timeout(monkeypatch):
    monkeypatch.setenv("REGEX_TIMEOUT_SECONDS", "0.2")
    cfg_mod._config = None
    yield
    cfg_mod._config = None


# ── config ───────────────────────────────────────────────────────────────────

def test_defaults(monkeypatch):
    monkeypatch.delenv("REGEX_MAX_LENGTH", raising=False)
    monkeypatch.delenv("REGEX_TIMEOUT_SECONDS", raising=False)
    assert regex_limits_from_env() == (500, 2.0)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REGEX_MAX_LENGTH", "0"),
        ("REGEX_MAX_LENGTH", "lots"),
        ("REGEX_TIMEOUT_SECONDS", "0"),
        ("REGEX_TIMEOUT_SECONDS", "-1"),
        ("REGEX_TIMEOUT_SECONDS", "inf"),
        ("REGEX_TIMEOUT_SECONDS", "nan"),
        ("REGEX_TIMEOUT_SECONDS", "soon"),
    ],
)
def test_invalid_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        regex_limits_from_env()


# ── length cap ───────────────────────────────────────────────────────────────

def test_length_cap_configurable(vault_factory, monkeypatch):
    monkeypatch.setenv("REGEX_MAX_LENGTH", "10")
    vault_factory({"note.md": "hello"})
    compile_user_regex("a" * 10)
    with pytest.raises(RegexTooLongError, match="REGEX_MAX_LENGTH"):
        compile_user_regex("a" * 11)


def test_search_rejects_too_long_regex(vault_factory):
    vault_factory({"note.md": "hello"})
    with pytest.raises(RegexTooLongError):
        search_notes("a" * 501, mode="regex")


def test_patch_note_text_rejects_too_long_regex(tmp_path, vault_factory):
    vault_factory({"note.md": "hello"})
    with pytest.raises(ValueError, match="too long"):
        patch_note_text("note.md", "a" * 501, "x", mode="regex")
    assert (tmp_path / "note.md").read_text() == "hello"


def test_find_replace_rejects_too_long_regex(tmp_path, vault_factory):
    vault_factory({"note.md": "hello"})
    with pytest.raises(ValueError, match="too long"):
        find_replace_in_vault("a" * 501, "x", mode="regex", dry_run=False)
    assert (tmp_path / "note.md").read_text() == "hello"


def test_exact_mode_is_not_length_capped(tmp_path, vault_factory):
    long_literal = "word " * 200
    vault_factory({"note.md": long_literal})
    patch_note_text("note.md", long_literal, "short")
    assert (tmp_path / "note.md").read_text() == "short"


# ── timeout ──────────────────────────────────────────────────────────────────

def test_search_times_out_once_not_per_file(vault_factory, short_timeout):
    vault_factory({f"n{i}.md": EVIL_LINE for i in range(10)})
    start = time.monotonic()
    with pytest.raises(RegexTimeoutError, match="catastrophic"):
        search_notes(EVIL, mode="regex")
    # One timeout aborts the whole search instead of 0.2s x 10 files.
    assert time.monotonic() - start < 1.5


def test_patch_note_text_times_out_without_writing(tmp_path, vault_factory, short_timeout):
    vault_factory({"note.md": EVIL_LINE})
    with pytest.raises(RegexTimeoutError):
        patch_note_text("note.md", EVIL, "x", mode="regex")
    assert (tmp_path / "note.md").read_text() == EVIL_LINE


def test_find_replace_times_out_without_writing(tmp_path, vault_factory, short_timeout):
    vault_factory({"a.md": EVIL_LINE, "b.md": EVIL_LINE})
    with pytest.raises(RegexTimeoutError):
        find_replace_in_vault(EVIL, "x", mode="regex", dry_run=False)
    assert (tmp_path / "a.md").read_text() == EVIL_LINE
    assert (tmp_path / "b.md").read_text() == EVIL_LINE


# ── re compatibility ─────────────────────────────────────────────────────────

def test_replacement_templates_match_re(tmp_path, vault_factory):
    vault_factory({"note.md": "v1.2.3 and v4.5.6"})
    patch_note_text(
        "note.md", r"v(?P<major>\d+)\.(\d+)\.(\d+)", r"\g<major>-\2-\3", mode="regex", count=0
    )
    assert (tmp_path / "note.md").read_text() == "1-2-3 and 4-5-6"


def test_search_regex_is_case_insensitive(vault_factory):
    vault_factory({"note.md": "Hello World"})
    results = search_notes(r"hello\s+world", mode="regex")
    assert [r["path"] for r in results] == ["note.md"]


def test_invalid_regex_still_reported(vault_factory):
    vault_factory({"note.md": "x"})
    with pytest.raises(ValueError, match="Invalid regex"):
        patch_note_text("note.md", "(unclosed", "x", mode="regex")
    # search keeps its lenient behaviour: an invalid regex simply matches nothing
    assert search_notes("(unclosed", mode="regex") == []

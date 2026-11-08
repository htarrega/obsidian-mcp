"""ReDoS protection for caller-supplied regexes (length cap + match timeout)."""
from __future__ import annotations

import re
import time

import pytest

import obsidian_mcp.config as cfg_mod
from obsidian_mcp.config import ConfigError, regex_limits_from_env, regex_max_repeat_from_env
from obsidian_mcp.tools import safe_regex
from obsidian_mcp.tools.read import search_notes
from obsidian_mcp.tools.safe_regex import (
    RegexTimeoutError,
    RegexTooComplexError,
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


# ── compile-time repetition limits (memory bombs) ────────────────────────────

BOMBS = [
    r"(((a{50}){50}){50}){20}",  # ~800 MB RSS at compile without the check
    r"(?:(?:a{100}){100}){100}",
    r"a{1000}",
    r"a{0,101}",
    r"a{101,}",
    r"(a{99999999999999999999})",
    r"((a{2}){2}){2}",  # three counted levels
    r"(?:x|(a{100}b){101})",
    r"(a{100}){100}{2}",
    r"(?x) ( ( a{2} ) {2} ) {2}",  # verbose: whitespace before the quantifier
]


@pytest.mark.parametrize("pattern", BOMBS)
def test_repetition_bombs_rejected_before_compile(vault_factory, monkeypatch, pattern):
    vault_factory({"note.md": "x"})

    def boom(*args, **kwargs):  # the bombs must never reach the compiler
        raise AssertionError("regex.compile called for a rejected pattern")

    monkeypatch.setattr(safe_regex.regex, "compile", boom)
    with pytest.raises(RegexTooComplexError):
        compile_user_regex(pattern)


def test_verbose_flag_ignores_whitespace_for_cost(vault_factory, monkeypatch):
    vault_factory({"note.md": "x"})
    monkeypatch.setattr(safe_regex.regex, "compile", lambda *a, **k: pytest.fail("compiled"))
    with pytest.raises(RegexTooComplexError):
        compile_user_regex("( ( a{2} ) {2} ) {2}", re.VERBOSE)


@pytest.mark.parametrize(
    "pattern",
    [
        r"\d{4}-\d{2}-\d{2}",
        r"(?:\d{1,3}\.){3}\d{1,3}",
        r"\d+(?:\.\d+(?:\.\d+)?)?",
        r"((a+)+)+",  # star nesting is not expanded at compile time
        r"(a{100}){100}",
        r"[a{9999}]+",  # braces inside a character class are literals
        r"\{9999\}",  # escaped braces are literals
        r"a\{1000}",
        r"\p{L}{3}",
        r"x{,5}y{2,}?z{3}+",
        r"(?#a{9999})a",
        r"[[:alpha:]]{2}",
        r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]",
        r"{foo}",
    ],
)
def test_normal_patterns_still_compile(vault_factory, pattern):
    vault_factory({"note.md": "x"})
    assert compile_user_regex(pattern).pattern == pattern


def test_max_repeat_configurable(vault_factory, monkeypatch):
    monkeypatch.setenv("REGEX_MAX_REPEAT", "5")
    cfg_mod._config = None
    vault_factory({"note.md": "x"})
    compile_user_regex("a{5}")
    with pytest.raises(RegexTooComplexError, match="REGEX_MAX_REPEAT"):
        compile_user_regex("a{6}")
    cfg_mod._config = None


@pytest.mark.parametrize("value", ["0", "-3", "many"])
def test_max_repeat_invalid(monkeypatch, value):
    monkeypatch.setenv("REGEX_MAX_REPEAT", value)
    with pytest.raises(ConfigError, match="REGEX_MAX_REPEAT"):
        regex_max_repeat_from_env()


@pytest.mark.parametrize("exc", [MemoryError, RecursionError, OverflowError])
def test_compile_resource_errors_become_value_errors(vault_factory, monkeypatch, exc):
    vault_factory({"note.md": "x"})

    def fail(*args, **kwargs):
        raise exc()

    monkeypatch.setattr(safe_regex.regex, "compile", fail)
    with pytest.raises(ValueError) as excinfo:
        compile_user_regex("abc")
    assert not isinstance(excinfo.value, (MemoryError, RecursionError, OverflowError))


def test_search_reports_too_complex_instead_of_matching_nothing(vault_factory):
    vault_factory({"note.md": "x"})
    with pytest.raises(RegexTooComplexError):
        search_notes(r"(((a{50}){50}){50}){20}", mode="regex")

"""Per-vault layout config: templates dir, periodic patterns, orphan excludes,
entry note, conventions path, and frontmatter-independent note metadata."""

from __future__ import annotations

import json
import os
import time
from datetime import date

import pytest

import obsidian_mcp.config as cfg_mod
from obsidian_mcp import server
from obsidian_mcp.config import Config, ConfigError, load_vaults_file
from obsidian_mcp.layout import (
    LayoutError,
    VaultLayout,
    format_moment,
    layout_from_env,
    layout_from_json,
    path_in_folders,
    render_periodic_path,
)
from obsidian_mcp.tools.lint import lint_schema
from obsidian_mcp.tools.query import get_orphans, get_periodic_note, query_notes
from obsidian_mcp.tools.read import list_notes
from obsidian_mcp.tools.templates import create_from_template, list_templates_info
from obsidian_mcp.tools.vault_layout import vault_conventions

LAYOUT_ENV = (
    "TEMPLATES_DIR",
    "PERIODIC_DAILY_PATH",
    "PERIODIC_WEEKLY_PATH",
    "PERIODIC_MONTHLY_PATH",
    "PERIODIC_QUARTERLY_PATH",
    "PERIODIC_YEARLY_PATH",
    "ORPHAN_EXCLUDES",
    "ENTRY_NOTE",
    "CONVENTIONS_PATH",
    "DENY_WRITE_PATHS",
)


@pytest.fixture(autouse=True)
def _clean_layout_env(monkeypatch):
    for name in LAYOUT_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def served(vault_factory, monkeypatch):
    def _make(files: dict[str, str] | None = None, **env: str):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        index = vault_factory(files)
        monkeypatch.setattr(server, "_indices", {cfg_mod.get_config().default_vault_name: index})
        return index

    return _make


# ── layout parsing ──────────────────────────────────────────────────────────


def test_defaults_match_historical_layout():
    layout = layout_from_env({})
    assert layout == VaultLayout()
    assert layout.templates_dir == "Templates"
    assert layout.conventions_path == "_AI_INSTRUCTIONS.md"
    assert layout.orphan_excludes == ("Journal", "Templates")
    assert layout.entry_note is None
    assert render_periodic_path(layout.periodic_pattern("daily"), "daily", date(2026, 3, 4)) == (
        "Journal/2026-03-04.md"
    )
    assert render_periodic_path(layout.periodic_pattern("weekly"), "weekly", date(2026, 1, 1)) == (
        "Journal/Weekly/2026-W01.md"
    )
    assert render_periodic_path(layout.periodic_pattern("quarterly"), "quarterly", date(2026, 5, 1)) == (
        "Journal/Quarterly/2026-Q2.md"
    )


def test_env_layout_parses_unicode_paths():
    layout = layout_from_env({
        "TEMPLATES_DIR": "(000) System/Plantillas Ñandú/",
        "PERIODIC_DAILY_PATH": "(400) Personal/Journal/{{date:YYYY-MM-DD}}.md",
        "PERIODIC_MONTHLY_PATH": "(400) Personal/Journal/{{date:YYYY}}/{{date:MM}}",
        "ORPHAN_EXCLUDES": "(000) System, (100) Attachments ,(400) Personal/Temporary,",
        "ENTRY_NOTE": "(500) Root/Mentat",
        "CONVENTIONS_PATH": "(000) System/_AI_INSTRUCTIONS.md",
    })
    assert layout.templates_dir == "(000) System/Plantillas Ñandú"
    assert layout.orphan_excludes == ("(000) System", "(100) Attachments", "(400) Personal/Temporary")
    assert layout.entry_note == "(500) Root/Mentat.md"
    assert layout.conventions_path == "(000) System/_AI_INSTRUCTIONS.md"
    assert layout.is_configured("periodic.daily")
    assert not layout.is_configured("periodic.weekly")
    assert render_periodic_path(layout.periodic_pattern("monthly"), "monthly", date(2026, 2, 9)) == (
        "(400) Personal/Journal/2026/02.md"
    )


@pytest.mark.parametrize(
    "env",
    [
        {"TEMPLATES_DIR": "../outside"},
        {"CONVENTIONS_PATH": "/etc/passwd"},
        {"ENTRY_NOTE": "a/../../x.md"},
        {"ORPHAN_EXCLUDES": "ok,../nope"},
        {"PERIODIC_DAILY_PATH": "../{{date:YYYY-MM-DD}}.md"},
        {"PERIODIC_DAILY_PATH": "Journal/today.md"},  # no placeholder
    ],
)
def test_env_layout_rejects_unsafe_values(env):
    with pytest.raises(LayoutError):
        layout_from_env(env)


def test_json_layout_and_validation():
    layout = layout_from_json(
        {
            "templates_dir": "Meta/Templates",
            "periodic": {"daily": "Days/{{date:YYYY-MM-DD}}.md", "weekly": None},
            "orphan_excludes": ["Meta"],
            "entry_note": "Home.md",
            "conventions_path": "Meta/rules.md",
        },
        vault_name="work",
    )
    assert layout.templates_dir == "Meta/Templates"
    assert layout.periodic_pattern("daily") == "Days/{{date:YYYY-MM-DD}}.md"
    assert layout.periodic_pattern("weekly") == "Journal/Weekly/{{date:GGGG-[W]WW}}.md"
    with pytest.raises(LayoutError, match="unknown period"):
        layout_from_json({"periodic": {"hourly": "{{date}}"}}, vault_name="w")
    with pytest.raises(LayoutError, match="list of strings"):
        layout_from_json({"orphan_excludes": "Meta"}, vault_name="w")


def test_moment_tokens():
    day = date(2026, 1, 4)  # Sunday, ISO week 2026-W01
    assert format_moment(day, "YYYY-MM-DD") == "2026-01-04"
    assert format_moment(day, "GGGG-[W]WW") == "2026-W01"
    assert format_moment(day, "dddd D MMMM YY") == "Sunday 4 January 26"
    assert format_moment(date(2024, 12, 30), "gggg-[W]ww") == "2025-W01"


def test_single_vault_config_layout_and_deny_default(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("CONVENTIONS_PATH", "(000) System/_AI_INSTRUCTIONS.md")
    cfg = Config()
    assert cfg.layout.conventions_path == "(000) System/_AI_INSTRUCTIONS.md"
    assert "(000) System/_AI_INSTRUCTIONS.md" in cfg.deny_write_paths
    assert "_AI_INSTRUCTIONS.md" not in cfg.deny_write_paths

    monkeypatch.setenv("TEMPLATES_DIR", "../x")
    with pytest.raises(ConfigError, match="TEMPLATES_DIR"):
        Config()


def test_multi_vault_layout_per_vault(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    config = tmp_path / "vaults.json"
    config.write_text(json.dumps({
        "vaults": {
            "a": {"path": str(a), "templates_dir": "T", "entry_note": "Home.md",
                  "orphan_excludes": ["Archive"], "conventions_path": "rules.md"},
            "b": {"path": str(b)},
        },
        "identities": [{"type": "api_key", "value": "k", "vaults": ["a", "b"]}],
    }))
    vaults, _ = load_vaults_file(str(config))
    assert vaults["a"].layout.templates_dir == "T"
    assert vaults["a"].layout.entry_note == "Home.md"
    assert "rules.md" in vaults["a"].deny_write_paths
    assert vaults["b"].layout == VaultLayout()
    assert "_AI_INSTRUCTIONS.md" in vaults["b"].deny_write_paths

    raw = json.loads(config.read_text())
    raw["vaults"]["a"]["conventions_path"] = "../rules.md"
    config.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match="conventions_path"):
        load_vaults_file(str(config))


def test_path_in_folders_rules():
    excludes = ("Journal", "(400) Personal/Temporary", "(000) System")
    assert path_in_folders("Journal/a.md", excludes)
    assert path_in_folders("Areas/journal/a.md", excludes)  # any depth, case-insensitive
    assert path_in_folders("(400) Personal/Temporary/x.md", excludes)
    assert path_in_folders("(400) personal/temporary/sub/x.md", excludes)
    assert not path_in_folders("(400) Personal/Sandbox/x.md", excludes)
    assert not path_in_folders("Other/(400) Personal/Temporary/x.md", excludes)  # rooted
    assert not path_in_folders("Journal.md", excludes)
    # NFD vs NFC spelling of the same folder
    assert path_in_folders("Café/x.md", ("Café",))


# ── periodic notes ──────────────────────────────────────────────────────────


def test_periodic_default_missing_returns_suggested_path(vault_factory):
    idx = vault_factory({"a.md": "x"})
    result = get_periodic_note(idx, "daily", "2026-03-04")
    assert result["exists"] is False
    assert result["suggested_path"] == "Journal/2026-03-04.md"
    assert result["configured"] is False
    assert "PERIODIC_DAILY_PATH" in result["hint"]
    assert result["template"] is None


def test_periodic_configured_pattern_reads_existing(vault_factory, monkeypatch):
    monkeypatch.setenv("PERIODIC_DAILY_PATH", "(400) Personal/Journal/{{date:YYYY-MM-DD}}.md")
    idx = vault_factory({"(400) Personal/Journal/2026-03-04.md": "# Day\n- [ ] task\n"})
    result = get_periodic_note(idx, "daily", "2026-03-04")
    assert result["exists"] is True
    assert result["path"] == "(400) Personal/Journal/2026-03-04.md"
    assert result["configured"] is True
    assert result["tasks"][0]["text"] == "task"

    missing = get_periodic_note(idx, "daily", "2026-03-05")
    assert missing["exists"] is False
    assert missing["suggested_path"] == "(400) Personal/Journal/2026-03-05.md"
    assert "PERIODIC_DAILY_PATH" not in missing["hint"]


def test_periodic_preview_uses_configured_templates_dir(vault_factory, monkeypatch):
    monkeypatch.setenv("TEMPLATES_DIR", "Meta/Tpl")
    idx = vault_factory({"Meta/Tpl/Daily-Note-Template.md": "# {{date}}\n"})
    result = get_periodic_note(idx, "daily", "2026-03-04")
    assert result["template"] == "Meta/Tpl/Daily-Note-Template.md"
    assert result["content"] == "# 2026-03-04\n"


def test_periodic_unknown_period(vault_factory):
    idx = vault_factory({})
    with pytest.raises(ValueError, match="Unknown period"):
        get_periodic_note(idx, "hourly", "today")


# ── templates ───────────────────────────────────────────────────────────────


def test_list_templates_missing_dir_hint(vault_factory):
    vault_factory({"a.md": "x"})
    info = list_templates_info()
    assert info["items"] == []
    assert "does not exist" in info["hint"]


def test_list_templates_tool_configured_dir(served):
    served({"(000) System/Templates/Meeting.md": "# {{title}}", "Templates/old.md": "x"},
           TEMPLATES_DIR="(000) System/Templates")
    result = server.list_templates_tool()
    assert result["data"]["items"] == ["(000) System/Templates/Meeting.md"]
    assert result["meta"]["templates_dir"] == "(000) System/Templates"
    assert "hint" not in result["meta"]


def test_list_templates_tool_missing_dir_is_not_an_error(served):
    served({"a.md": "x"}, TEMPLATES_DIR="(000) System/Templates")
    result = server.list_templates_tool()
    assert result["success"] is True
    assert result["data"]["items"] == []
    assert "TEMPLATES_DIR" in result["meta"]["hint"]


def test_create_from_template_resolves_name_in_templates_dir(tmp_path, vault_factory, monkeypatch):
    monkeypatch.setenv("TEMPLATES_DIR", "(000) System/Templates")
    vault_factory({"(000) System/Templates/Meeting.md": "# {{title}}\n"})
    result = create_from_template("Meeting", "Notes/Standup.md")
    assert result["template"] == "(000) System/Templates/Meeting.md"
    assert (tmp_path / "Notes" / "Standup.md").read_text() == "# Standup\n"


# ── orphans ─────────────────────────────────────────────────────────────────

ORPHAN_VAULT = {
    "(500) Root/Mentat.md": "[[linked]]",
    "(200) Notes/linked.md": "x",
    "(200) Notes/lonely.md": "x",
    "(000) System/rules.md": "x",
    "(400) Personal/Temporary/tmp.md": "x",
    "(400) Personal/Sandbox/sand.md": "x",
    "Journal/2026-01-01.md": "x",
}


def test_orphans_default_excludes_unchanged(vault_factory):
    idx = vault_factory(ORPHAN_VAULT)
    orphans = get_orphans(idx, exclude_folders=list(VaultLayout().orphan_excludes))
    assert "Journal/2026-01-01.md" not in orphans
    assert "(500) Root/Mentat.md" in orphans  # no entry note configured


def test_orphans_tool_uses_config(served):
    served(
        ORPHAN_VAULT,
        ORPHAN_EXCLUDES="(000) System,(100) Attachments,(400) Personal/Temporary",
        ENTRY_NOTE="(500) Root/Mentat.md",
    )
    result = server.get_orphans_tool()
    items = result["data"]["items"]
    assert "(500) Root/Mentat.md" not in items
    assert "(000) System/rules.md" not in items
    assert "(400) Personal/Temporary/tmp.md" not in items
    assert "(400) Personal/Sandbox/sand.md" in items
    assert "(200) Notes/lonely.md" in items
    assert "Journal/2026-01-01.md" in items  # configured list replaces the default
    assert result["meta"]["entry_note"] == "(500) Root/Mentat.md"

    explicit = server.get_orphans_tool(exclude_folders=["(400) Personal"])
    assert "(400) Personal/Sandbox/sand.md" not in explicit["data"]["items"]
    assert "(500) Root/Mentat.md" not in explicit["data"]["items"]


# ── conventions ─────────────────────────────────────────────────────────────


def test_conventions_from_configured_file(vault_factory, monkeypatch):
    monkeypatch.setenv("CONVENTIONS_PATH", "(000) System/_AI_INSTRUCTIONS.md")
    idx = vault_factory({"(000) System/_AI_INSTRUCTIONS.md": "# Rules\n", "_AI_INSTRUCTIONS.md": "old"})
    result = vault_conventions(idx)
    assert result == {"conventions": "# Rules\n", "source": "file",
                      "path": "(000) System/_AI_INSTRUCTIONS.md"}


def test_conventions_auto_generated_when_missing(served, tmp_path):
    served(
        {
            "(500) Root/Mentat.md": "[[a]] [[b]]",
            "(200) Notes/a.md": "---\nstatus: active\n---\nbody",
            "(200) Notes/b.md": "no frontmatter",
        },
        ENTRY_NOTE="(500) Root/Mentat.md",
    )
    (tmp_path / "(100) Attachments").mkdir()
    (tmp_path / "(100) Attachments" / "img.png").write_bytes(b"\x89PNG")
    (tmp_path / "(000) System").mkdir()
    result = server.get_vault_conventions_tool()
    data = result["data"]
    assert data["source"] == "auto-generated"
    text = data["conventions"]
    assert "`(200) Notes` — 2 notes" in text
    assert "`(000) System` — 0 notes" in text
    assert "(500) Root/Mentat.md" in text
    assert "1 of 3 notes (33%) have frontmatter" in text
    assert "`status`: 1 notes" in text
    assert "1 non-Markdown files" in text
    assert "body" not in text  # never leaks note content


def test_conventions_auto_generated_most_linked_without_entry(vault_factory):
    idx = vault_factory({"hub.md": "x", "a.md": "[[hub]]", "b.md": "[[hub]]"})
    text = vault_conventions(idx)["conventions"]
    assert "most-linked note: `hub.md` (2 backlinks)" in text


def test_lint_schema_reads_configured_conventions(vault_factory, monkeypatch):
    monkeypatch.setenv("CONVENTIONS_PATH", "Meta/rules.md")
    idx = vault_factory({
        "Meta/rules.md": "## Frontmatter Schema\n```yaml\nstatus: a | b\n```\n",
        "n.md": "---\nstatus: c\n---\n",
    })
    result = lint_schema(idx)
    assert result["schema"] == {"status": ["a", "b"]}
    assert result["violations"][0]["path"] == "n.md"


def test_lint_schema_missing_file_hint(vault_factory):
    idx = vault_factory({"n.md": "x"})
    result = lint_schema(idx)
    assert result["schema"] == {} and result["violations"] == []
    assert "_AI_INSTRUCTIONS.md" in result["hint"]


# ── created fallback and frontmatter-free filters ───────────────────────────


def _set_mtime(path, when: str):
    ts = time.mktime(time.strptime(when, "%Y-%m-%d"))
    os.utime(path, (ts, ts))


def test_created_falls_back_to_mtime_and_sorts(tmp_path, vault_factory):
    idx = vault_factory({
        "old.md": "no fm",
        "new.md": "no fm",
        "fm.md": "---\ncreated: 2025-06-01\n---\n",
    })
    _set_mtime(tmp_path / "old.md", "2024-01-01")
    _set_mtime(tmp_path / "new.md", "2026-01-01")
    items = query_notes(idx, sort_by="created")
    assert [item["path"] for item in items] == ["old.md", "fm.md", "new.md"]
    sources = {item["path"]: item["created_source"] for item in items}
    assert sources["fm.md"] == "frontmatter"
    assert sources["old.md"] in ("mtime", "birthtime")
    if sources["old.md"] == "mtime":
        assert items[0]["created"].startswith("2024-01-01")


def test_list_notes_include_meta_created_source(tmp_path, vault_factory):
    vault_factory({"a.md": "x", "b.md": "---\ncreated: 2025-01-01\n---\n"})
    items = {item["path"]: item for item in list_notes(include_meta=True)}
    assert items["b.md"]["created"] == "2025-01-01"
    assert items["b.md"]["created_source"] == "frontmatter"
    assert items["a.md"]["created_source"] in ("mtime", "birthtime")
    assert items["a.md"]["created"]


def test_query_filters_without_frontmatter(tmp_path, vault_factory):
    idx = vault_factory({
        "(200) Notes/Árbol de decisión.md": "one two three four five",
        "(200) Notes/arbitrary.md": "- [ ] do it\n",
        "(300) Permanent/other.md": "short",
        "(200) Notes/titled.md": "---\ntitle: Decision Tree\n---\nwords here",
    })
    _set_mtime(tmp_path / "(300) Permanent" / "other.md", "2020-05-05")

    def paths(**kwargs):
        return sorted(item["path"] for item in query_notes(idx, **kwargs))

    assert paths(title_contains="ARBOL") == ["(200) Notes/Árbol de decisión.md"]
    assert paths(title_contains="decision") == [
        "(200) Notes/titled.md", "(200) Notes/Árbol de decisión.md"
    ]
    assert paths(path_glob="(300)*") == ["(300) Permanent/other.md"]
    assert paths(path_glob="*/arb*.md") == ["(200) Notes/arbitrary.md"]
    assert paths(mtime_before="2020-05-05") == ["(300) Permanent/other.md"]
    assert "(300) Permanent/other.md" not in paths(mtime_after="2020-05-06")
    assert paths(min_words=5) == ["(200) Notes/Árbol de decisión.md"]
    assert paths(has_tasks=True) == ["(200) Notes/arbitrary.md"]
    assert "(200) Notes/arbitrary.md" not in paths(has_tasks=False)
    with pytest.raises(ValueError, match="mtime_after"):
        query_notes(idx, mtime_after="yesterday")


def test_query_notes_tool_passes_new_filters(served):
    served({"a.md": "- [ ] x", "b.md": "none"})
    result = server.query_notes_tool(has_tasks=True)
    assert [item["path"] for item in result["data"]["items"]] == ["a.md"]

from __future__ import annotations

import pytest

import obsidian_mcp.config as cfg_mod
from obsidian_mcp import server
from obsidian_mcp.domain.parser import extract_tasks
from obsidian_mcp.tools.query import get_tasks, get_tasks_page

# ── parsing ───────────────────────────────────────────────────────────────


def test_task_bullet_variants_and_nesting():
    body = (
        "- [ ] dash\n"
        "* [x] star\n"
        "+ [X] plus\n"
        "1. [ ] numbered\n"
        "2) [ ] paren numbered\n"
        "    - [ ] nested\n"
        "\t* [x] tab nested\n"
        "> - [ ] in callout\n"
        "- [?] other status ignored\n"
        "- [ ]\n"
        "-[ ] no space\n"
    )
    tasks = extract_tasks(body)
    assert [t.text for t in tasks] == [
        "dash", "star", "plus", "numbered", "paren numbered", "nested", "tab nested",
        "in callout",
    ]
    assert [t.done for t in tasks] == [False, True, True, False, False, False, True, False]
    assert [t.line for t in tasks] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_tasks_in_code_and_comments_ignored():
    body = "```\n- [ ] in fence\n```\n~~~md\n- [ ] in tilde\n~~~\n%%\n- [ ] in comment\n%%\n- [ ] real"
    tasks = extract_tasks(body)
    assert [(t.text, t.line) for t in tasks] == [("real", 10)]


# ── paging / dedupe / grouping ────────────────────────────────────────────


@pytest.fixture
def worklog_index(vault_factory):
    days = "".join(
        f"## Day {i}\n- [ ] Golden  Files >> generate JSONs\n- [x] day {i} done\n" for i in range(5)
    )
    return vault_factory({
        "Work/log.md": days + "- [ ] golden files >> GENERATE jsons\n",
        "Work/Archive/old.md": "- [ ] archived\n",
        "Personal/todo.md": "- [ ] buy milk\n- [x] call mom\n",
    })


def test_page_meta_and_truncation(worklog_index):
    page = get_tasks_page(worklog_index, status="all", limit=3)
    assert page["meta"] == {
        "total": 14, "returned": 3, "truncated": True, "offset": 0, "limit": 3,
    }
    rest = get_tasks_page(worklog_index, status="all", limit=100, offset=12)
    assert rest["meta"]["returned"] == 2
    assert rest["meta"]["truncated"] is False


def test_limit_is_clamped_and_offset_validated(worklog_index):
    assert get_tasks_page(worklog_index, limit=5000)["meta"]["limit"] == 1000
    assert get_tasks_page(worklog_index, limit=0)["meta"]["limit"] == 1
    with pytest.raises(ValueError):
        get_tasks_page(worklog_index, offset=-1)
    with pytest.raises(ValueError):
        get_tasks_page(worklog_index, group_by="tag")


def test_dedupe_keeps_last_occurrence(worklog_index):
    tasks = get_tasks(worklog_index, status="open", folder="Work", dedupe=True)
    golden = [t for t in tasks if "golden" in t["text"].lower()]
    assert len(golden) == 1
    assert golden[0]["occurrences"] == 6
    assert golden[0]["first_line"] == 2
    assert golden[0]["last_line"] == golden[0]["line"] == 16
    assert golden[0]["text"] == "golden files >> GENERATE jsons"
    # Distinct texts are untouched; dedupe never merges across notes.
    assert any(t["text"] == "archived" and t["occurrences"] == 1 for t in tasks)


def test_exclude_folders_and_path_glob(worklog_index):
    by_name = get_tasks(worklog_index, status="all", exclude_folders=["Archive"])
    assert not any(t["source"].startswith("Work/Archive/") for t in by_name)
    by_prefix = get_tasks(worklog_index, status="all", exclude_folders=["Work/"])
    assert {t["source"] for t in by_prefix} == {"Personal/todo.md"}
    globbed = get_tasks(worklog_index, status="all", path_glob="personal/*.md")
    assert {t["source"] for t in globbed} == {"Personal/todo.md"}


def test_group_by_source(worklog_index):
    page = get_tasks_page(worklog_index, status="open", group_by="source", dedupe=True)
    assert [g["source"] for g in page["items"]] == [
        "Personal/todo.md", "Work/Archive/old.md", "Work/log.md",
    ]
    assert all("source" not in t for g in page["items"] for t in g["tasks"])
    assert page["meta"]["total"] == 3


def test_summary_only_counts_per_note(worklog_index):
    page = get_tasks_page(worklog_index, summary_only=True)
    assert page["items"] == [
        {"source": "Personal/todo.md", "open": 1, "done": 1, "total": 2},
        {"source": "Work/Archive/old.md", "open": 1, "done": 0, "total": 1},
        {"source": "Work/log.md", "open": 6, "done": 5, "total": 11},
    ]
    assert page["meta"]["total"] == 3


def test_tool_envelope_carries_meta(worklog_index, monkeypatch):
    monkeypatch.setattr(
        server, "_indices", {cfg_mod.get_config().default_vault_name: worklog_index}
    )
    result = server.get_tasks_tool(status="all", limit=2)
    assert len(result["data"]["items"]) == 2
    assert result["meta"]["total"] == 14
    assert result["meta"]["truncated"] is True
    assert result["meta"]["offset"] == 0

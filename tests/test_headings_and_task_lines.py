"""Heading extraction ignores code/frontmatter; task lines are file lines."""
from __future__ import annotations

from obsidian_mcp.domain.markdown_scan import extract_headings, frontmatter_end
from obsidian_mcp.domain.parser import parse_note


def test_headings_skip_code_blocks_inline_code_and_frontmatter():
    text = (
        "---\n"
        "# not a heading: yaml comment\n"
        "a: 1\n"
        "---\n"
        "## Real one\n"
        "```bash\n"
        "# shell comment\n"
        "```\n"
        "~~~\n"
        "## inside tilde fence\n"
        "~~~\n"
        "%% ## in a comment %%\n"
        "#tag is not a heading\n"
        "### Use `foo` here ###\n"
    )
    assert extract_headings(text) == [
        {"level": 2, "text": "Real one", "line": 5},
        {"level": 3, "text": "Use `foo` here", "line": 14},
    ]


def test_headings_without_frontmatter_count_from_line_one():
    assert extract_headings("# Title\nbody\n## 6 Nov\n") == [
        {"level": 1, "text": "Title", "line": 1},
        {"level": 2, "text": "6 Nov", "line": 3},
    ]


def test_frontmatter_end():
    assert frontmatter_end("no frontmatter") == 0
    assert frontmatter_end("---\na: 1\n---\nbody") == len("---\na: 1\n---\n")


def test_task_lines_are_file_lines_even_with_frontmatter():
    raw = "---\nstatus: x\n---\n\nIntro\n- [ ] first\n- [x] second\n"
    note = parse_note(raw)
    assert [(t.text, t.line) for t in note.tasks] == [("first", 6), ("second", 7)]


def test_task_lines_skip_leading_blank_lines():
    note = parse_note("\n\n- [ ] task\n")
    assert note.tasks[0].line == 3

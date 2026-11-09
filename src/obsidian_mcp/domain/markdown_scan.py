"""Shared Markdown scanning helpers.

Obsidian ignores wikilinks, tags and tasks that sit inside code (fenced
blocks and inline spans) or inside ``%% ... %%`` comments. Every extractor
runs over :func:`mask_non_content` output instead of the raw body, so they all
agree on what counts as "prose".

Masking replaces every masked character with a space while keeping newlines,
so offsets and line numbers computed on the masked text are valid for the
original text too.
"""
from __future__ import annotations

import re

# An opening/closing code fence: optional indentation and blockquote markers
# (fences inside list items and callouts are common), then 3+ backticks or
# tildes. Backtick fences may not carry backticks in their info string.
_FENCE_RE = re.compile(r"^(?P<prefix>[ \t>]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# Two consecutive line breaks (optionally with whitespace between them) end a
# paragraph, and an inline code span never crosses a paragraph boundary.
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")


def _blank(segment: str) -> str:
    """Replace every character except newlines with a space."""
    return "".join("\n" if ch == "\n" else " " for ch in segment)


def mask_fenced_code(text: str) -> str:
    """Blank out fenced code blocks (``` and ~~~), fences included.

    A block closes on a line holding only the same fence character repeated
    at least as many times as the opener; an unclosed block runs to the end
    of the text (CommonMark semantics).
    """
    lines = text.split("\n")
    out: list[str] = []
    fence_char = ""
    fence_len = 0
    for line in lines:
        if not fence_char:
            m = _FENCE_RE.match(line)
            if m and not (m.group("fence")[0] == "`" and "`" in m.group("info")):
                fence_char = m.group("fence")[0]
                fence_len = len(m.group("fence"))
                out.append(_blank(line))
            else:
                out.append(line)
            continue
        out.append(_blank(line))
        m = _FENCE_RE.match(line)
        if (
            m
            and m.group("fence")[0] == fence_char
            and len(m.group("fence")) >= fence_len
            and not m.group("info").strip()
        ):
            fence_char = ""
            fence_len = 0
    return "\n".join(out)


def mask_inline_code_and_comments(text: str) -> str:
    """Blank out inline code spans and closed ``%% ... %%`` comments.

    Scans left to right so whichever construct opens first wins: a ``%%``
    inside a code span is code, a backtick inside a comment is comment.
    A code span is a run of N backticks closed by the next run of exactly N
    backticks within the same paragraph. An unmatched backtick run or ``%%``
    is literal text.
    """
    chars = list(text)
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "`":
            j = i
            while j < n and text[j] == "`":
                j += 1
            run = j - i
            end = _find_closing_backticks(text, j, run)
            if end is None:
                i = j
                continue
            for k in range(i, end):
                if chars[k] != "\n":
                    chars[k] = " "
            i = end
        elif ch == "%" and text.startswith("%%", i):
            close = text.find("%%", i + 2)
            if close == -1:
                i += 2
                continue
            end = close + 2
            for k in range(i, end):
                if chars[k] != "\n":
                    chars[k] = " "
            i = end
        else:
            i += 1
    return "".join(chars)


def _find_closing_backticks(text: str, start: int, run: int) -> int | None:
    """End offset (exclusive) of the closing run of exactly ``run`` backticks,
    or None if the paragraph ends first."""
    paragraph_end = _BLANK_LINE_RE.search(text, start)
    limit = paragraph_end.start() if paragraph_end else len(text)
    i = start
    while i < limit:
        if text[i] == "`":
            j = i
            while j < limit and text[j] == "`":
                j += 1
            if j - i == run:
                return j
            i = j
        else:
            i += 1
    return None


def mask_non_content(text: str) -> str:
    """Blank out fenced code, inline code and ``%%`` comments (length- and
    line-preserving). Use this before extracting links, tags or tasks."""
    return mask_inline_code_and_comments(mask_fenced_code(text))


def mask_pattern(text: str, pattern: re.Pattern[str]) -> str:
    """Blank out every match of ``pattern`` (length- and line-preserving)."""
    return pattern.sub(lambda m: _blank(m.group(0)), text)


# A YAML frontmatter block at the very start of a file.
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

# An ATX heading: up to three spaces of indentation, 1-6 "#", then at least
# one space or tab ("#tag" is not a heading).
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*$", re.MULTILINE)
_CLOSING_HASHES_RE = re.compile(r"[ \t]+#+$")


def frontmatter_end(text: str) -> int:
    """Offset just past a leading YAML frontmatter block (0 if there is none)."""
    m = _FRONTMATTER_RE.match(text)
    return m.end() if m else 0


def extract_headings(text: str) -> list[dict]:
    """ATX headings of a whole file as ``[{level, text, line}]``.

    ``line`` is the 1-based line in ``text``. Frontmatter, fenced/inline code
    and ``%%`` comments are ignored, so a ``# comment`` inside a code block is
    not reported as a heading.
    """
    end = frontmatter_end(text)
    scan = _blank(text[:end]) + mask_non_content(text[end:])
    headings: list[dict] = []
    for m in _HEADING_RE.finditer(scan):
        # Take the title from the original text: masking blanks inline code,
        # which may legitimately be part of a heading title.
        start = m.start(2)
        stop = text.find("\n", start)
        title = text[start:] if stop == -1 else text[start:stop]
        title = _CLOSING_HASHES_RE.sub("", title.strip())
        if not title:
            continue
        headings.append({
            "level": len(m.group(1)),
            "text": title,
            "line": text.count("\n", 0, m.start()) + 1,
        })
    return headings

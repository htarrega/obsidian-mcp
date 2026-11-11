"""Inline Markdown links ``[text](dest)`` / ``![alt](dest)`` with exact spans.

Obsidian accepts three spellings for a destination with spaces or
parentheses: percent-encoded (``My%20note.md``), angle brackets
(``<My note.md>``) and raw balanced parentheses (``(200)%20Notes/a.md``).
A single regex can't follow balanced parentheses, so this is a small scanner.
Code, ``%%`` comments and frontmatter are skipped; spans index the original
text so a caller can splice replacements in.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import unquote

from .markdown_scan import frontmatter_end, mask_non_content

# "https:", "mailto:", "obsidian:", "file:" ... — anything with a URL scheme.
_SCHEME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*:")
# What may follow the destination: an optional "title" / 'title', then ")".
_TAIL_RE = re.compile(r"[ \t]*(?:\"[^\"\n]*\"|'[^'\n]*')?[ \t]*\)")
# A reference definition "[ref]: dest" at the start of a line.
_REFERENCE_DEF_RE = re.compile(r"^ {0,3}\[[^\]\n]+\]:[ \t]*(<[^>\n]*>|\S+)", re.MULTILINE)


@dataclass(frozen=True)
class MarkdownLink:
    start: int  # destination span in the original text (includes <> when angle)
    end: int
    raw: str  # destination as written, without <>
    path: str  # percent-decoded path, without the #subpath
    subpath: str  # "#Heading" / "#^block" exactly as written, or ""
    angle: bool
    embed: bool


def _scannable(text: str) -> str:
    masked = mask_non_content(text)
    fm = frontmatter_end(text)
    if fm:
        masked = "".join("\n" if ch == "\n" else " " for ch in masked[:fm]) + masked[fm:]
    return masked


def _split(raw: str) -> tuple[str, str] | None:
    """(decoded path, raw subpath) for an internal destination, else None."""
    path_raw, sep, sub = raw.partition("#")
    if not path_raw or _SCHEME_RE.match(path_raw) or path_raw.startswith("//"):
        return None  # pure "#anchor", external URL or protocol-relative URL
    if "[" in path_raw or "://" in path_raw:
        return None  # a pasted link nested in the destination: [t]([name](https://...))
    return unquote(path_raw), sep + sub


def iter_markdown_links(text: str) -> Iterator[MarkdownLink]:
    """Internal inline links and embeds outside code, comments and frontmatter."""
    masked = _scannable(text)
    size = len(masked)
    i = 0
    while True:
        j = masked.find("](", i)
        if j < 0:
            return
        line_start = masked.rfind("\n", 0, j) + 1
        opener = masked.rfind("[", line_start, j)
        i = j + 2
        if opener < 0:
            continue
        k = j + 2
        while k < size and masked[k] in " \t":
            k += 1
        if k < size and masked[k] == "<":
            close = masked.find(">", k + 1)
            if close < 0 or "\n" in masked[k:close]:
                continue
            start, end, raw, angle = k, close + 1, text[k + 1 : close], True
        else:
            depth, start = 0, k
            while k < size and masked[k] not in " \t\n":
                if masked[k] == "(":
                    depth += 1
                elif masked[k] == ")":
                    if depth == 0:
                        break
                    depth -= 1
                k += 1
            end, raw, angle = k, text[start:k], False
        tail = _TAIL_RE.match(masked, end)
        if not raw or tail is None:
            continue
        split = _split(raw)
        i = tail.end()
        if split is None:
            continue
        yield MarkdownLink(
            start=start,
            end=end,
            raw=raw,
            path=split[0],
            subpath=split[1],
            angle=angle,
            embed=opener > 0 and masked[opener - 1] == "!",
        )


def iter_reference_destinations(text: str) -> Iterator[str]:
    """Decoded paths of "[ref]: dest" definitions (internal ones only)."""
    masked = _scannable(text)
    for match in _REFERENCE_DEF_RE.finditer(masked):
        raw = text[match.start(1) : match.end(1)].strip("<>")
        split = _split(raw)
        if split is not None:
            yield split[0]

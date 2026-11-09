from __future__ import annotations

import logging
import re

import frontmatter

from ..config import ConfigError, tags_source_from_env
from .markdown_scan import mask_non_content, mask_pattern
from .models import BlockRef, Callout, Note, Task, WikiLink

logger = logging.getLogger(__name__)

# [[Target]], [[Target|Alias]], [[Target#Heading]], [[Target#Heading|Alias]]
_WIKILINK_RE = re.compile(r"\[\[([^\]|#^\n]+)(?:#([^\]|^\n]+))?(?:\|([^\]\n]+))?\]\]")

# Bracketed "links" that are really data, e.g. tensor shapes [[1,320,320,3]]:
# only digits, commas, whitespace, minus signs and dots.
_NON_LINK_TARGET_RE = re.compile(r"^[\d,\s.\-]+$")

# [[Note^block-id]] or [[Note^block-id|Alias]]
_BLOCK_LINK_RE = re.compile(r"\[\[([^\]^]+)\^([\w-]+)(?:\|[^\]]*)?\]\]")

# Paragraph ending with ^block-id
_BLOCK_REF_RE = re.compile(r"^(.+?)\s+\^([\w-]+)\s*$", re.MULTILINE)

# > [!TYPE] Title\n> body lines
_CALLOUT_RE = re.compile(
    r"^> \[!([\w]+)\][ \t]*(.*?)\n((?:>[ \t]*.*\n?)*)",
    re.MULTILINE,
)

# A task bullet: "-", "*", "+" or "1." / "1)", optionally indented or inside a
# blockquote/callout, followed by a [ ] / [x] / [X] checkbox and the text.
_TASK_RE = re.compile(
    r"^[ \t>]*(?:[-*+]|\d+[.)])[ \t]+\[([ xX])\][ \t]+(\S.*?)[ \t]*$", re.MULTILINE
)

# Tasks-plugin emoji markers, extracted out of the task text separately.
_TASK_DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
_TASK_DONE_DATE_RE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
_TASK_PRIORITY_RE = re.compile(r"[⏫🔼🔽]")
_TASK_PRIORITY_MAP = {"⏫": "high", "🔼": "medium", "🔽": "low"}
_TASK_RECURRENCE_RE = re.compile(r"🔁\s*([^\n]*?)(?=\s*[📅✅⏫🔼🔽]|$)")

# key:: value  (Dataview inline fields — must start at line beginning)
_INLINE_FIELD_RE = re.compile(r"^([\w][\w /-]*)::[ \t]*(.+)$", re.MULTILINE)

# Any [[...]] wikilink or ![[...]] embed — the "#Heading" anchor inside
# [[Note#Heading]] is part of the link, not a tag (Obsidian doesn't index it).
_ANY_WIKILINK_RE = re.compile(r"!?\[\[[^\]\n]*\]\]")

# Markdown links/images [text](target) and bare or <angle-bracketed> URLs —
# "#fragment" parts of URLs (#L79, #discussion_r123, ...) are not tags.
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)")
_URL_RE = re.compile(r"(?:\b[a-zA-Z][a-zA-Z0-9+.\-]*://|\bwww\.)[^\s<>()\[\]]*")

# An inline tag: "#" at start of line or after whitespace, then Obsidian's tag
# charset (Unicode letters, digits, "_", "-", "/").
_INLINE_TAG_RE = re.compile(r"(?:(?<=\s)|^)#([\w/-]+)", re.MULTILINE)

# A tag must contain at least one character that is not a digit (or "/") —
# Obsidian doesn't treat "#1" or "#2024" as tags ("see point #9").
_NUMERIC_TAG_RE = re.compile(r"^[\d/]+$")

# Hex colors (#007AFF, #2147B1cc, #a1b) and hex identifiers such as commit or
# UUID fragments (#b7c3, #6e4c0f44-26e3): only hex digits (optionally in
# "-"-separated groups) with at least one digit AND one letter, so real words
# (#add, #facade) and numeric-ish tags (#2024-01) survive.
_HEX_LIKE_RE = re.compile(r"^(?=.*\d)(?=.*[a-fA-F])[0-9a-fA-F]+(?:-[0-9a-fA-F]+)*$")


def _default_tags_source() -> str:
    try:
        return tags_source_from_env()
    except ConfigError:
        return "both"


def parse_note(raw_content: str, path: str = "", tags_source: str | None = None) -> Note:
    """Parse a note. ``tags_source`` ('frontmatter' | 'inline' | 'both')
    defaults to the TAGS_SOURCE setting."""
    try:
        post = frontmatter.loads(raw_content)
        fm = dict(post.metadata)
        body = post.content
    except Exception:
        logger.warning("Failed to parse frontmatter in %s – treating as plain content", path)
        fm = {}
        body = raw_content

    # The parsed body drops the frontmatter and surrounding blank lines; task
    # line numbers are reported against the whole file so a caller can act on
    # them (set_task_status_tool) without knowing about that offset.
    body_start = raw_content.rfind(body) if body else -1
    line_offset = raw_content.count("\n", 0, body_start) if body_start > 0 else 0

    return Note(
        path=path,
        frontmatter=fm,
        tags=extract_tags(fm, body, source=tags_source or _default_tags_source()),
        aliases=extract_aliases(fm),
        wikilinks=extract_wikilinks(body),
        block_refs=extract_block_refs(body),
        block_links=extract_block_links(body),
        callouts=extract_callouts(body),
        tasks=extract_tasks(body, line_offset=line_offset),
        inline_fields=extract_inline_fields(body),
        content=body,
    )


def extract_aliases(fm: dict) -> list[str]:
    raw = fm.get("aliases", [])
    if isinstance(raw, str):
        return [a.strip() for a in raw.split(",") if a.strip()]
    if isinstance(raw, list):
        return [str(a).strip() for a in raw if a]
    return []


def is_valid_inline_tag(tag: str) -> bool:
    """Obsidian-style validity for the text after "#": not purely numeric
    and not a hex color code or hex identifier."""
    return bool(tag) and not _NUMERIC_TAG_RE.match(tag) and not _HEX_LIKE_RE.match(tag)


def extract_inline_tags(body: str) -> list[str]:
    """Inline ``#tags`` outside code, comments, links and URLs, in order of
    first appearance (deduplicated)."""
    scan = mask_non_content(body)
    for pattern in (_ANY_WIKILINK_RE, _MARKDOWN_LINK_RE, _URL_RE):
        scan = mask_pattern(scan, pattern)
    tags: list[str] = []
    for match in _INLINE_TAG_RE.finditer(scan):
        tag = match.group(1).rstrip("/")
        if is_valid_inline_tag(tag) and tag not in tags:
            tags.append(tag)
    return tags


def extract_frontmatter_tags(fm: dict) -> list[str]:
    fm_tags = fm.get("tags", [])
    if isinstance(fm_tags, str):
        fm_tags = [t.strip() for t in fm_tags.split(",")]
    tags: list[str] = []
    if isinstance(fm_tags, list):
        for raw in fm_tags:
            tag = str(raw).strip().lstrip("#") if raw else ""
            if tag and tag not in tags:
                tags.append(tag)
    return tags


def extract_tags(fm: dict, body: str, source: str = "both") -> list[str]:
    """Tags from the frontmatter ``tags`` property and/or inline ``#tags``.
    source: 'frontmatter' | 'inline' | 'both'."""
    tags: list[str] = []
    if source in ("frontmatter", "both"):
        tags.extend(extract_frontmatter_tags(fm))
    if source in ("inline", "both"):
        tags.extend(tag for tag in extract_inline_tags(body) if tag not in tags)
    return tags


def is_link_target(target: str) -> bool:
    """False for bracketed data that merely looks like a wikilink, such as
    nested brackets or tensor shapes ``[[1,320,320,3]]``."""
    return bool(target) and not target.startswith("[") and not _NON_LINK_TARGET_RE.match(target)


def extract_wikilinks(body: str) -> list[WikiLink]:
    """Wikilinks and embeds outside code and ``%%`` comments."""
    links: list[WikiLink] = []
    for m in _WIKILINK_RE.finditer(mask_non_content(body)):
        # "\|" escapes the alias pipe inside Markdown tables.
        target = m.group(1).strip().removesuffix("\\").strip()
        if not is_link_target(target):
            continue
        heading = m.group(2).strip() if m.group(2) else None
        alias = m.group(3).strip() if m.group(3) else None
        embed = m.start() > 0 and body[m.start() - 1] == "!"
        links.append(WikiLink(target=target, heading=heading, alias=alias, embed=embed))
    return links


def extract_block_refs(body: str) -> list[BlockRef]:
    refs: list[BlockRef] = []
    for m in _BLOCK_REF_RE.finditer(body):
        line_num = body[: m.start()].count("\n") + 1
        refs.append(BlockRef(block_id=m.group(2), line=line_num, text=m.group(1).strip()))
    return refs


def extract_block_links(body: str) -> list[str]:
    return [f"{m.group(1)}^{m.group(2)}" for m in _BLOCK_LINK_RE.finditer(body)]


def extract_callouts(body: str) -> list[Callout]:
    callouts: list[Callout] = []
    for m in _CALLOUT_RE.finditer(body):
        callout_type = m.group(1).upper()
        title = m.group(2).strip()
        raw_body = m.group(3)
        clean_body = re.sub(r"^>[ \t]?", "", raw_body, flags=re.MULTILINE).strip()
        callouts.append(Callout(type=callout_type, title=title, body=clean_body))
    return callouts


def extract_task_markers(
    text: str,
) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Pull Tasks-plugin emoji markers (due/done-date/priority/recurrence) out
    of a task line, returning (clean_text, due, recurrence, priority, done_date)."""
    due = None
    if m := _TASK_DUE_RE.search(text):
        due = m.group(1)
        text = text[: m.start()] + text[m.end():]

    done_date = None
    if m := _TASK_DONE_DATE_RE.search(text):
        done_date = m.group(1)
        text = text[: m.start()] + text[m.end():]

    priority = None
    if m := _TASK_PRIORITY_RE.search(text):
        priority = _TASK_PRIORITY_MAP[m.group(0)]
        text = text[: m.start()] + text[m.end():]

    recurrence = None
    if m := _TASK_RECURRENCE_RE.search(text):
        recurrence = m.group(1).strip() or None
        text = text[: m.start()] + text[m.end():]

    return re.sub(r"[ \t]{2,}", " ", text).strip(), due, recurrence, priority, done_date


def extract_tasks(body: str, line_offset: int = 0) -> list[Task]:
    """Checkbox tasks outside code and ``%%`` comments. ``line_offset`` is
    added to each 1-based line number (lines preceding ``body`` in the file)."""
    tasks: list[Task] = []
    for m in _TASK_RE.finditer(mask_non_content(body)):
        line_num = body[: m.start()].count("\n") + 1 + line_offset
        done = m.group(1).lower() == "x"
        text, due, recurrence, priority, done_date = extract_task_markers(m.group(2).strip())
        tasks.append(
            Task(
                text=text,
                done=done,
                line=line_num,
                due=due,
                recurrence=recurrence,
                priority=priority,
                done_date=done_date,
            )
        )
    return tasks


def extract_inline_fields(body: str) -> dict[str, str]:
    """Extract Dataview-style inline fields (key:: value) from the note body."""
    return {
        m.group(1).strip(): m.group(2).strip()
        for m in _INLINE_FIELD_RE.finditer(body)
    }

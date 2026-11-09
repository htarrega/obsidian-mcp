from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from pathlib import Path

from ..config import get_config
from ..domain.parser import parse_note
from ..storage.filesystem import VaultStorage
from ..storage.policy import matches_path_rule
from .query import matches_frontmatter_filter
from .safe_regex import (
    RegexTimeoutError,
    RegexTooComplexError,
    RegexTooLongError,
    compile_user_regex,
)
from .vault_layout import created_info


def list_notes(folder: str = "", include_meta: bool = False) -> list:
    cfg = get_config()
    storage = VaultStorage.from_config(cfg)

    results = []
    for resolved in storage.list_files(folder):
        p, rel = resolved.absolute, resolved.relative
        if p.suffix.lower() != ".md":
            continue
        if _is_excluded(rel, cfg.exclude_paths):
            continue
        if include_meta:
            try:
                raw = storage.read_text(rel)
                note = parse_note(raw, path=rel)
                created, created_source = created_info(note.frontmatter, resolved.stat_result)
                results.append({
                    "path": rel,
                    "title": note.frontmatter.get("title", p.stem),
                    "tags": note.tags,
                    "status": note.frontmatter.get("status"),
                    "created": created,
                    "created_source": created_source,
                    "mtime": resolved.stat_result.st_mtime,
                })
            except Exception:
                results.append({"path": rel, "title": p.stem, "tags": [], "status": None,
                                "created": "", "created_source": "none", "mtime": 0.0})
        else:
            results.append(rel)

    return sorted(results, key=lambda x: x["path"] if isinstance(x, dict) else x)


def read_note(path: str) -> dict:
    cfg = get_config()
    raw, revision = VaultStorage.from_config(cfg).read_text_with_revision(path)
    note = parse_note(raw, path=path)
    return {
        "content": note.content,
        "frontmatter": note.frontmatter,
        "tags": note.tags,
        "aliases": note.aliases,
        "wikilinks": [
            {"target": wl.target, "alias": wl.alias, "heading": wl.heading}
            for wl in note.wikilinks
        ],
        "block_refs": [
            {"block_id": br.block_id, "line": br.line, "text": br.text}
            for br in note.block_refs
        ],
        "callouts": [
            {"type": c.type, "title": c.title, "body": c.body}
            for c in note.callouts
        ],
        "tasks": [
            {
                "text": t.text,
                "done": t.done,
                "line": t.line,
                "due": t.due,
                "recurrence": t.recurrence,
                "priority": t.priority,
                "done_date": t.done_date,
            }
            for t in note.tasks
        ],
        "inline_fields": note.inline_fields,
        "revision": revision.token,
    }


def search_notes(
    query: str,
    tag: str | None = None,
    mode: str = "exact",
    limit: int = 20,
    frontmatter_filter: dict | None = None,
    field: str | None = None,
    threshold: float = 0.8,
    max_snippets_per_note: int = 2,
    context_lines: int = 1,
) -> list[dict]:
    """Full-text search across the vault.
    mode: 'exact' (default) | 'regex' | 'fuzzy'.
    frontmatter_filter: same shape as query_notes_tool's (plain value = exact
    match, or an operator dict like {"$ne": v}/{"$nin": [...]}/{"$exists": bool}) —
    combines with the text search in a single call instead of filtering first
    and grepping results after.
    field: None/'body' (default, search line-by-line in the note body) |
    'filename' (match only against the file name, not its content).
    threshold: fuzzy-match similarity cutoff (0-1, only used when mode='fuzzy').
    max_snippets_per_note / context_lines: shape of the returned snippets
    (overlapping windows are merged; long lines are clipped around the match).
    exact/fuzzy matching is case- and accent-insensitive ("Siplan" finds
    "Siplán"); regex mode matches the raw text."""
    if not 1 <= max_snippets_per_note <= MAX_SNIPPETS_PER_NOTE:
        raise ValueError(
            f"max_snippets_per_note must be between 1 and {MAX_SNIPPETS_PER_NOTE}"
        )
    if not 0 <= context_lines <= MAX_CONTEXT_LINES:
        raise ValueError(f"context_lines must be between 0 and {MAX_CONTEXT_LINES}")
    cfg = get_config()
    storage = VaultStorage.from_config(cfg)
    results: list[dict] = []

    if frontmatter_filter:
        matches_frontmatter_filter({}, frontmatter_filter)  # validate operators up front

    try:
        pattern = compile_user_regex(query, re.IGNORECASE) if mode == "regex" else None
    except (RegexTooLongError, RegexTooComplexError):
        raise
    except ValueError:
        pattern = None  # invalid regex: no matches, as before

    for resolved in storage.list_files():
        p, rel = resolved.absolute, resolved.relative
        if p.suffix.lower() != ".md":
            continue
        if _is_excluded(rel, cfg.exclude_paths):
            continue
        try:
            raw = storage.read_text(rel)
            note = parse_note(raw, path=rel)
            if tag and tag not in note.tags:
                continue
            if frontmatter_filter and not matches_frontmatter_filter(note.frontmatter, frontmatter_filter):
                continue

            if field == "filename":
                snippets, score = _match_filename(p.name, query, pattern, mode, threshold)
            else:
                lines = raw.splitlines()
                snippets, score = _find_snippets(
                    lines, query, pattern, mode, threshold,
                    max_snippets=max_snippets_per_note, context_lines=context_lines,
                )
            if score == 0:
                continue

            results.append({"path": rel, "score": score, "snippets": snippets, "tags": note.tags})
        except RegexTimeoutError:
            raise
        except Exception:
            pass

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def _match_filename(
    name: str,
    query: str,
    pattern: re.Pattern | None,
    mode: str,
    threshold: float,
) -> tuple[list[dict], int]:
    """Match `query` against a bare filename instead of note body lines —
    reuses _find_snippets' line-scoring logic on a single-line input."""
    snippets, score = _find_snippets([name], query, pattern, mode, threshold)
    if snippets:
        snippets = [{"line": 0, "context": [{"line": 0, "text": name, "match": True}]}]
    return snippets, score


_EMBED_RE = re.compile(r"!\[\[([^\]#|^]+?)(?:#([^\]|^]+?))?(?:\|[^\]]*)?\]\]")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def get_note_outline(path: str) -> dict:
    """Return the structural map of a note: headings, block refs, frontmatter keys.
    Does not return body text — efficient for large notes."""
    cfg = get_config()
    raw, revision = VaultStorage.from_config(cfg).read_text_with_revision(path)
    note = parse_note(raw, path=path)

    headings = [
        {"level": len(m.group(1)), "text": m.group(2).strip(), "line": raw[:m.start()].count("\n") + 1}
        for m in _HEADING_RE.finditer(raw)
    ]
    return {
        "path": path,
        "headings": headings,
        "block_refs": [
            {"block_id": br.block_id, "line": br.line, "text": br.text[:80]}
            for br in note.block_refs
        ],
        "frontmatter_keys": list(note.frontmatter.keys()),
        "tags": note.tags,
        "aliases": note.aliases,
        "inline_fields": note.inline_fields,
        "word_count": len(raw.split()),
        "line_count": len(raw.splitlines()),
        "revision": revision.token,
    }


def render_note(path: str, depth: int = 1) -> str:
    """Read a note and resolve all ![[embed]] transclusions inline.
    depth controls recursion (0 = raw content, 1 = one level, 2 = two levels)."""
    cfg = get_config()
    storage = VaultStorage.from_config(cfg)
    raw = storage.read_text(path)
    if depth == 0:
        return raw

    def _replace(m: re.Match) -> str:
        target_stem = m.group(1).strip()
        heading = (m.group(2) or "").strip()
        target_rel = _find_note_by_stem(cfg.vault_path, target_stem, cfg.exclude_paths, storage=storage)
        if target_rel is None:
            return m.group(0)
        try:
            target_raw = storage.read_text(target_rel)
            if heading:
                target_raw = _extract_section(target_raw, heading)
            if depth > 1:
                target_raw = render_note(target_rel, depth=depth - 1)
            return f"\n---\n*from [[{target_stem}]]:*\n{target_raw.strip()}\n---\n"
        except Exception:
            return m.group(0)

    return _EMBED_RE.sub(_replace, raw)


def _find_note_by_stem(
    root: Path,
    stem: str,
    exclude_paths: Iterable[str],
    *,
    storage: VaultStorage | None = None,
) -> str | None:
    stem_lower = stem.lower()
    # A missing gateway is a programming error/closed result, not permission
    # to fall back to unrestricted filesystem traversal.
    if storage is None:
        return None
    candidates = storage.list_files()
    for resolved in candidates:
        p, rel = resolved.absolute, resolved.relative
        if p.suffix.lower() != ".md":
            continue
        if _is_excluded(rel, exclude_paths):
            continue
        if p.stem.lower() == stem_lower:
            return rel
    return None


def _extract_section(content: str, heading: str) -> str:
    """Extract the content under a Markdown heading."""
    lines = content.splitlines(keepends=True)
    heading_lower = heading.lower()
    in_section = False
    section_level = 0
    result: list[str] = []
    for line in lines:
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            title = line.lstrip("#").strip()
            if not in_section:
                if title.lower() == heading_lower:
                    in_section = True
                    section_level = level
                    result.append(line)
            else:
                if level <= section_level:
                    break
                result.append(line)
        elif in_section:
            result.append(line)
    return "".join(result) if result else content


MAX_SNIPPETS_PER_NOTE = 10
MAX_CONTEXT_LINES = 5
# Longest text returned for a single snippet line; longer lines are clipped
# (around the match on matching lines) and marked with an ellipsis.
SNIPPET_LINE_MAX_CHARS = 240


def _fold(text: str) -> tuple[str, list[int]]:
    """Lower-case and strip diacritics ("Siplán" -> "siplan").

    Returns the folded text plus, for each folded character, the index of the
    original character it came from, so match offsets map back to the raw line.
    """
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if ch.isascii():
            folded = ch.lower()
        else:
            folded = "".join(
                c for c in unicodedata.normalize("NFKD", ch)
                if not unicodedata.combining(c)
            ).lower()
        out.append(folded)
        index.extend([i] * len(folded))
    return "".join(out), index


def _clip(text: str, center: int | None) -> str:
    """Cap a snippet line at SNIPPET_LINE_MAX_CHARS, keeping `center` visible."""
    limit = SNIPPET_LINE_MAX_CHARS
    if len(text) <= limit:
        return text
    if center is None:
        return text[: limit - 1] + "…"
    start = max(0, min(center - limit // 3, len(text) - limit))
    end = start + limit
    clipped = text[start:end]
    if start > 0:
        clipped = "…" + clipped[1:]
    if end < len(text):
        clipped = clipped[:-1] + "…"
    return clipped


def _find_snippets(
    lines: list[str],
    query: str,
    pattern: re.Pattern | None,
    mode: str,
    threshold: float = 0.8,
    max_snippets: int = 3,
    context_lines: int = 2,
) -> tuple[list[dict], int]:
    """Score every line and build merged, length-capped context windows.

    The score sums over all matching lines; only the first `max_snippets`
    windows (after merging overlapping/adjacent ones) are returned.
    """
    total_score = 0
    query_folded, _ = _fold(query)
    matches: dict[int, int | None] = {}  # line index -> match offset

    for i, line in enumerate(lines):
        offset: int | None = None
        if mode == "regex" and pattern:
            m = pattern.search(line)
            line_score = 2 if m else 0
            offset = m.start() if m else None
        else:
            line_folded, index = _fold(line)
            if mode == "fuzzy":
                line_score = _fuzzy_score(line_folded, query_folded, threshold)
            elif query_folded and query_folded in line_folded:
                if re.search(rf"\b{re.escape(query_folded)}\b", line_folded):
                    line_score = 3 if line_folded == query_folded else 2
                else:
                    line_score = 1
            else:
                line_score = 0
            pos = line_folded.find(query_folded) if query_folded else -1
            if pos >= 0 and index:
                offset = index[pos]

        if line_score > 0:
            total_score += line_score
            matches[i] = offset

    # Merge overlapping/adjacent windows, but cap a merged window's height so a
    # run of matching lines cannot turn into one huge snippet.
    max_height = 2 * context_lines + 5
    windows: list[list[int]] = []  # [start, end) line ranges
    for i in matches:
        start = max(0, i - context_lines)
        end = min(len(lines), i + context_lines + 1)
        if windows and start <= windows[-1][1]:
            last = windows[-1]
            if i < last[1]:
                continue  # already visible in the previous window
            capped_end = min(end, last[0] + max_height)
            if capped_end > i:
                last[1] = max(last[1], capped_end)
                continue
            start = last[1]
        if len(windows) >= max_snippets:
            break
        windows.append([start, end])

    snippets: list[dict] = []
    for start, end in windows:
        first_match = next(j for j in range(start, end) if j in matches)
        snippets.append({
            "line": first_match + 1,
            "context": [
                {
                    "line": j + 1,
                    "text": _clip(lines[j], matches.get(j)),
                    "match": j in matches,
                }
                for j in range(start, end)
            ],
        })
    return snippets, total_score


def _fuzzy_score(line_lower: str, query_lower: str, threshold: float = 0.8) -> int:
    """Fuzzy match: all query words must find a similar word in the line
    (ratio >= threshold, default 0.8 — lower it for looser/more tolerant
    matches, raise it to cut down on unrelated noise)."""
    if query_lower in line_lower:
        return 2
    query_words = query_lower.split()
    line_words = line_lower.split()
    matched_words = 0
    for qw in query_words:
        if any(SequenceMatcher(None, qw, lw).ratio() >= threshold for lw in line_words):
            matched_words += 1
    return matched_words if matched_words == len(query_words) and query_words else 0


def _is_excluded(rel_path: str, exclude_paths: Iterable[str]) -> bool:
    normalized = rel_path.replace("\\", "/").strip("/")
    return any(matches_path_rule(normalized, rule) for rule in exclude_paths)

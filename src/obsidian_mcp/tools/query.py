from __future__ import annotations

from collections import deque
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..config import get_config
from ..domain.index import VaultIndex
from ..domain.parser import parse_note
from ..layout import PERIOD_TEMPLATE_NAMES, path_in_folders, render_periodic_path, same_path
from ..storage.filesystem import VaultStorage
from .scoping import ReadScope
from .vault_layout import NoteFilters, created_info, read_conventions_file


def _load_note(vault_path, note_path: str):
    """Read and parse a single note. Thin helper to avoid repetition."""
    return parse_note(VaultStorage.from_config().read_text(note_path), path=note_path)


def _readable_notes(index: VaultIndex, scope: ReadScope | None = None) -> list[str]:
    """Every indexed note the calling identity may see, sorted."""
    scope = scope or ReadScope.current()
    return sorted(scope.filter(index.get_all_notes()))


def get_backlinks(path: str, index: VaultIndex) -> list[str]:
    # A backlink from a note this identity may not read would leak that note's
    # existence and path, so the result is scoped like a direct read.
    return ReadScope.current().filter(index.get_backlinks(path))


def get_vault_conventions() -> str:
    """Raw text of the configured conventions file (CONVENTIONS_PATH), or ""."""
    return read_conventions_file()


def get_broken_links(index: VaultIndex) -> list[dict]:
    cfg = get_config()
    results: list[dict] = []
    for note_path in _readable_notes(index):
        try:
            note = _load_note(cfg.vault_path, note_path)
            for wl in note.wikilinks:
                if not index.has_note(wl.target):
                    results.append({"source": note_path, "link": wl.target})
        except Exception:
            pass
    return results


def get_orphans(
    index: VaultIndex,
    exclude_folders: list[str] | None = None,
    entry_note: str | None = None,
) -> list[str]:
    """exclude_folders: single-segment names match at any depth, entries with
    '/' are rooted prefixes (case/Unicode-insensitive). entry_note is never
    reported."""
    excluded = tuple(exclude_folders or ())
    scope = ReadScope.current()
    orphans = []
    for note_path in _readable_notes(index, scope):
        if excluded and path_in_folders(note_path, excluded):
            continue
        if same_path(note_path, entry_note):
            continue
        # Only visible backlinks count: an invisible one would otherwise
        # reveal that some unreadable note links here.
        if not scope.filter(index.get_backlinks(note_path)):
            orphans.append(note_path)
    return orphans


def get_link_graph(
    root: str,
    index: VaultIndex,
    depth: int = 2,
    direction: str = "both",
) -> dict:
    cfg = get_config()
    scope = ReadScope.current()
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(root, 0)])

    def _meta(path: str) -> dict:
        try:
            note = _load_note(cfg.vault_path, path)
            return {
                "path": path,
                "title": note.frontmatter.get("title", Path(path).stem),
                "tags": note.tags,
            }
        except Exception:
            return {"path": path, "title": Path(path).stem, "tags": []}

    while queue:
        current, level = queue.popleft()
        if current in visited or level > depth:
            continue
        # A note outside this identity's read scope is neither reported nor
        # traversed through — an unreadable hop must not leak its own path or
        # the paths it links on to.
        if not scope.allows(current):
            continue
        visited.add(current)
        nodes[current] = _meta(current)

        if direction in ("outgoing", "both"):
            for target in sorted(index.get_outlinks(current)):
                resolved = index.resolve_alias(target) or target
                if not scope.allows(resolved):
                    continue
                edges.append({"from": current, "to": resolved, "type": "outgoing"})
                if resolved not in visited:
                    queue.append((resolved, level + 1))

        if direction in ("incoming", "both"):
            for source in scope.filter(index.get_backlinks(current)):
                edges.append({"from": source, "to": current, "type": "incoming"})
                if source not in visited:
                    queue.append((source, level + 1))

    # Deduplicate edges
    seen_edges: set[tuple] = set()
    unique_edges = []
    for e in edges:
        key = (e["from"], e["to"])
        if key not in seen_edges:
            seen_edges.add(key)
            unique_edges.append(e)

    return {"root": root, "nodes": list(nodes.values()), "edges": unique_edges}


def get_vault_stats(index: VaultIndex) -> dict:
    # Aggregate counts are computed over the visible notes only, so they never
    # hint at how much content sits outside this identity's read scope.
    scope = ReadScope.current()
    all_notes = _readable_notes(index, scope)
    total_links = sum(len(index.get_outlinks(p)) for p in all_notes)
    orphans = get_orphans(index)
    broken = get_broken_links(index)

    # Most linked = notes with most (visible) backlinks. Each entry carries its
    # count so numbered names like "(300) Permanent" are not read as counters.
    backlink_counts = {p: len(scope.filter(index.get_backlinks(p))) for p in all_notes}
    by_backlinks = sorted(
        (p for p, n in backlink_counts.items() if n > 0),
        key=lambda p: (-backlink_counts[p], p),
    )

    return {
        "total_notes": len(all_notes),
        "total_links": total_links,
        "orphans_count": len(orphans),
        "broken_links_count": len(broken),
        "most_linked": [
            {"path": p, "backlinks": backlink_counts[p]} for p in by_backlinks[:5]
        ],
        "index_ready": index.is_ready(),
    }


def get_tag_tree(index: VaultIndex) -> dict:
    scope = ReadScope.current()
    return index.get_tag_tree(include=None if scope.unrestricted else scope.allows)


def get_tasks(
    index: VaultIndex,
    status: str = "open",
    folder: str = "",
    tag: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
) -> list[dict]:
    """due_before/due_after: 'YYYY-MM-DD', inclusive, compared against each
    task's 📅 due date (tasks without a due date never match either filter)."""
    cfg = get_config()
    results: list[dict] = []

    for note_path in _readable_notes(index):
        if folder and not note_path.startswith(folder.rstrip("/") + "/"):
            continue
        try:
            note = _load_note(cfg.vault_path, note_path)
            if tag and tag not in note.tags:
                continue
            for task in note.tasks:
                if status == "open" and task.done:
                    continue
                if status == "done" and not task.done:
                    continue
                if (due_before or due_after) and not task.due:
                    continue
                if due_before and task.due > due_before:
                    continue
                if due_after and task.due < due_after:
                    continue
                results.append({
                    "text": task.text,
                    "done": task.done,
                    "source": note_path,
                    "line": task.line,
                    "due": task.due,
                    "recurrence": task.recurrence,
                    "priority": task.priority,
                    "done_date": task.done_date,
                })
        except Exception:
            pass
    return results


def resolve_alias(name: str, index: VaultIndex) -> str | None:
    resolved = index.resolve_alias(name)
    if resolved is None or not ReadScope.current().allows(resolved):
        return None
    return resolved


def list_all_tags(index: VaultIndex, sort_by: str = "count") -> list[dict]:
    """Return all tags in the vault with note counts.
    sort_by: 'count' (descending) | 'name' (ascending)."""
    scope = ReadScope.current()
    counts = index.get_all_tags_with_counts(
        include=None if scope.unrestricted else scope.allows
    )
    tags = [{"tag": tag, "count": count} for tag, count in counts.items()]
    if sort_by == "name":
        tags.sort(key=lambda x: x["tag"])
    else:
        tags.sort(key=lambda x: (-x["count"], x["tag"]))
    return tags


def get_periodic_note(index: VaultIndex, period: str = "daily", date_str: str = "today") -> dict:
    """Read or preview a periodic note (daily/weekly/monthly/quarterly/yearly).
    date_str: 'today' | 'yesterday' | ISO date string (YYYY-MM-DD)."""
    cfg = get_config()

    if date_str == "today":
        target = date.today()
    elif date_str == "yesterday":
        target = date.today() - timedelta(days=1)
    else:
        target = date.fromisoformat(date_str)

    if period not in PERIOD_TEMPLATE_NAMES:
        raise ValueError(f"Unknown period {period!r}. Use: daily|weekly|monthly|quarterly|yearly")
    layout = cfg.layout
    pattern = layout.periodic_pattern(period)
    configured = layout.is_configured(f"periodic.{period}")
    rel_path = render_periodic_path(pattern, period, target)
    note_id = Path(rel_path).stem
    template_name = PERIOD_TEMPLATE_NAMES[period]

    storage = VaultStorage.from_config(cfg)
    target = storage.resolve_read(rel_path)
    if storage.exists(target.relative):
        raw = storage.read_text(target.relative)
        note = parse_note(raw, path=rel_path)
        return {
            "path": rel_path,
            "period": period,
            "date": note_id,
            "exists": True,
            "pattern": pattern,
            "configured": configured,
            "content": note.content,
            "frontmatter": note.frontmatter,
            "tasks": [{"text": t.text, "done": t.done, "line": t.line} for t in note.tasks],
        }

    # Preview from template if available
    content = ""
    template_rel: str | None = f"{layout.templates_dir}/{template_name}"
    try:
        raw_tpl = storage.read_text(template_rel)
        content = raw_tpl.replace("{{date}}", note_id).replace("{{title}}", note_id)
    except (FileNotFoundError, PermissionError, IsADirectoryError, OSError):
        template_rel = None

    env_name = f"PERIODIC_{period.upper()}_PATH"
    hint = (
        f"No {period} note at {rel_path}. Create it there (e.g. with write_note_tool) "
        "if the user wants one."
    )
    if not configured:
        hint += (
            f" The path comes from the default pattern {pattern!r}; this vault may not "
            f"use {period} notes — the server admin can set {env_name} "
            f"(or periodic.{period} in vaults.json)."
        )
    return {"path": rel_path, "period": period, "date": note_id, "exists": False,
            "suggested_path": rel_path, "pattern": pattern, "configured": configured,
            "template": template_rel, "hint": hint,
            "content": content, "frontmatter": {}, "tasks": []}


def _membership(actual, expected, *, negate: bool) -> bool:
    if not isinstance(expected, (list, tuple, set, frozenset)):
        raise ValueError("frontmatter_filter '$in'/'$nin' requires a list value")
    return (actual not in expected) if negate else (actual in expected)


_FM_OPERATORS = {
    "$ne": lambda actual, expected: actual != expected,
    "$eq": lambda actual, expected: actual == expected,
    "$in": lambda actual, expected: _membership(actual, expected, negate=False),
    "$nin": lambda actual, expected: _membership(actual, expected, negate=True),
    "$exists": lambda actual, expected: (actual is not None) == bool(expected),
}


def matches_frontmatter_filter(frontmatter: dict, frontmatter_filter: dict) -> bool:
    """Check a note's frontmatter against a filter dict.

    Each value is either a plain value (exact match, backward compatible)
    or an operator dict like {"$ne": "done"}, {"$nin": [...]}, or
    {"$exists": True} ("$exists": False matches a missing/None field).
    Multiple operators on the same field are all required to hold (AND).
    """
    for key, expected in frontmatter_filter.items():
        actual = frontmatter.get(key)
        if isinstance(expected, dict) and expected and all(k.startswith("$") for k in expected):
            for op, op_value in expected.items():
                fn = _FM_OPERATORS.get(op)
                if fn is None:
                    raise ValueError(f"Unknown frontmatter_filter operator: {op!r}")
                if not fn(actual, op_value):
                    return False
        else:
            if actual != expected:
                return False
    return True


def query_notes(
    index: VaultIndex,
    tags: list[str] | None = None,
    status: str | None = None,
    frontmatter_filter: dict | None = None,
    inline_field_filter: dict | None = None,
    sort_by: str = "path",
    sort_desc: bool = False,
    limit: int = 50,
    folder: str = "",
    title_contains: str | None = None,
    path_glob: str | None = None,
    mtime_after: str | None = None,
    mtime_before: str | None = None,
    min_words: int | None = None,
    has_tasks: bool | None = None,
) -> list[dict]:
    """Filter notes by tags, status, or arbitrary frontmatter fields.
    sort_by: 'path' | 'title' | 'created' | 'mtime'. ``created`` falls back
    to the file's birth time/mtime when frontmatter has none
    (``created_source`` says which).
    frontmatter_filter values may be a plain value (exact match) or an
    operator dict: {"$ne": v}, {"$eq": v}, {"$in": [...]}, {"$nin": [...]},
    {"$exists": True|False}."""
    cfg = get_config()
    storage = VaultStorage.from_config(cfg)
    all_notes = _readable_notes(index)
    results: list[dict] = []

    if frontmatter_filter:
        matches_frontmatter_filter({}, frontmatter_filter)  # validate operators up front
    filters = NoteFilters(
        title_contains=title_contains,
        path_glob=path_glob,
        mtime_after=mtime_after,
        mtime_before=mtime_before,
        min_words=min_words,
        has_tasks=has_tasks,
    )

    for note_path in all_notes:
        if folder and not note_path.startswith(folder.rstrip("/") + "/"):
            continue
        if not filters.matches_path(note_path):
            continue
        try:
            full = storage.resolve_read(note_path)
            stat_result = storage.stat(full.relative) if storage.exists(full.relative) else None
            mtime = stat_result.st_mtime if stat_result is not None else 0.0
            if not filters.matches_mtime(mtime):
                continue
            note = _load_note(cfg.vault_path, note_path)

            if tags and not all(t in note.tags for t in tags):
                continue

            note_status = note.frontmatter.get("status")
            if status is not None and note_status != status:
                continue

            if frontmatter_filter and not matches_frontmatter_filter(note.frontmatter, frontmatter_filter):
                continue

            if inline_field_filter and not all(
                note.inline_fields.get(k) == str(v) for k, v in inline_field_filter.items()
            ):
                continue

            title = note.frontmatter.get("title", Path(note_path).stem)
            if filters.active and not filters.matches_note(note_path, title, note):
                continue

            created, created_source = created_info(note.frontmatter, stat_result)
            results.append({
                "path": note_path,
                "title": title,
                "tags": note.tags,
                "status": note_status,
                "created": created,
                "created_source": created_source,
                "mtime": mtime,
                "frontmatter": note.frontmatter,
                "inline_fields": note.inline_fields,
            })
        except Exception:
            pass

    _sort_keys: dict[str, Any] = {
        "path": lambda x: x["path"],
        "title": lambda x: str(x["title"]).lower(),
        "created": lambda x: x["created"],
        "mtime": lambda x: x["mtime"],
    }
    key_fn = _sort_keys.get(sort_by, _sort_keys["path"])
    results.sort(key=key_fn, reverse=sort_desc)
    return results[:limit]

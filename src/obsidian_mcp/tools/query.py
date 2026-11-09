from __future__ import annotations

import fnmatch
from collections import deque
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..config import get_config
from ..domain.index import VaultIndex
from ..domain.links import LinkResolver, fold, link_kind
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


def build_link_resolver(index: VaultIndex) -> LinkResolver:
    """Resolver over every linkable vault file plus the index's aliases."""
    return LinkResolver(index.list_link_targets(), index.get_alias_map())


def find_broken_links(index: VaultIndex) -> dict:
    """Broken wikilinks/embeds plus ambiguity warnings.

    Returns {items: [{source, link, kind, suggestion}], warnings: [...]}.
    One item per distinct (source, link); ``kind`` is 'attachment' or 'note';
    ``suggestion`` is the closest existing file (accent/case-insensitive name
    similarity) or None. A warning is emitted for each bare link name that
    matches several notes/files (Obsidian then picks one by path)."""
    cfg = get_config()
    scope = ReadScope.current()
    resolver = build_link_resolver(index)
    include = None if scope.unrestricted else scope.allows
    items: list[dict] = []
    ambiguous: dict[str, dict] = {}
    for note_path in _readable_notes(index, scope):
        try:
            note = _load_note(cfg.vault_path, note_path)
        except Exception:
            continue
        seen: set[str] = set()
        for wl in note.wikilinks:
            key = fold(wl.target)
            if key in seen:
                continue
            seen.add(key)
            candidates = resolver.candidates(wl.target, source=note_path)
            if not candidates:
                items.append({
                    "source": note_path,
                    "link": wl.target,
                    "kind": link_kind(wl.target),
                    "suggestion": resolver.suggest(wl.target, include=include),
                })
                continue
            visible = [c for c in candidates if include is None or include(c)]
            if len(visible) > 1 and "/" not in wl.target:
                entry = ambiguous.setdefault(
                    key,
                    {"type": "ambiguous_link", "link": wl.target, "candidates": sorted(visible),
                     "sources": []},
                )
                entry["sources"].append(note_path)
    return {"items": items, "warnings": list(ambiguous.values())}


def get_broken_links(index: VaultIndex) -> list[dict]:
    """Broken links only (see find_broken_links)."""
    return find_broken_links(index)["items"]


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


TASKS_DEFAULT_LIMIT = 100
TASKS_MAX_LIMIT = 1000
TASK_GROUP_BY = ("none", "source")


def _normalize_task_text(text: str) -> str:
    return " ".join(text.split()).casefold()


def _folder_excluded(note_path: str, exclude_folders: list[str]) -> bool:
    """A folder with a "/" is a vault-relative prefix; a bare name matches
    any folder segment of the path."""
    parts = note_path.split("/")[:-1]
    for folder in exclude_folders:
        folder = folder.strip().strip("/")
        if not folder:
            continue
        if "/" in folder:
            if note_path.startswith(folder + "/"):
                return True
        elif folder in parts:
            return True
    return False


def _dedupe_tasks(tasks: list[dict]) -> list[dict]:
    """Collapse tasks with the same normalized text in the same note, keeping
    the last occurrence (by line) with occurrences/first_line/last_line."""
    groups: dict[tuple[str, str], dict] = {}
    for task in tasks:
        key = (task["source"], _normalize_task_text(task["text"]))
        group = groups.get(key)
        if group is None:
            groups[key] = {**task, "occurrences": 1, "first_line": task["line"],
                           "last_line": task["line"]}
            continue
        first_line = min(group["first_line"], task["line"])
        occurrences = group["occurrences"] + 1
        if task["line"] >= group["last_line"]:
            group.clear()
            group.update(task)
            group["last_line"] = task["line"]
        group["first_line"] = first_line
        group["occurrences"] = occurrences
    return sorted(groups.values(), key=lambda t: (t["source"], t["line"]))


def get_tasks(
    index: VaultIndex,
    status: str = "open",
    folder: str = "",
    tag: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    *,
    exclude_folders: list[str] | None = None,
    path_glob: str | None = None,
    dedupe: bool = False,
) -> list[dict]:
    """Every matching task, unpaged (see get_tasks_page for the tool view).

    status: 'open' | 'done' | 'all'. due_before/due_after: 'YYYY-MM-DD',
    inclusive, compared against each task's 📅 due date (tasks without a due
    date never match either filter). path_glob: fnmatch-style pattern on the
    note path, case-insensitive ("*" also crosses "/")."""
    cfg = get_config()
    results: list[dict] = []
    exclude_folders = exclude_folders or []
    glob = path_glob.casefold() if path_glob else None

    for note_path in _readable_notes(index):
        if folder and not note_path.startswith(folder.rstrip("/") + "/"):
            continue
        if exclude_folders and _folder_excluded(note_path, exclude_folders):
            continue
        if glob and not fnmatch.fnmatchcase(note_path.casefold(), glob):
            continue
        try:
            note = _load_note(cfg.vault_path, note_path)
        except Exception:
            continue
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
    return _dedupe_tasks(results) if dedupe else results


def get_tasks_page(
    index: VaultIndex,
    status: str = "open",
    folder: str = "",
    tag: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    *,
    limit: int = TASKS_DEFAULT_LIMIT,
    offset: int = 0,
    dedupe: bool = False,
    group_by: str = "none",
    exclude_folders: list[str] | None = None,
    path_glob: str | None = None,
    summary_only: bool = False,
) -> dict:
    """Paged task listing: {items, meta: {total, returned, truncated, offset}}.

    group_by='source' groups the page's tasks per note. summary_only returns
    per-note counts {source, open, done, total} (status is ignored there) and
    pages over notes instead of tasks."""
    if group_by not in TASK_GROUP_BY:
        raise ValueError(f"Unknown group_by {group_by!r}; expected 'none' or 'source'")
    if offset < 0:
        raise ValueError("offset must be >= 0")
    limit = max(1, min(int(limit), TASKS_MAX_LIMIT))
    tasks = get_tasks(
        index,
        status="all" if summary_only else status,
        folder=folder,
        tag=tag,
        due_before=due_before,
        due_after=due_after,
        exclude_folders=exclude_folders,
        path_glob=path_glob,
        dedupe=dedupe,
    )
    if summary_only:
        counts: dict[str, dict] = {}
        for task in tasks:
            entry = counts.setdefault(
                task["source"], {"source": task["source"], "open": 0, "done": 0, "total": 0}
            )
            entry["done" if task["done"] else "open"] += 1
            entry["total"] += 1
        rows: list[dict] = list(counts.values())
    else:
        rows = tasks
    page = rows[offset:offset + limit]
    if group_by == "source" and not summary_only:
        grouped: dict[str, dict] = {}
        for task in page:
            entry = grouped.setdefault(task["source"], {"source": task["source"], "tasks": []})
            entry["tasks"].append({k: v for k, v in task.items() if k != "source"})
        items: list[dict] = list(grouped.values())
    else:
        items = page
    return {
        "items": items,
        "meta": {
            "total": len(rows),
            "returned": len(page),
            "truncated": offset + len(page) < len(rows),
            "offset": offset,
            "limit": limit,
        },
    }


def resolve_alias(name: str, index: VaultIndex) -> str | None:
    """Resolve a link target, alias, note stem or attachment file name
    (``mobileExec.png``, ``images/x.png``) to its vault path."""
    resolved = build_link_resolver(index).resolve(name) or index.resolve_alias(name)
    if resolved is None or not ReadScope.current().allows(resolved):
        return None
    return resolved


def resolve_note_ref(ref: str, index: VaultIndex) -> tuple[str, list[str]]:
    """Resolve a note reference to a vault path: ``(path, other_candidates)``.

    ``ref`` is either a note path (``Folder/Note.md``) or a bare note name /
    link target (``Note``, ``note``, ``Folder/Note``, an alias), resolved like
    an Obsidian wikilink: case-insensitive, by file name anywhere in the vault,
    the shortest path winning when several notes share a name. An existing
    ``.md`` path is returned as is without consulting the index. Raises
    FileNotFoundError if nothing readable matches.
    """
    ref = ref.strip()
    if not ref:
        raise ValueError("A note path or name is required")
    storage = VaultStorage.from_config()
    if ref.lower().endswith(".md") and storage.exists(ref, read=True):
        return storage.resolve_read(ref).relative, []

    scope = ReadScope.current()
    resolver = build_link_resolver(index)
    candidates = [
        path
        for path in resolver.candidates(ref)
        if path.lower().endswith(".md") and scope.allows(path)
    ]
    if not candidates:
        raise FileNotFoundError(f"Note not found: {ref!r}")
    preferred = resolver.resolve(ref)
    if preferred not in candidates:
        preferred = min(candidates, key=lambda path: (path.count("/"), len(path), path))
    return preferred, sorted(path for path in candidates if path != preferred)


def get_outgoing_links(path: str, index: VaultIndex) -> list[dict]:
    """Wikilinks and embeds of one note, resolved with Obsidian semantics.

    One item per distinct (target, heading, embed): {target, heading, embed,
    kind: 'note'|'attachment', resolved: path | None, count}. A target outside
    the caller's read scope is reported as unresolved, so its existence does
    not leak."""
    note = parse_note(VaultStorage.from_config().read_text(path), path=path)
    scope = ReadScope.current()
    resolver = build_link_resolver(index)
    items: dict[tuple, dict] = {}
    for link in note.wikilinks:
        key = (fold(link.target), fold(link.heading or ""), link.embed)
        if key in items:
            items[key]["count"] += 1
            continue
        resolved = resolver.resolve(link.target, source=path)
        if resolved is not None and not scope.allows(resolved):
            resolved = None
        kind = link_kind(resolved if resolved else link.target)
        if resolved and resolved.lower().endswith(".md"):
            kind = "note"
        items[key] = {
            "target": link.target,
            "heading": link.heading,
            "embed": link.embed,
            "kind": kind,
            "resolved": resolved,
            "count": 1,
        }
    return list(items.values())


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

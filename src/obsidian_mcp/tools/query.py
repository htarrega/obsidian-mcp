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
from ..storage.filesystem import VaultStorage
from .scoping import ReadScope


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
    cfg = get_config()
    storage = VaultStorage.from_config(cfg)
    try:
        raw = storage.read_text("_AI_INSTRUCTIONS.md")
    except (FileNotFoundError, PermissionError):
        return ""
    return raw


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


def get_orphans(index: VaultIndex, exclude_folders: list[str] | None = None) -> list[str]:
    exclude_folders = exclude_folders or []
    scope = ReadScope.current()
    orphans = []
    for note_path in _readable_notes(index, scope):
        parts = Path(note_path).parts
        if any(part in exclude_folders for part in parts):
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

    # Most linked = notes with most backlinks
    by_backlinks = sorted(
        all_notes,
        key=lambda p: len(scope.filter(index.get_backlinks(p))),
        reverse=True,
    )

    return {
        "total_notes": len(all_notes),
        "total_links": total_links,
        "orphans_count": len(orphans),
        "broken_links_count": len(broken),
        "most_linked": by_backlinks[:5],
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

    iso_cal = target.isocalendar()

    if period == "daily":
        note_id = target.isoformat()
        rel_path = f"Journal/{note_id}.md"
        template_name = "Daily-Note-Template.md"
    elif period == "weekly":
        note_id = f"{iso_cal[0]}-W{iso_cal[1]:02d}"
        rel_path = f"Journal/Weekly/{note_id}.md"
        template_name = "Weekly-Note-Template.md"
    elif period == "monthly":
        note_id = target.strftime("%Y-%m")
        rel_path = f"Journal/Monthly/{note_id}.md"
        template_name = "Monthly-Note-Template.md"
    elif period == "quarterly":
        q = (target.month - 1) // 3 + 1
        note_id = f"{target.year}-Q{q}"
        rel_path = f"Journal/Quarterly/{note_id}.md"
        template_name = "Quarterly-Note-Template.md"
    elif period == "yearly":
        note_id = str(target.year)
        rel_path = f"Journal/Yearly/{note_id}.md"
        template_name = "Yearly-Note-Template.md"
    else:
        raise ValueError(f"Unknown period {period!r}. Use: daily|weekly|monthly|quarterly|yearly")

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
            "content": note.content,
            "frontmatter": note.frontmatter,
            "tasks": [{"text": t.text, "done": t.done, "line": t.line} for t in note.tasks],
        }

    # Preview from template if available
    content = ""
    template_rel = f"Templates/{template_name}"
    try:
        raw_tpl = storage.read_text(template_rel)
        content = raw_tpl.replace("{{date}}", note_id).replace("{{title}}", note_id)
    except (FileNotFoundError, PermissionError):
        pass

    return {"path": rel_path, "period": period, "date": note_id, "exists": False,
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
) -> list[dict]:
    """Filter notes by tags, status, or arbitrary frontmatter fields.
    sort_by: 'path' | 'title' | 'created' | 'mtime'.
    frontmatter_filter values may be a plain value (exact match) or an
    operator dict: {"$ne": v}, {"$eq": v}, {"$in": [...]}, {"$nin": [...]},
    {"$exists": True|False}."""
    cfg = get_config()
    storage = VaultStorage.from_config(cfg)
    all_notes = _readable_notes(index)
    results: list[dict] = []

    if frontmatter_filter:
        matches_frontmatter_filter({}, frontmatter_filter)  # validate operators up front

    for note_path in all_notes:
        if folder and not note_path.startswith(folder.rstrip("/") + "/"):
            continue
        try:
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

            full = storage.resolve_read(note_path)
            results.append({
                "path": note_path,
                "title": note.frontmatter.get("title", Path(note_path).stem),
                "tags": note.tags,
                "status": note_status,
                "created": str(note.frontmatter.get("created", "")),
                "mtime": storage.stat(full.relative).st_mtime if storage.exists(full.relative) else 0.0,
                "frontmatter": note.frontmatter,
                "inline_fields": note.inline_fields,
            })
        except Exception:
            pass

    _sort_keys: dict[str, Any] = {
        "path": lambda x: x["path"],
        "title": lambda x: x["title"].lower(),
        "created": lambda x: x["created"],
        "mtime": lambda x: x["mtime"],
    }
    key_fn = _sort_keys.get(sort_by, _sort_keys["path"])
    results.sort(key=key_fn, reverse=sort_desc)
    return results[:limit]

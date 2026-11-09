"""Layout-aware helpers: conventions (file or auto-generated summary),
frontmatter-independent note metadata (created fallback) and filters."""

from __future__ import annotations

import fnmatch
import os
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath

from ..config import get_config
from ..domain.index import VaultIndex
from ..storage.filesystem import VaultStorage
from ..storage.policy import VaultPathError, matches_path_rule

# ── conventions ─────────────────────────────────────────────────────────────


def read_conventions_file() -> str:
    """Raw text of the configured conventions file, or "" if absent/unreadable."""
    cfg = get_config()
    try:
        return VaultStorage.from_config(cfg).read_text(cfg.layout.conventions_path)
    except (FileNotFoundError, PermissionError, IsADirectoryError, VaultPathError, OSError):
        return ""


def _top_folder(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "(vault root)"


def conventions_summary(index: VaultIndex) -> str:
    """Markdown summary of the vault's observable structure, for vaults
    without a conventions file. Lists only paths and counts, never note text."""
    # Imported lazily: query.py imports this module.
    from .query import _load_note, _readable_notes
    from .scoping import ReadScope

    cfg = get_config()
    layout = cfg.layout
    scope = ReadScope.current()
    notes = _readable_notes(index, scope)

    folder_counts: Counter[str] = Counter(_top_folder(p) for p in notes)
    field_counts: Counter[str] = Counter()
    with_frontmatter = 0
    for note_path in notes:
        try:
            note = _load_note(cfg.vault_path, note_path)
        except Exception:
            continue
        if note.frontmatter:
            with_frontmatter += 1
            field_counts.update(str(key) for key in note.frontmatter)

    storage = VaultStorage.from_config(cfg)
    attachments = 0
    try:
        for entry in storage.list_files(""):
            rel = entry.relative
            if rel.lower().endswith(".md"):
                continue
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            if any(matches_path_rule(rel, rule) for rule in cfg.exclude_paths):
                continue
            if scope.allows(rel):
                attachments += 1
    except (FileNotFoundError, PermissionError, OSError):
        pass
    try:
        top_dirs = sorted(
            entry.name
            for entry in storage.list_dir("")
            if entry.is_dir
            and not entry.name.startswith(".")
            and scope.allows(entry.name)
            and not any(matches_path_rule(entry.name, rule) for rule in cfg.exclude_paths)
        )
    except (FileNotFoundError, PermissionError, OSError, AttributeError):
        top_dirs = []
    for folder in top_dirs:
        folder_counts.setdefault(folder, 0)

    lines = [
        "# Vault conventions (auto-generated)",
        "",
        f"No conventions file was found at `{layout.conventions_path}`; this summary "
        "was derived from the vault itself. Create that file (or set CONVENTIONS_PATH) "
        "to give explicit rules.",
        "",
        f"## Top-level folders ({len(notes)} notes total)",
    ]
    for folder in sorted(folder_counts):
        lines.append(f"- `{folder}` — {folder_counts[folder]} notes")

    lines += ["", "## Entry note"]
    if layout.entry_note:
        exists = index.has_note(layout.entry_note) or layout.entry_note in index.get_all_notes()
        lines.append(
            f"- `{layout.entry_note}` (configured{'' if exists else ', not found'})"
        )
    else:
        ranked = sorted(
            ((len(scope.filter(index.get_backlinks(p))), p) for p in notes),
            key=lambda item: (-item[0], item[1]),
        )
        if ranked and ranked[0][0] > 0:
            count, path = ranked[0]
            lines.append(f"- not configured; most-linked note: `{path}` ({count} backlinks)")
        else:
            lines.append("- not configured")

    pct = (100.0 * with_frontmatter / len(notes)) if notes else 0.0
    lines += [
        "",
        "## Frontmatter",
        f"- {with_frontmatter} of {len(notes)} notes ({pct:.0f}%) have frontmatter",
    ]
    for name, count in field_counts.most_common(20):
        lines.append(f"- `{name}`: {count} notes")

    lines += ["", "## Attachments", f"- {attachments} non-Markdown files"]

    lines += ["", "## Layout settings"]
    lines.append(
        f"- templates: `{layout.templates_dir}/`"
        + ("" if layout.is_configured("templates_dir") else " (default, not configured)")
    )
    daily = layout.periodic_pattern("daily")
    lines.append(
        f"- daily notes: `{daily}`"
        + ("" if layout.is_configured("periodic.daily") else " (default, not configured)")
    )
    excludes = ", ".join(f"`{f}`" for f in layout.orphan_excludes) or "none"
    lines.append(f"- orphan excludes: {excludes}")
    return "\n".join(lines) + "\n"


def vault_conventions(index: VaultIndex) -> dict:
    """{conventions, source: "file"|"auto-generated", path}."""
    cfg = get_config()
    text = read_conventions_file()
    if text.strip():
        return {"conventions": text, "source": "file", "path": cfg.layout.conventions_path}
    return {
        "conventions": conventions_summary(index),
        "source": "auto-generated",
        "path": cfg.layout.conventions_path,
    }


# ── created fallback ────────────────────────────────────────────────────────


def created_info(frontmatter: dict, stat_result: os.stat_result | None) -> tuple[str, str]:
    """(created, created_source). Uses frontmatter ``created`` when present,
    else the file's birth time (where the OS reports one), else its mtime."""
    value = frontmatter.get("created")
    if value not in (None, ""):
        return str(value), "frontmatter"
    if stat_result is None:
        return "", "none"
    birth = getattr(stat_result, "st_birthtime", None)
    if birth:
        return datetime.fromtimestamp(birth).isoformat(timespec="seconds"), "birthtime"
    return datetime.fromtimestamp(stat_result.st_mtime).isoformat(timespec="seconds"), "mtime"


# ── frontmatter-independent filters ─────────────────────────────────────────


def fold_text(value: str) -> str:
    """Accent- and case-insensitive key: strip combining marks, casefold."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _parse_bound(value: str, *, name: str, upper: bool) -> float:
    """ISO date/datetime → timestamp. A date-only upper bound covers the whole day."""
    text = value.strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            start = datetime(day.year, day.month, day.day)
            return (start + timedelta(days=1)).timestamp() if upper else start.timestamp()
        return datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD) or datetime") from exc


_WORD_RE = re.compile(r"\w+", re.UNICODE)


class NoteFilters:
    """Filters that work on any note, with or without frontmatter."""

    def __init__(
        self,
        title_contains: str | None = None,
        path_glob: str | None = None,
        mtime_after: str | None = None,
        mtime_before: str | None = None,
        min_words: int | None = None,
        has_tasks: bool | None = None,
    ) -> None:
        self.title_key = fold_text(title_contains) if title_contains else None
        self.path_glob = (
            unicodedata.normalize("NFC", path_glob).casefold() if path_glob else None
        )
        self.after = _parse_bound(mtime_after, name="mtime_after", upper=False) if mtime_after else None
        self.before = _parse_bound(mtime_before, name="mtime_before", upper=True) if mtime_before else None
        if min_words is not None and min_words < 0:
            raise ValueError("min_words must be >= 0")
        self.min_words = min_words
        self.has_tasks = has_tasks

    @property
    def active(self) -> bool:
        return any(
            value is not None
            for value in (self.title_key, self.path_glob, self.after, self.before,
                          self.min_words, self.has_tasks)
        )

    def matches_path(self, path: str) -> bool:
        if self.path_glob is None:
            return True
        return fnmatch.fnmatchcase(unicodedata.normalize("NFC", path).casefold(), self.path_glob)

    def matches_mtime(self, mtime: float) -> bool:
        if self.after is not None and mtime < self.after:
            return False
        return not (self.before is not None and mtime >= self.before)

    def matches_note(self, path: str, title: str, note) -> bool:
        if self.title_key is not None:
            stem = PurePosixPath(path).stem
            if self.title_key not in fold_text(str(title)) and self.title_key not in fold_text(stem):
                return False
        if self.min_words is not None and len(_WORD_RE.findall(note.content)) < self.min_words:
            return False
        return self.has_tasks is None or bool(note.tasks) == self.has_tasks

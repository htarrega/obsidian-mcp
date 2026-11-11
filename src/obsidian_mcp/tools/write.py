from __future__ import annotations

import difflib
import logging
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import get_config
from ..domain.index import VaultIndex
from ..domain.links import LinkResolver
from ..domain.markdown_links import MarkdownLink, iter_markdown_links, iter_reference_destinations
from ..domain.markdown_scan import frontmatter_end, mask_non_content
from ..domain.models import (
    PreconditionRequiredError,
    RevisionConflictError,
    normalize_revision_token,
)
from ..domain.parser import extract_inline_tags, extract_task_markers
from ..envelope import batch_error, batch_item, batch_summary
from ..storage.filesystem import VaultStorage
from ..storage.locking import acquire_lock
from ..storage.policy import ReadPermissionError, WritePermissionError
from ..storage.revisions import read_text_for_update, revision_result
from .read import _is_excluded
from .safe_regex import compile_user_regex

logger = logging.getLogger(__name__)


def _storage() -> VaultStorage:
    return VaultStorage.from_config()


def _check_write_permission(relative_path: str) -> None:
    _storage().resolve_write(relative_path)


def _require_note_path(path: str) -> str:
    if not path.lower().endswith(".md"):
        raise ValueError("Note paths must end in .md")
    return path


def _unified_diff(before: str, after: str, path: str) -> str:
    """Unified diff between two full note texts, git-diff-style — lets a
    caller see exactly what a write/patch changed without re-reading the
    note before and after."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _replacement(find: str, replace: str, mode: str):
    """Compile ``find`` and return ``(pattern, repl)`` for ``pattern.subn``.

    In 'exact' mode the replacement is literal: a plain string would be read
    as a regex template, so a backslash in it (``C:\\path``) would fail or be
    rewritten. In 'regex' mode ``\\1``/``\\g<name>`` keep working.
    """
    if mode == "regex":
        return compile_user_regex(find), replace
    if mode == "exact":
        return re.compile(re.escape(find)), lambda _match: replace
    raise ValueError(f"Unknown mode: {mode!r} (use 'exact' or 'regex')")


@dataclass
class _PlannedWrite:
    path: str
    before: str | None  # None: the note does not exist yet
    after: str
    revision: str | None  # token read under the lock; None for a new note


def _commit_writes(storage: VaultStorage, planned: list[_PlannedWrite]) -> dict:
    """Write every planned note or none of them.

    Callers hold the locks and pass revisions read under them, so another
    writer (Sync, Obsidian) is detected as a revision conflict. If a write
    fails, the notes already written get their previous text back and notes
    this call created are moved to .trash (deleted outright only when
    permanent deletion is allowed), then the error is re-raised.
    Returns {path: FileRevision}.
    """
    committed: list[tuple[_PlannedWrite, object]] = []
    try:
        for write in planned:
            revision = storage.write_text_atomic(
                write.path,
                write.after,
                expected_revision=write.revision,
                create_only=write.before is None,
            )
            committed.append((write, revision))
    except BaseException:
        _rollback_writes(storage, committed)
        raise
    return {write.path: revision for write, revision in committed}


def _rollback_writes(storage: VaultStorage, committed: list) -> None:
    for write, revision in reversed(committed):
        try:
            if write.before is None:
                if storage.policy.allow_permanent_delete:
                    storage.delete(write.path)
                else:
                    storage.trash(write.path)
            else:
                storage.write_text_atomic(
                    write.path, write.before, expected_revision=revision.token
                )
        except Exception:  # keep undoing the rest; the original error wins
            logger.exception("Rollback failed for %s", write.path)


def write_note(
    path: str,
    content: str,
    index: VaultIndex | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
    create_only: bool = False,
) -> dict:
    """Write (create or overwrite) a note.

    If `content` has no YAML frontmatter of its own and a note already exists
    at `path` with frontmatter, that existing frontmatter is carried over
    onto the new body instead of being silently dropped — an overwrite that
    only changes the body shouldn't destroy status/tags/created/etc.

    dry_run=True previews the result (including the merged frontmatter and a
    unified diff against the current file) without writing anything —
    check the preview before setting dry_run=False.
    """
    _require_note_path(path)
    storage = _storage()
    target = storage.resolve_write(path)
    path = target.relative
    if not storage.policy.can_read(path):
        raise ReadPermissionError(
            "write_note requires read access to determine whether the target exists, "
            "preserve frontmatter, and calculate its diff"
        )

    lock = acquire_lock(path, lock_path=get_config().lock_path)
    try:
        exists = storage.exists(path, read=True)
        if exists:
            before, current_revision = read_text_for_update(storage, path, expected_revision)
        else:
            if expected_revision is not None:
                raise RevisionConflictError(path, expected_revision, None)
            before, current_revision = "", None

        frontmatter_preserved = False
        if not _FM_RE.match(content) and before:
            existing_fm, _ = _parse_frontmatter(before)
            if existing_fm:
                content = _serialize_frontmatter(existing_fm, content)
                frontmatter_preserved = True

        diff = _unified_diff(before, content, path)

        if dry_run:
            return {
                "path": path,
                "status": "dry_run",
                "frontmatter_preserved": frontmatter_preserved,
                "preview": content,
                "diff": diff,
                "revision": current_revision,
            }

        if (
            exists
            and not create_only
            and get_config().require_write_preconditions
            and expected_revision is None
        ):
            raise PreconditionRequiredError(path)
        revision = storage.write_text_atomic(
            path,
            content,
            expected_revision=current_revision,
            create_only=create_only or not exists,
        )
    finally:
        lock.release()

    if index is not None:
        index.update(path)

    return revision_result({
        "path": path,
        "status": "written",
        "frontmatter_preserved": frontmatter_preserved,
        "diff": diff,
    }, revision)


def patch_note(
    path: str,
    section: str,
    new_content: str,
    mode: str = "replace",
    target_type: str = "heading",
    index: VaultIndex | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Edit a note section or block reference.

    mode: 'replace' | 'insert_before' | 'insert_after' | 'append'
    target_type: 'heading' | 'block_ref'
    dry_run: render the patch and return {preview, diff} without writing.
    """
    _require_note_path(path)
    target = _storage().resolve_write(path)
    path = target.relative

    lock = acquire_lock(path, lock_path=get_config().lock_path)
    try:
        storage = _storage()
        raw, current_revision = read_text_for_update(storage, path, expected_revision)
        if target_type == "block_ref":
            patched = _patch_block_ref(raw, section, new_content, mode)
        else:
            patched = _patch_section(raw, section, new_content, mode)
        diff = _unified_diff(raw, patched, path)

        if dry_run:
            return {
                "path": path,
                "status": "dry_run",
                "mode": mode,
                "target_type": target_type,
                "preview": patched,
                "diff": diff,
                "revision": current_revision,
            }

        revision = storage.write_text_atomic(path, patched, expected_revision=current_revision)
    finally:
        lock.release()

    if index is not None:
        index.update(path)

    return revision_result(
        {
            "path": path,
            "status": "patched",
            "mode": mode,
            "target_type": target_type,
            "diff": diff,
        },
        revision,
    )


def _find_section_bounds(content: str, heading: str) -> tuple[int, int, int, int]:
    """Return (heading_start, heading_end, body_end, level) for a heading.

    ``heading`` is the heading text; a leading ``## `` is tolerated.
    """
    escaped = re.escape(re.sub(r"^#{1,6}\s+", "", heading.strip()))
    m = re.search(r"^(#{1,6})\s+" + escaped + r"[ \t]*$", content, re.MULTILINE)
    if not m:
        raise ValueError(f"Section {heading!r} not found in note")
    level = len(m.group(1))
    end_m = re.search(r"^#{1," + str(level) + r"}\s", content[m.end():], re.MULTILINE)
    body_end = m.end() + end_m.start() if end_m else len(content)
    return m.start(), m.end(), body_end, level


def _patch_section(content: str, heading: str, new_content: str, mode: str) -> str:
    h_start, h_end, body_end, _ = _find_section_bounds(content, heading)
    heading_line = content[h_start:h_end].rstrip("\n")

    if mode == "replace":
        return content[:h_start] + heading_line + "\n\n" + new_content.strip() + "\n" + content[body_end:]
    if mode == "insert_before":
        return content[:h_start] + new_content.strip() + "\n\n" + content[h_start:]
    if mode == "insert_after":
        return content[:h_end] + "\n" + new_content.strip() + "\n\n" + content[h_end:]
    if mode == "append":
        before = content[:body_end].rstrip("\n")
        return before + "\n\n" + new_content.strip() + "\n" + content[body_end:]
    raise ValueError(f"Unknown mode: {mode!r}")


def _patch_block_ref(content: str, block_id: str, new_content: str, mode: str) -> str:
    pattern = re.compile(rf"^(.+?)\s+\^{re.escape(block_id)}\s*$", re.MULTILINE)
    m = pattern.search(content)
    if not m:
        raise ValueError(f"Block reference ^{block_id} not found in note")

    if mode == "replace":
        return content[:m.start()] + new_content.strip() + " ^" + block_id + content[m.end():]
    if mode == "insert_before":
        return content[:m.start()] + new_content.strip() + "\n" + content[m.start():]
    if mode in ("insert_after", "append"):
        return content[:m.end()] + "\n" + new_content.strip() + content[m.end():]
    raise ValueError(f"Unknown mode: {mode!r}")


def delete_note(
    path: str,
    trash: bool = True,
    index: VaultIndex | None = None,
    expected_revision: str | None = None,
) -> dict:
    """Delete a note. trash=True moves it to .trash/ in the vault root."""
    cfg = get_config()
    _require_note_path(path)
    target = _storage().resolve_delete(path, permanent=not trash)
    path = target.relative
    lock = acquire_lock(path, lock_path=cfg.lock_path)
    try:
        storage = _storage()
        current = storage.revision(path, read=False)
        if expected_revision is not None and current.token != normalize_revision_token(expected_revision):
            raise RevisionConflictError(path, expected_revision, current)
        if cfg.require_write_preconditions and expected_revision is None:
            raise PreconditionRequiredError(path)
        if trash:
            storage.trash(path)
        else:
            storage.delete(path)
    finally:
        lock.release()

    if index is not None:
        index.remove(path)

    return {"path": path, "status": "deleted", "trash": trash}


def restore_note(trashed_name: str, to_path: str, index: VaultIndex | None = None) -> dict:
    """Restore a note previously moved to .trash/ (via delete_note trash=True).

    trashed_name: the filename as it sits under .trash/ (see list_trash_tool) —
    a collision at delete time may have appended a random suffix, so this is
    often not the note's original filename.
    to_path: where to put it back (you choose it; the original folder isn't
    recoverable from the trash entry alone).
    """
    if "/" in trashed_name or "\\" in trashed_name or trashed_name in (".", ".."):
        raise ValueError(f"trashed_name must be a bare filename, not a path: {trashed_name!r}")

    cfg = get_config()
    if not to_path.lower().endswith(".md"):
        raise ValueError("Note paths must end in .md")
    storage = _storage()
    storage.resolve_write(to_path)
    info = storage.trash_info(trashed_name)
    if info.is_dir:
        raise ValueError("The trashed item is a folder, not a note")

    lock = acquire_lock(f".trash/{trashed_name}", lock_path=cfg.lock_path)
    try:
        destination = storage.restore(trashed_name, to_path)
        to_path = destination.relative
    finally:
        lock.release()

    if index is not None:
        index.update(to_path)

    revision = storage.revision(to_path, read=False)
    return revision_result(
        {"from": f".trash/{trashed_name}", "to": to_path, "status": "restored"},
        revision,
    )


def append_to_note(
    path: str,
    content: str,
    section: str | None = None,
    create: bool = True,
    index: VaultIndex | None = None,
    expected_revision: str | None = None,
) -> dict:
    """Append content to a note (or a specific section). Creates the note if it doesn't exist."""
    _require_note_path(path)
    target = _storage().resolve_write(path)
    path = target.relative
    lock = acquire_lock(path, lock_path=get_config().lock_path)
    try:
        storage = _storage()
        if storage.exists(path, read=True):
            raw, current_revision = read_text_for_update(storage, path, expected_revision)
            if section:
                patched = _patch_section(raw, section, content, mode="append")
            else:
                patched = raw.rstrip("\n") + "\n\n" + content.strip() + "\n"
        elif create:
            if expected_revision is not None:
                raise RevisionConflictError(path, expected_revision, None)
            patched = content
            current_revision = None
        else:
            raise FileNotFoundError(f"Note not found: {path!r}")
        revision = storage.write_text_atomic(
            path,
            patched,
            expected_revision=current_revision,
            create_only=current_revision is None,
        )
    finally:
        lock.release()

    if index is not None:
        index.update(path)

    return revision_result({"path": path, "status": "appended"}, revision)


_H1_LINE_RE = re.compile(r"# [^\n]*(?:\n|\Z)")


def _prepend_offset(raw: str) -> int:
    """Where prepended content goes: after the frontmatter, after any blank
    lines that follow it, and after a leading level-1 heading (a note title)
    if the body starts with one."""
    offset = frontmatter_end(raw)
    while raw.startswith("\n", offset) or raw.startswith("\r\n", offset):
        offset += 1 if raw[offset] == "\n" else 2
    h1 = _H1_LINE_RE.match(raw, offset)
    if h1:
        offset = h1.end()
    return offset


def _prepend_text(raw: str, content: str) -> str:
    block = content if content.endswith("\n") else content + "\n"
    offset = _prepend_offset(raw)
    if offset and not raw[:offset].endswith("\n"):
        block = "\n" + block  # an H1 on the last line without a newline
    return raw[:offset] + block + raw[offset:]


def prepend_to_note(
    path: str,
    content: str,
    create: bool = True,
    index: VaultIndex | None = None,
    expected_revision: str | None = None,
) -> dict:
    """Insert content at the top of a note's body (see _prepend_offset): the
    typical use is a new dated section on a newest-on-top log. ``content`` is
    inserted verbatim plus a trailing newline if it lacks one; nothing else in
    the note changes. Creates the note if it is missing and create=True."""
    _require_note_path(path)
    target = _storage().resolve_write(path)
    path = target.relative
    if not content.strip():
        raise ValueError("content must not be empty")
    block = content if content.endswith("\n") else content + "\n"
    lock = acquire_lock(path, lock_path=get_config().lock_path)
    try:
        storage = _storage()
        if storage.exists(path, read=True):
            raw, current_revision = read_text_for_update(storage, path, expected_revision)
            patched = _prepend_text(raw, content)
        elif create:
            if expected_revision is not None:
                raise RevisionConflictError(path, expected_revision, None)
            patched = block
            current_revision = None
        else:
            raise FileNotFoundError(f"Note not found: {path!r}")
        revision = storage.write_text_atomic(
            path,
            patched,
            expected_revision=current_revision,
            create_only=current_revision is None,
        )
    finally:
        lock.release()

    if index is not None:
        index.update(path)

    return revision_result({"path": path, "status": "prepended"}, revision)


# A task line, split so the checkbox state can be swapped in place.
_TASK_LINE_RE = re.compile(
    r"^(?P<head>[ \t>]*(?:[-*+]|\d+[.)])[ \t]+\[)(?P<state>[ xX])(?P<tail>\][ \t]+)(?P<text>\S.*?)[ \t]*$"
)


def _task_text_matches(expected: str, raw_text: str, line: str) -> bool:
    """Whitespace- and case-insensitive match of ``expected`` against the task
    text as get_tasks_tool reports it (Tasks-plugin markers stripped), the raw
    text after the checkbox, or the whole line."""
    def norm(value: str) -> str:
        return " ".join(value.split()).casefold()

    wanted = norm(expected)
    if not wanted:
        return False
    clean = extract_task_markers(raw_text)[0]
    return wanted in (norm(clean), norm(raw_text), norm(line))


def set_task_status(
    path: str,
    line: int,
    done: bool,
    expected_text: str,
    index: VaultIndex | None = None,
    expected_revision: str | None = None,
) -> dict:
    """Check or uncheck the task on 1-based file line ``line``.

    Refuses (ValueError) if that line is not a checkbox task outside code, or
    if its text does not match ``expected_text`` — protection against acting
    on a stale line number after the note changed. Only the checkbox character
    changes; an already-matching state is reported as unchanged and nothing
    is written."""
    _require_note_path(path)
    target = _storage().resolve_write(path)
    path = target.relative
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise ValueError("line must be a positive integer (1-based file line)")
    lock = acquire_lock(path, lock_path=get_config().lock_path)
    try:
        storage = _storage()
        raw, current_revision = read_text_for_update(storage, path, expected_revision)
        lines = raw.split("\n")
        if line > len(lines):
            raise ValueError(f"{path!r} has only {len(lines)} lines; line {line} does not exist")
        original = lines[line - 1]
        body = original[:-1] if original.endswith("\r") else original
        masked = mask_non_content(raw).split("\n")[line - 1]
        m = _TASK_LINE_RE.match(body)
        if m is None or _TASK_LINE_RE.match(masked.rstrip("\r")) is None:
            raise ValueError(f"Line {line} of {path!r} is not a task: {body.strip()[:120]!r}")
        if not _task_text_matches(expected_text, m.group("text"), body):
            raise ValueError(
                f"Line {line} of {path!r} does not match expected_text "
                f"(found {m.group('text')[:120]!r}); re-read the note and retry"
            )
        was_done = m.group("state").lower() == "x"
        if was_done == done:
            return {
                "path": path,
                "status": "unchanged",
                "line": line,
                "text": body,
                "done": done,
                "revision": current_revision,
            }
        new_line = m.group("head") + ("x" if done else " ") + body[m.end("state"):]
        lines[line - 1] = new_line + ("\r" if original.endswith("\r") else "")
        revision = storage.write_text_atomic(
            path, "\n".join(lines), expected_revision=current_revision
        )
    finally:
        lock.release()

    if index is not None:
        index.update(path)

    return revision_result(
        {"path": path, "status": "task_updated", "line": line, "text": new_line, "done": done},
        revision,
    )


def patch_frontmatter(
    path: str,
    updates: dict,
    merge_arrays: bool = True,
    index: VaultIndex | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Update YAML frontmatter, optionally returning a write-free preview."""
    _require_note_path(path)
    target = _storage().resolve_write(path)
    path = target.relative

    if not _storage().exists(path, read=True):
        raise FileNotFoundError(f"Note not found: {path!r}")

    lock = acquire_lock(path, lock_path=get_config().lock_path)
    try:
        storage = _storage()
        raw, current_revision = read_text_for_update(storage, path, expected_revision)
        patched = _apply_frontmatter_updates(raw, updates, merge_arrays)
        diff = _unified_diff(raw, patched, path)

        if dry_run:
            return {
                "path": path,
                "status": "dry_run",
                "updated_keys": list(updates.keys()),
                "preview": patched,
                "diff": diff,
                "revision": current_revision,
            }

        revision = storage.write_text_atomic(path, patched, expected_revision=current_revision)
    finally:
        lock.release()

    if index is not None:
        index.update(path)

    return revision_result({
        "path": path,
        "status": "frontmatter_patched",
        "updated_keys": list(updates.keys()),
        "diff": diff,
    }, revision)


def patch_frontmatter_batch(
    updates: list[dict],
    index: VaultIndex | None = None,
    dry_run: bool = False,
) -> dict:
    """Patch frontmatter on multiple notes in one pass instead of one
    patch_frontmatter call per note.

    updates: list of {"path": str, "updates": dict, "merge_arrays": bool,
    "expected_revision": str} (merge_arrays defaults to True per entry, same
    as patch_frontmatter). One entry failing (missing note, bad path, stale
    expected_revision, ...) doesn't abort the rest — every entry produces one
    envelope-shaped item, carrying either its committed `revision` or an
    `error` with the raised exception's class name.

    Returns {"results": [...], "summary": {total, succeeded, failed}}.
    """
    results: list[dict] = []
    for entry in updates:
        path = entry.get("path")
        try:
            result = patch_frontmatter(
                path,
                entry.get("updates", {}),
                merge_arrays=entry.get("merge_arrays", True),
                index=index,
                dry_run=dry_run,
                expected_revision=entry.get("expected_revision"),
            )
            data = {
                "updated_keys": result.get("updated_keys", []),
                "diff": result.get("diff", ""),
            }
            if dry_run:
                data["preview"] = result.get("preview", "")
            results.append(
                batch_item(result["path"], revision=result.get("revision"), data=data)
            )
        except Exception as exc:
            results.append(batch_error(path, exc))

    return {"results": results, "summary": batch_summary(results)}


_FM_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_INLINE_TAG_RE = re.compile(r"(?<!\S)#([\w/]+)")


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Parse YAML frontmatter block. Returns (fm_dict, body_text)."""
    m = _FM_RE.match(raw)
    if m:
        try:
            fm: dict = yaml.safe_load(m.group(1)) or {}
        except Exception:
            fm = {}
        return fm, raw[m.end():]
    return {}, raw


def _serialize_frontmatter(fm: dict, body: str) -> str:
    """Serialize frontmatter dict and body back to a note string."""
    new_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False).strip()
    return f"---\n{new_fm}\n---\n{body}"


def manage_tags(
    path: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    index: VaultIndex | None = None,
    expected_revision: str | None = None,
) -> dict:
    """Add or remove tags on a note, honouring TAGS_SOURCE.

    'both' (default) and 'frontmatter': added tags go to the frontmatter
    ``tags`` array. 'inline': added tags are appended to the body as #tags.
    Removal always edits the frontmatter array; inline #tags (outside code
    and comments) are stripped unless TAGS_SOURCE is 'frontmatter'."""
    _require_note_path(path)
    target = _storage().resolve_write(path)
    path = target.relative

    if not _storage().exists(path, read=True):
        raise FileNotFoundError(f"Note not found: {path!r}")

    add = list(add or [])
    remove = list(remove or [])

    lock = acquire_lock(path, lock_path=get_config().lock_path)
    try:
        storage = _storage()
        raw, current_revision = read_text_for_update(storage, path, expected_revision)
        patched = _apply_tag_changes(raw, add, remove, source=get_config().tags_source)
        revision = storage.write_text_atomic(path, patched, expected_revision=current_revision)
    finally:
        lock.release()

    if index is not None:
        index.update(path)

    return revision_result(
        {"path": path, "status": "tags_updated", "added": add, "removed": remove}, revision
    )


def _apply_tag_changes(
    raw: str, add: list[str], remove: list[str], source: str = "both"
) -> str:
    fm, body = _parse_frontmatter(raw)

    current: list = fm.get("tags", [])
    if not isinstance(current, list):
        current = [current] if current else []

    if source == "inline":
        existing = set(extract_inline_tags(body))
        new_inline = [tag.lstrip("#") for tag in add if tag.lstrip("#") not in existing]
        if new_inline:
            body = body.rstrip("\n") + "\n\n" + " ".join(f"#{tag}" for tag in new_inline) + "\n"
    else:
        for tag in add:
            if tag not in current:
                current.append(tag)
    for tag in remove:
        current = [t for t in current if t != tag]
    if current or "tags" in fm:
        fm["tags"] = current

    if source != "frontmatter" and remove:
        body = _strip_inline_tags(body, remove)

    return _serialize_frontmatter(fm, body)


def _strip_inline_tags(body: str, tags: list[str]) -> str:
    """Remove ``#tag`` occurrences in prose (code and %% comments untouched)."""
    masked = mask_non_content(body)
    for tag in tags:
        pattern = re.compile(r"(?<!\S)#" + re.escape(tag.lstrip("#")) + r"(?=[\s,.]|$)")
        for m in reversed(list(pattern.finditer(masked))):
            body = body[: m.start()] + body[m.end():]
            masked = masked[: m.start()] + masked[m.end():]
    # Collapse multiple spaces created by removal (prose lines only)
    lines = body.split("\n")
    masked_lines = masked.split("\n")
    return "\n".join(
        re.sub(r"(?<=\S)[ \t]{2,}", " ", line) if line == masked_line else line
        for line, masked_line in zip(lines, masked_lines, strict=True)
    )


def _apply_frontmatter_updates(raw: str, updates: dict, merge_arrays: bool) -> str:
    fm, body = _parse_frontmatter(raw)

    for key, value in updates.items():
        if merge_arrays and isinstance(value, list) and isinstance(fm.get(key), list):
            existing: list = fm[key]
            fm[key] = existing + [v for v in value if v not in existing]
        else:
            fm[key] = value

    return _serialize_frontmatter(fm, body)


def _link_pattern(from_path: str) -> re.Pattern:
    """Wikilinks/embeds to a note: [[stem]], [[stem.md]], [[Folder/stem]],
    [[Folder/stem.md]], each optionally with #heading, ^block or |alias
    (also the table-escaped \\|alias)."""
    path = from_path.replace("\\", "/")
    stem = Path(path).stem
    names = {stem, stem + ".md", path, path[:-3]}
    targets = sorted((re.escape(name) for name in names), key=len, reverse=True)
    return re.compile(
        rf"\[\[({'|'.join(targets)})((?:\\?[|#][^\]]*)?)\]\]", re.IGNORECASE
    )


def _spell_like(target: str, old_path: str, source_after: str) -> str:
    """Path to ``target`` written the way ``old_path`` was: relative stays
    relative, a bare name stays a bare name, ``.md`` only if it had one;
    anything else becomes the full vault path."""
    if target.lower().endswith(".md") and not old_path.lower().endswith(".md"):
        target = target[:-3]
    if old_path.startswith(("./", "../")):
        relative = posixpath.relpath(target, posixpath.dirname(source_after) or ".")
        if old_path.startswith("./") and not relative.startswith("../"):
            relative = "./" + relative
        return relative
    if "/" not in old_path.strip("/"):
        return target.rsplit("/", 1)[-1]
    return ("/" if old_path.startswith("/") else "") + target


def _encode_like(path: str, old: MarkdownLink) -> str:
    """Destination for ``path`` in the old link's style (<> or %-encoding)."""
    if old.angle:
        return f"<{path}{old.subpath}>"
    encoded = path.replace("%", "%25").replace(" ", "%20")
    if re.search(r"%2[89]", old.raw, re.IGNORECASE):
        encoded = encoded.replace("(", "%28").replace(")", "%29")
    return encoded + old.subpath


@dataclass
class _LinkRewrite:
    text: str
    wikilinks: int
    markdown: int


def _rewrite_links(
    raw: str,
    *,
    source_before: str,
    source_after: str,
    from_path: str,
    to_path: str,
    wiki_re: re.Pattern,
    resolver: LinkResolver,
) -> _LinkRewrite:
    """Point every link to ``from_path`` in one note at ``to_path``.

    Wikilinks become [[new name...]]; Markdown links keep their style. In the
    moved note itself (source_before == from_path), relative Markdown links
    to other files are re-relativised from the new folder. Code, comments
    (and, for Markdown links, frontmatter) are left alone.
    """
    to_stem = Path(to_path).stem
    moved_self = source_before == from_path
    changed_folder = posixpath.dirname(source_before) != posixpath.dirname(source_after)
    edits: list[tuple[int, int, str]] = []
    wikilinks = markdown = 0
    masked = mask_non_content(raw)
    for match in wiki_re.finditer(masked):
        new = f"[[{to_stem}{raw[match.start(2):match.end(2)]}]]"
        if new != raw[match.start():match.end()]:
            edits.append((match.start(), match.end(), new))
            wikilinks += 1
    for link in iter_markdown_links(raw):
        target = resolver.resolve(link.path, source=source_before)
        if target == from_path:
            target = to_path
        elif not (moved_self and changed_folder and target and link.path.startswith(("./", "../"))):
            continue
        new = _encode_like(_spell_like(target, link.path, source_after), link)
        if new != raw[link.start:link.end]:
            edits.append((link.start, link.end, new))
            markdown += 1
    text = raw
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return _LinkRewrite(text, wikilinks, markdown)


def _same_name_notes(storage: VaultStorage, stem: str, *, exclude: str = "") -> list[str]:
    """Other notes whose file name is ``stem`` (case-insensitive)."""
    wanted = stem.casefold()
    return sorted(
        candidate.relative
        for candidate in storage.list_files()
        if candidate.relative.lower().endswith(".md")
        and candidate.relative != exclude
        and Path(candidate.relative).stem.casefold() == wanted
    )


def move_note(
    from_path: str,
    to_path: str,
    index: VaultIndex | None = None,
    dry_run: bool = False,
) -> dict:
    """Move or rename a note and rewrite the links that point to it: wikilinks
    and Markdown links/embeds (style kept: %20 or <>, relative or full path,
    .md or not). The moved note's own relative Markdown links are fixed for
    its new folder. "[ref]: path" definitions are only reported.

    All or nothing: the note is moved first, then the linking notes (and its
    own self-links) are rewritten; if any of that fails, the rewrites are
    undone and the note is moved back. dry_run=True lists the notes whose
    links would change.

    Refused when another note already uses the new name (every [[name]] link
    in the vault would become ambiguous) or shares the old one (a bare
    [[old]] link could mean either note, so rewriting it would hijack it).
    """
    cfg = get_config()
    _require_note_path(from_path)
    _require_note_path(to_path)
    storage = _storage()
    from_path = storage.resolve_delete(from_path).relative
    to_path = storage.resolve_write(to_path).relative
    if from_path == to_path:
        raise ValueError("Source and destination are the same note")
    if not storage.exists(from_path, read=False):
        raise FileNotFoundError(f"Source note not found: {from_path!r}")
    if storage.exists(to_path, read=False):
        raise FileExistsError(f"Target already exists: {to_path!r}")

    from_stem, to_stem = Path(from_path).stem, Path(to_path).stem
    taken = _same_name_notes(storage, to_stem, exclude=from_path)
    if taken:
        raise FileExistsError(
            f"Another note is already named {to_stem!r}: {', '.join(taken)}. "
            "Pick a different name; note names must be unique for [[links]]."
        )
    twins = _same_name_notes(storage, from_stem, exclude=from_path)
    if twins:
        raise ValueError(
            f"{from_stem!r} is also the name of {', '.join(twins)}, so [[{from_stem}]] "
            "links are ambiguous and can't be rewritten safely. Rename the other note first."
        )

    files = [
        candidate.relative
        for candidate in storage.list_files()
        if not candidate.relative.startswith(".trash/")
    ]
    resolver = LinkResolver(files)
    wiki_re = _link_pattern(from_path)

    def rewrite(raw: str, rel: str) -> _LinkRewrite:
        after = to_path if rel == from_path else rel
        return _rewrite_links(
            raw,
            source_before=rel,
            source_after=after,
            from_path=from_path,
            to_path=to_path,
            wiki_re=wiki_re,
            resolver=resolver,
        )

    # Every note whose links change; the rewrite is a second mutation, so
    # each one must be writable before anything moves.
    linking: list[str] = []
    counts: list[dict] = []
    reference_links: list[str] = []
    for rel in files:
        if not rel.lower().endswith(".md"):
            continue
        try:
            raw = storage.read_text(rel)
        except Exception:
            continue
        result = rewrite(raw, rel)
        if result.text != raw:
            counts.append({
                "path": rel,
                "count": result.wikilinks + result.markdown,
                "wikilinks": result.wikilinks,
                "markdown": result.markdown,
            })
            if rel != from_path:  # the moved note is rewritten at its new path
                _check_write_permission(rel)
                linking.append(rel)
        if any(
            resolver.resolve(dest, source=rel) == from_path
            for dest in iter_reference_destinations(raw)
        ):
            reference_links.append(rel)

    if dry_run:
        return {
            "from": from_path,
            "to": to_path,
            "status": "dry_run",
            "links": counts,
            "updated_links_in": linking,
            "reference_links_not_updated": reference_links,
        }

    self_links = any(item["path"] == from_path for item in counts)
    locks = []
    try:
        for rel in sorted({*linking, from_path, to_path}):
            locks.append(acquire_lock(rel, lock_path=cfg.lock_path))
        planned = []
        for rel in linking:
            raw, revision = storage.read_text_with_revision(rel)
            after = rewrite(raw, rel).text
            if after != raw:
                planned.append(_PlannedWrite(rel, raw, after, revision.token))

        storage.move(from_path, to_path)
        try:
            if self_links:
                raw, revision = storage.read_text_with_revision(to_path)
                after = rewrite(raw, from_path).text
                if after != raw:
                    planned.append(_PlannedWrite(to_path, raw, after, revision.token))
            _commit_writes(storage, planned)
        except BaseException:
            try:
                storage.move(to_path, from_path)
            except Exception:
                logger.exception("Could not move %s back to %s", to_path, from_path)
            raise
    finally:
        for lock in reversed(locks):
            lock.release()

    if index is not None:
        index.remove(from_path)
        index.update(to_path)
        for write in planned:
            index.update(write.path)

    return {
        "from": from_path,
        "to": to_path,
        "status": "moved",
        "updated_links_in": [write.path for write in planned if write.path != to_path],
        "reference_links_not_updated": reference_links,
    }


def copy_note(
    from_path: str,
    to_path: str,
    index: VaultIndex | None = None,
    dry_run: bool = False,
) -> dict:
    """Copy a note to a new path on the server, byte for byte.

    Never overwrites. Links are not touched. data.name_conflicts lists other
    notes with the same file name, which makes [[name]] links ambiguous.
    """
    _require_note_path(from_path)
    _require_note_path(to_path)
    storage = _storage()
    from_path = storage.resolve_read(from_path).relative
    to_path = storage.resolve_write(to_path).relative
    if from_path == to_path:
        raise ValueError("Source and destination are the same note")
    if not storage.exists(from_path, read=True):
        raise FileNotFoundError(f"Source note not found: {from_path!r}")
    if storage.exists(to_path, read=False):
        raise FileExistsError(f"Target already exists: {to_path!r}")

    name_conflicts = _same_name_notes(storage, Path(to_path).stem)
    content = storage.read_bytes(from_path)

    if dry_run:
        return {
            "from": from_path,
            "path": to_path,
            "status": "dry_run",
            "bytes": len(content),
            "name_conflicts": name_conflicts,
        }

    lock = acquire_lock(to_path, lock_path=get_config().lock_path)
    try:
        revision = storage.write_bytes_atomic(to_path, content, create_only=True)
    finally:
        lock.release()

    if index is not None:
        index.update(to_path)

    return revision_result(
        {"from": from_path, "path": to_path, "status": "copied", "name_conflicts": name_conflicts},
        revision,
    )


BATCH_MAX_EDITS = 50
DIFF_MAX_CHARS = 4000  # per note in batch results; keeps responses small


def _capped_diff(diff: str) -> dict:
    if len(diff) <= DIFF_MAX_CHARS:
        return {"diff": diff}
    return {"diff": diff[:DIFF_MAX_CHARS], "diff_truncated": True}
_BATCH_OPS = ("replace", "section", "append", "prepend", "write")


def _field(edit: dict, name: str, *, required: bool = True, empty_ok: bool = False) -> str | None:
    value = edit.get(name)
    if value is None:
        if required:
            raise ValueError(f"missing {name!r}")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name!r} must be a string")
    if not value and not empty_ok:
        raise ValueError(f"{name!r} must not be empty")
    return value


def _apply_edit(text: str | None, op: str, edit: dict) -> str:
    """Apply one batch edit to a note's text (None: the note does not exist)."""
    if op == "write":
        content = _field(edit, "content", empty_ok=True)
        if text and not _FM_RE.match(content):
            existing_fm, _ = _parse_frontmatter(text)
            if existing_fm:
                content = _serialize_frontmatter(existing_fm, content)
        return content
    if op in ("append", "prepend") and text is None:
        content = _field(edit, "content")
        if edit.get("section"):
            raise ValueError("note does not exist, so it has no section")
        return content if content.endswith("\n") else content + "\n"
    if text is None:
        raise ValueError("note does not exist")
    if op == "replace":
        pattern, repl = _replacement(
            _field(edit, "find"),
            _field(edit, "replace", empty_ok=True),
            edit.get("mode", "exact"),
        )
        count = edit.get("count", 1)
        if not isinstance(count, int) or count < 0:
            raise ValueError("'count' must be an integer >= 0 (0 = all)")
        patched, n = pattern.subn(repl, text, count=count)
        if n == 0:
            raise ValueError(f"{edit['find']!r} not found")
        return patched
    if op == "section":
        section = _field(edit, "section")
        new_content = _field(edit, "new_content", empty_ok=True)
        mode = edit.get("mode", "replace")
        target_type = edit.get("target_type", "heading")
        if target_type == "block_ref":
            return _patch_block_ref(text, section.lstrip("^"), new_content, mode)
        if target_type != "heading":
            raise ValueError("'target_type' must be 'heading' or 'block_ref'")
        return _patch_section(text, section, new_content, mode)
    if op == "append":
        content = _field(edit, "content")
        section = _field(edit, "section", required=False)
        if section:
            return _patch_section(text, section, content, mode="append")
        return text.rstrip("\n") + "\n\n" + content.strip() + "\n"
    if op == "prepend":
        return _prepend_text(text, _field(edit, "content"))
    raise ValueError(f"unknown op {op!r} (use one of {', '.join(_BATCH_OPS)})")


def batch_edit(
    edits: list[dict],
    index: VaultIndex | None = None,
    dry_run: bool = False,
) -> dict:
    """Apply several edits to one or more notes, all or nothing.

    Edits run in order; several edits on one note build on each other. Every
    edit is checked against the current text before anything is written, and
    if a write still fails the notes already written are restored.
    """
    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must be a non-empty list")
    if len(edits) > BATCH_MAX_EDITS:
        raise ValueError(f"Too many edits ({len(edits)}; the limit is {BATCH_MAX_EDITS})")
    cfg = get_config()
    storage = _storage()

    order: list[str] = []
    pinned: dict[str, str] = {}
    steps: list[tuple[int, str, str, dict]] = []
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"edits[{i}] must be an object")
        op = edit.get("op")
        if op not in _BATCH_OPS:
            raise ValueError(f"edits[{i}]: unknown op {op!r} (use one of {', '.join(_BATCH_OPS)})")
        path = edit.get("path")
        if not isinstance(path, str) or not path.lower().endswith(".md"):
            raise ValueError(f"edits[{i}]: 'path' must be a note path ending in .md")
        rel = storage.resolve_write(path).relative
        if not storage.policy.can_read(rel):
            raise ReadPermissionError(f"batch_edit needs read access to {rel!r}")
        expected = edit.get("expected_revision")
        if expected is not None:
            expected = normalize_revision_token(expected)
            if pinned.setdefault(rel, expected) != expected:
                raise ValueError(f"edits[{i}]: conflicting expected_revision for {rel!r}")
        if rel not in order:
            order.append(rel)
        steps.append((i, rel, op, edit))

    locks = []
    try:
        for rel in sorted(order):
            locks.append(acquire_lock(rel, lock_path=cfg.lock_path))
        before: dict[str, str | None] = {}
        revisions: dict[str, str | None] = {}
        for rel in order:
            if storage.exists(rel, read=True):
                before[rel], revisions[rel] = read_text_for_update(storage, rel, pinned.get(rel))
            elif rel in pinned:
                raise RevisionConflictError(rel, pinned[rel], None)
            else:
                before[rel], revisions[rel] = None, None

        current = dict(before)
        for i, rel, op, edit in steps:
            if (
                op == "write"
                and before[rel] is not None
                and cfg.require_write_preconditions
                and rel not in pinned
            ):
                raise PreconditionRequiredError(rel)
            try:
                current[rel] = _apply_edit(current[rel], op, edit)
            except ValueError as exc:
                raise ValueError(
                    f"edits[{i}] ({op} on {rel!r}): {exc}; nothing was written"
                ) from exc

        changed = [rel for rel in order if current[rel] != before[rel]]
        notes = {
            rel: {
                "path": rel,
                "created": before[rel] is None,
                **_capped_diff(_unified_diff(before[rel] or "", current[rel], rel)),
            }
            for rel in changed
        }
        unchanged = [rel for rel in order if rel not in notes]

        if dry_run:
            for rel in changed:
                notes[rel]["revision"] = revisions[rel]
            return {
                "status": "dry_run",
                "edits": len(edits),
                "notes": list(notes.values()),
                "unchanged": unchanged,
            }

        # Existing notes first, new notes last: a failure among the overwrites
        # then needs no deletes to undo.
        planned = sorted(
            (_PlannedWrite(rel, before[rel], current[rel], revisions[rel]) for rel in changed),
            key=lambda write: write.before is None,
        )
        committed = _commit_writes(storage, planned)
    finally:
        for lock in reversed(locks):
            lock.release()

    if index is not None:
        for rel in changed:
            index.update(rel)

    for rel, revision in committed.items():
        notes[rel]["revision"] = revision.token
    return {
        "status": "applied",
        "edits": len(edits),
        "notes": list(notes.values()),
        "unchanged": unchanged,
    }


def _is_writable(rel_path: str) -> bool:
    """Like _check_write_permission, but returns False instead of raising —
    used to silently skip out-of-scope files during a vault-wide sweep
    instead of aborting the whole operation for one file."""
    try:
        _check_write_permission(rel_path)
    except WritePermissionError:
        return False
    return True


def _match_snippets(raw: str, pattern: re.Pattern, limit: int = 3) -> list[dict]:
    """Up to `limit` example matches, each with its 1-indexed line number and
    the full matched line — mirrors search_notes' snippet shape for
    familiarity, but simpler since there's no relevance scoring here."""
    snippets = []
    for i, line in enumerate(raw.splitlines()):
        if pattern.search(line):
            snippets.append({"line": i + 1, "text": line})
            if len(snippets) >= limit:
                break
    return snippets


def patch_note_text(
    path: str,
    find: str,
    replace: str,
    mode: str = "exact",
    count: int = 1,
    dry_run: bool = False,
    index: VaultIndex | None = None,
    expected_revision: str | None = None,
) -> dict:
    """Find and replace text anywhere in a note's body — no heading/block-ref
    anchor required, unlike patch_note_tool. For a handful of scattered
    single-note edits (e.g. "bump this one enum value" inside a long note)
    this is far cheaper than reading, rewriting, and re-sending the whole
    note through write_note_tool.

    mode: 'exact' (literal substring, default) | 'regex'.
    count: max replacements (default 1, first match only); 0 = replace all.
    dry_run=True (default False) previews matches and the resulting diff
    without writing anything.
    Raises ValueError if `find` doesn't match anything in the note.
    """
    _require_note_path(path)
    storage = _storage()
    target = storage.resolve_write(path)
    path = target.relative
    if not storage.exists(path, read=True):
        raise FileNotFoundError(f"Note not found: {path!r}")

    pattern, repl = _replacement(find, replace, mode)

    lock = acquire_lock(path, lock_path=get_config().lock_path)
    try:
        raw, current_revision = read_text_for_update(storage, path, expected_revision)
        replace_count = count if count > 0 else 0
        patched, n = pattern.subn(repl, raw, count=replace_count)
        if n == 0:
            raise ValueError(f"{find!r} not found in {path!r}")
        diff = _unified_diff(raw, patched, path)

        if dry_run:
            return {
                "path": path,
                "status": "dry_run",
                "replacements": n,
                "preview": _match_snippets(raw, pattern, limit=5),
                "diff": diff,
                "revision": current_revision,
            }

        revision = storage.write_text_atomic(path, patched, expected_revision=current_revision)
    finally:
        lock.release()

    if index is not None:
        index.update(path)

    return revision_result(
        {"path": path, "status": "patched", "replacements": n, "diff": diff}, revision
    )


def find_replace_in_vault(
    search: str,
    replace: str,
    mode: str = "exact",
    folder: str = "",
    dry_run: bool = True,
    index: VaultIndex | None = None,
) -> dict:
    """Find and replace text across every note in the vault (or a subfolder).

    mode: 'exact' (literal substring) | 'regex'.
    dry_run=True (default) only previews matches, writes nothing — always
    run once with dry_run=True first to check what would change before
    setting dry_run=False. .trash/ and EXCLUDE_PATHS are always skipped;
    files outside WRITE_PATHS (or all files, if the server is READ_ONLY)
    are silently skipped rather than aborting the whole run, and reported
    under "skipped_write_protected".
    """
    cfg = get_config()
    storage = _storage()

    pattern, repl = _replacement(search, replace, mode)

    candidates: list[tuple[str, str, int]] = []
    skipped_write_protected: list[str] = []
    for candidate in storage.list_files(folder):
        rel = candidate.relative
        if not rel.lower().endswith(".md"):
            continue
        if _is_excluded(rel, cfg.exclude_paths) or Path(rel).parts[0] == ".trash":
            continue
        try:
            raw = storage.read_text(rel)
        except Exception:
            continue
        count = len(pattern.findall(raw))
        if count == 0:
            continue
        if not _is_writable(rel):
            skipped_write_protected.append(rel)
            continue
        candidates.append((rel, raw, count))

    if dry_run:
        return {
            "dry_run": True,
            "matches": [
                {"path": rel, "match_count": count, "preview": _match_snippets(raw, pattern)}
                for rel, raw, count in candidates
            ],
            "total_matches": sum(count for *_, count in candidates),
            "skipped_write_protected": skipped_write_protected,
        }

    if not candidates:
        return {
            "dry_run": False,
            "replaced_in": [],
            "total_replacements": 0,
            "skipped_write_protected": skipped_write_protected,
        }

    locks = []
    try:
        for rel, _, _ in candidates:
            locks.append(acquire_lock(rel, lock_path=cfg.lock_path))
        replaced_in: list[str] = []
        total = 0
        for rel, raw, count in candidates:
            storage.write_text_atomic(rel, pattern.sub(repl, raw))
            replaced_in.append(rel)
            total += count
    finally:
        for lock in reversed(locks):
            lock.release()

    if index is not None:
        for rel in replaced_in:
            index.update(rel)

    return {
        "dry_run": False,
        "replaced_in": replaced_in,
        "total_replacements": total,
        "skipped_write_protected": skipped_write_protected,
    }

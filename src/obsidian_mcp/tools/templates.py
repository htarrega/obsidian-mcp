from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from ..config import get_config
from ..domain.index import VaultIndex
from ..storage.filesystem import VaultStorage
from ..storage.locking import acquire_lock
from ..storage.policy import VaultPathError
from ..storage.revisions import prepare_full_write, revision_result

# Matches {{variable}} and {{variable:format}}
_VAR_RE = re.compile(r"\{\{(\w+)(?::([^}]*))?\}\}")


def create_from_template(
    template_path: str,
    output_path: str,
    variables: dict | None = None,
    index: VaultIndex | None = None,
    expected_revision: str | None = None,
    create_only: bool = False,
) -> dict:
    """Render a template and write the result as a new note.

    template_path may be a vault path or a name relative to TEMPLATES_DIR.
    Built-in variables: date, time, title, week, month, month_num, year, weekday.
    Custom variables in the `variables` dict override built-ins.
    Supports date format specs: {{date:YYYY-MM}} → formatted date string.
    Preserves {{unknown_var}} as-is if not provided.

    expected_revision: pin replacement to a prior read of output_path; strict
    mode requires it when output_path already exists.
    create_only: require output_path to be absent and never overwrite it.
    """
    cfg = get_config()
    storage = VaultStorage.from_config(cfg)
    if not output_path.lower().endswith(".md"):
        raise ValueError("Template output paths must end in .md")
    output = storage.resolve_write(output_path)
    output_path = output.relative
    expected, effective_create_only = prepare_full_write(
        storage, output_path, expected_revision, create_only
    )

    template_path = resolve_template_path(storage, template_path)
    raw_template = storage.read_text(template_path)

    now = datetime.now()
    today = date.today()
    iso_cal = today.isocalendar()

    built_in: dict[str, str] = {
        "date": today.isoformat(),
        "time": now.strftime("%H:%M"),
        "title": Path(output_path).stem,
        "week": f"{iso_cal[0]}-W{iso_cal[1]:02d}",
        "month": today.strftime("%B"),
        "month_num": today.strftime("%m"),
        "year": str(today.year),
        "weekday": today.strftime("%A"),
    }

    merged = {**built_in, **(variables or {})}

    def _replace(m: re.Match) -> str:
        name = m.group(1)
        fmt = m.group(2)
        if name not in merged:
            return m.group(0)  # keep unknown vars unchanged
        val = merged[name]
        if fmt:
            try:
                d = date.fromisoformat(val)
                # Convert strftime-style format (YYYY→%Y, MM→%m, DD→%d, etc.)
                fmt_mapped = (fmt
                    .replace("YYYY", "%Y").replace("YY", "%y")
                    .replace("MM", "%m").replace("DD", "%d")
                    .replace("HH", "%H").replace("mm", "%M")
                    .replace("ss", "%S"))
                val = d.strftime(fmt_mapped)
            except Exception:
                pass
        return val

    rendered = _VAR_RE.sub(_replace, raw_template)
    lock = acquire_lock(output_path, lock_path=cfg.lock_path)
    try:
        revision = storage.write_text_atomic(
            output_path,
            rendered,
            expected_revision=expected,
            create_only=effective_create_only,
        )
    finally:
        lock.release()

    if index is not None:
        index.update(output_path)

    return revision_result({
        "template": template_path,
        "output": output_path,
        "status": "created",
        "variables": merged,
    }, revision)


def resolve_template_path(storage: VaultStorage, template_path: str) -> str:
    """Return template_path as given if it exists, else try it relative to
    the configured templates folder (with ``.md`` appended if missing)."""
    if storage.exists(template_path):
        return template_path
    templates_dir = get_config().layout.templates_dir
    candidates = [f"{templates_dir}/{template_path}"]
    if not template_path.lower().endswith(".md"):
        candidates += [f"{template_path}.md", f"{templates_dir}/{template_path}.md"]
    for candidate in candidates:
        try:
            if storage.exists(candidate):
                return candidate
        except (VaultPathError, PermissionError):
            continue
    return template_path


def list_templates_info() -> dict:
    """{items, templates_dir, hint?}: Markdown files under the configured
    templates folder. A missing folder yields an empty list plus a hint."""
    cfg = get_config()
    layout = cfg.layout
    storage = VaultStorage.from_config(cfg)
    result: dict = {"templates_dir": layout.templates_dir}
    try:
        exists = storage.exists(layout.templates_dir)
    except (VaultPathError, PermissionError):
        exists = False
    if not exists:
        source = "TEMPLATES_DIR" if layout.is_configured("templates_dir") else "the default"
        result["items"] = []
        result["hint"] = (
            f"Templates folder {layout.templates_dir!r} (from {source}) does not exist "
            "in this vault, so there are no templates. Write notes directly instead; "
            "the server admin can point TEMPLATES_DIR (or templates_dir in vaults.json) "
            "at the vault's template folder."
        )
        return result
    try:
        result["items"] = sorted(
            p.relative
            for p in storage.list_files(layout.templates_dir)
            if p.relative.lower().endswith(".md")
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError, VaultPathError):
        result["items"] = []
    if not result["items"]:
        result["hint"] = f"Templates folder {layout.templates_dir!r} contains no .md files."
    return result


def list_templates() -> list[str]:
    """List all template files in the configured templates folder."""
    return list_templates_info()["items"]

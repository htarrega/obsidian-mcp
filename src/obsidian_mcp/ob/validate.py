"""Validation of model-supplied values before they reach ``ob sync-config``.

``ob`` takes these settings as comma-separated lists, so every element is
checked individually and re-joined here — the model never supplies a raw CLI
argument. An empty list maps to ``""``, which ``ob`` treats as "clear".
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable

FILE_TYPES = ("image", "audio", "video", "pdf", "unsupported")
CONFIG_CATEGORIES = (
    "app",
    "appearance",
    "appearance-data",
    "hotkey",
    "core-plugin",
    "core-plugin-data",
    "community-plugin",
    "community-plugin-data",
)
CONFLICT_STRATEGIES = ("merge", "conflict")

MAX_FOLDERS = 200
MAX_FOLDER_CHARS = 512


class ObValidationError(ValueError):
    pass


def normalize_folder(raw: object) -> str:
    """Return a normalized vault-relative folder or raise ObValidationError."""
    if not isinstance(raw, str):
        raise ObValidationError("folder names must be strings")
    if any(ch in raw for ch in ("\x00", "\n", "\r")):
        raise ObValidationError("folder names must not contain NUL or newline characters")
    if "," in raw:
        raise ObValidationError(f"folder names must not contain commas: {raw!r}")
    if "\\" in raw:
        raise ObValidationError(f"use '/' as the folder separator: {raw!r}")
    folder = raw.strip()
    if not folder:
        raise ObValidationError("folder names must not be empty")
    if folder.startswith("/") or (len(folder) >= 2 and folder[1] == ":"):
        raise ObValidationError(f"folders must be relative to the vault root: {raw!r}")
    parts = [part for part in folder.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ObValidationError(f"folders must not contain '..': {raw!r}")
    if not parts:
        raise ObValidationError(f"folder does not name a directory: {raw!r}")
    normalized = posixpath.join(*parts)
    if normalized.startswith("-"):
        raise ObValidationError(f"folder names must not start with '-': {raw!r}")
    if len(normalized) > MAX_FOLDER_CHARS:
        raise ObValidationError("folder name is too long")
    return normalized


def excluded_folders_value(folders: Iterable[object]) -> tuple[str, list[str]]:
    """Validate folders; return (CSV value for ob, normalized list)."""
    items = list(folders)
    if len(items) > MAX_FOLDERS:
        raise ObValidationError(f"at most {MAX_FOLDERS} excluded folders are supported")
    normalized: list[str] = []
    for item in items:
        folder = normalize_folder(item)
        if folder not in normalized:
            normalized.append(folder)
    return ",".join(normalized), normalized


def enum_list_value(values: Iterable[object], allowed: tuple[str, ...], what: str) -> str:
    """Validate a list of enum members; return the deduplicated CSV."""
    selected: list[str] = []
    for value in values:
        if not isinstance(value, str) or value not in allowed:
            raise ObValidationError(
                f"invalid {what} {value!r}; allowed: {', '.join(allowed)}"
            )
        if value not in selected:
            selected.append(value)
    return ",".join(selected)


def conflict_strategy_value(strategy: object) -> str:
    if not isinstance(strategy, str) or strategy not in CONFLICT_STRATEGIES:
        raise ObValidationError(
            f"invalid conflict strategy {strategy!r}; allowed: {', '.join(CONFLICT_STRATEGIES)}"
        )
    return strategy

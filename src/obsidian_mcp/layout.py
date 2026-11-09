"""Per-vault layout settings: where templates, periodic notes, the conventions
file and the entry note live, and which folders orphan detection skips.

Every value is a vault-relative path validated by the same normaliser as the
access-policy rules (no absolute paths, no ``..`` escapes). The defaults
reproduce the historical hard-coded layout (``Templates/``, ``Journal/...``,
``_AI_INSTRUCTIONS.md``) so vaults without any configuration behave exactly
as before. Layout settings never grant access: every read still goes through
``VaultStorage`` and the vault's read/write policy.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from .storage.policy import VaultPathError, _normalise_relative

PERIODS = ("daily", "weekly", "monthly", "quarterly", "yearly")

DEFAULT_TEMPLATES_DIR = "Templates"
DEFAULT_CONVENTIONS_PATH = "_AI_INSTRUCTIONS.md"
DEFAULT_ORPHAN_EXCLUDES = ("Journal", "Templates")
DEFAULT_PERIODIC: Mapping[str, str] = {
    "daily": "Journal/{{date:YYYY-MM-DD}}.md",
    "weekly": "Journal/Weekly/{{date:GGGG-[W]WW}}.md",
    "monthly": "Journal/Monthly/{{date:YYYY-MM}}.md",
    "quarterly": "Journal/Quarterly/{{date:YYYY-[Q]Q}}.md",
    "yearly": "Journal/Yearly/{{date:YYYY}}.md",
}
# Default moment format per period for a bare ``{{date}}`` placeholder.
_PERIOD_DEFAULT_FORMAT = {
    "daily": "YYYY-MM-DD",
    "weekly": "GGGG-[W]WW",
    "monthly": "YYYY-MM",
    "quarterly": "YYYY-[Q]Q",
    "yearly": "YYYY",
}
PERIOD_TEMPLATE_NAMES = {
    "daily": "Daily-Note-Template.md",
    "weekly": "Weekly-Note-Template.md",
    "monthly": "Monthly-Note-Template.md",
    "quarterly": "Quarterly-Note-Template.md",
    "yearly": "Yearly-Note-Template.md",
}


class LayoutError(ValueError):
    """An invalid layout setting (reported as a ConfigError at startup)."""


# ── moment.js-style date formatting ─────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{\{\s*date(?:\s*:\s*([^}]*))?\s*\}\}")
_MOMENT_TOKEN_RE = re.compile(
    r"\[[^\]]*\]|YYYY|YY|GGGG|gggg|Q|MMMM|MMM|MM|M|DDDD|DDD|DD|D|dddd|ddd|WW|W|ww|w|E|e"
)
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")


def format_moment(value: date, fmt: str) -> str:
    """Format ``value`` with the moment.js tokens Obsidian users write in
    Daily/Periodic Notes settings. Weeks are ISO weeks (``ww``/``w`` are
    treated like ``WW``/``W``); ``[text]`` is a literal."""
    iso_year, iso_week, iso_day = value.isocalendar()
    day_of_year = value.timetuple().tm_yday

    def _token(match: re.Match) -> str:
        token = match.group(0)
        if token.startswith("["):
            return token[1:-1]
        return {
            "YYYY": f"{value.year:04d}",
            "YY": f"{value.year % 100:02d}",
            "GGGG": f"{iso_year:04d}",
            "gggg": f"{iso_year:04d}",
            "Q": str((value.month - 1) // 3 + 1),
            "MMMM": _MONTHS[value.month - 1],
            "MMM": _MONTHS[value.month - 1][:3],
            "MM": f"{value.month:02d}",
            "M": str(value.month),
            "DDDD": f"{day_of_year:03d}",
            "DDD": str(day_of_year),
            "DD": f"{value.day:02d}",
            "D": str(value.day),
            "dddd": _WEEKDAYS[value.weekday()],
            "ddd": _WEEKDAYS[value.weekday()][:3],
            "WW": f"{iso_week:02d}",
            "W": str(iso_week),
            "ww": f"{iso_week:02d}",
            "w": str(iso_week),
            "E": str(iso_day),
            "e": str(iso_day - 1),
        }[token]

    return _MOMENT_TOKEN_RE.sub(_token, fmt)


def render_periodic_path(pattern: str, period: str, value: date) -> str:
    """Expand every ``{{date[:FORMAT]}}`` in ``pattern`` and validate the
    result as a vault-relative Markdown path."""
    default_fmt = _PERIOD_DEFAULT_FORMAT.get(period, "YYYY-MM-DD")
    rendered = _PLACEHOLDER_RE.sub(
        lambda m: format_moment(value, (m.group(1) or "").strip() or default_fmt),
        pattern,
    )
    if not rendered.lower().endswith(".md"):
        rendered += ".md"
    try:
        return _normalise_relative(rendered, allow_empty=False)
    except VaultPathError as exc:
        raise LayoutError(str(exc)) from exc


# ── path helpers ────────────────────────────────────────────────────────────

def _fold(value: str) -> str:
    """Unicode- and case-insensitive comparison key (NFC + casefold)."""
    return unicodedata.normalize("NFC", value).casefold()


def _clean_path(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LayoutError(f"{name} must be a non-empty string")
    try:
        return _normalise_relative(value.strip().rstrip("/\\"), allow_empty=False)
    except VaultPathError as exc:
        raise LayoutError(f"{name}: {exc}") from exc


def _clean_pattern(value: object, *, name: str, period: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LayoutError(f"{name} must be a non-empty string")
    pattern = value.strip()
    if not _PLACEHOLDER_RE.search(pattern):
        raise LayoutError(
            f"{name} must contain a {{{{date}}}} or {{{{date:FORMAT}}}} placeholder"
        )
    # Validate with two sample dates (year boundary + mid-year) so a pattern
    # that renders outside the vault fails at startup, not on first call.
    for sample in (date(2025, 12, 31), date(2026, 6, 15)):
        render_periodic_path(pattern, period, sample)
    return pattern


def path_in_folders(path: str, folders: tuple[str, ...]) -> bool:
    """True if ``path`` lies under one of ``folders``.

    An entry containing ``/`` is a rooted prefix (``A/B`` matches ``A/B/x.md``).
    A single-segment entry matches that folder at any depth (``Journal``
    matches ``Journal/x.md`` and ``Areas/Journal/x.md``), which is the
    historical ``exclude_folders`` behaviour. Comparison is case- and
    Unicode-normalisation-insensitive.
    """
    folded = _fold(path)
    parts = folded.split("/")[:-1]
    for folder in folders:
        key = _fold(folder.strip().strip("/"))
        if not key:
            continue
        if "/" in key:
            if folded.startswith(key + "/"):
                return True
        elif key in parts:
            return True
    return False


def same_path(left: str | None, right: str | None) -> bool:
    return bool(left) and bool(right) and _fold(left) == _fold(right)


# ── the layout ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VaultLayout:
    templates_dir: str = DEFAULT_TEMPLATES_DIR
    # (period, pattern) pairs — a tuple keeps the dataclass hashable.
    periodic: tuple[tuple[str, str], ...] = tuple(DEFAULT_PERIODIC.items())
    orphan_excludes: tuple[str, ...] = DEFAULT_ORPHAN_EXCLUDES
    entry_note: str | None = None
    conventions_path: str = DEFAULT_CONVENTIONS_PATH
    # Which settings were configured explicitly (vs. defaults), for hints.
    configured: frozenset[str] = field(default_factory=frozenset)

    def periodic_pattern(self, period: str) -> str:
        for name, pattern in self.periodic:
            if name == period:
                return pattern
        raise LayoutError(
            f"Unknown period {period!r}. Use: {'|'.join(PERIODS)}"
        )

    def is_configured(self, key: str) -> bool:
        return key in self.configured


def _build(values: Mapping[str, object], *, prefix: str, env_style: bool) -> VaultLayout:
    """Build a layout from already-extracted raw values (None = default)."""
    def key(name: str) -> str:
        return f"{prefix}{name.upper() if env_style else name}"

    configured: set[str] = set()
    kwargs: dict[str, object] = {}

    templates_dir = values.get("templates_dir")
    if templates_dir is not None:
        kwargs["templates_dir"] = _clean_path(templates_dir, name=key("templates_dir"))
        configured.add("templates_dir")

    conventions = values.get("conventions_path")
    if conventions is not None:
        kwargs["conventions_path"] = _clean_path(conventions, name=key("conventions_path"))
        configured.add("conventions_path")

    entry = values.get("entry_note")
    if entry is not None:
        entry_path = _clean_path(entry, name=key("entry_note"))
        if not entry_path.lower().endswith(".md"):
            entry_path += ".md"
        kwargs["entry_note"] = entry_path
        configured.add("entry_note")

    excludes = values.get("orphan_excludes")
    if excludes is not None:
        if not isinstance(excludes, (list, tuple)) or not all(
            isinstance(item, str) for item in excludes
        ):
            raise LayoutError(f"{key('orphan_excludes')} must be a list of strings")
        cleaned: list[str] = []
        for item in excludes:
            if item.strip():
                folder = _clean_path(item, name=key("orphan_excludes"))
                if folder not in cleaned:
                    cleaned.append(folder)
        kwargs["orphan_excludes"] = tuple(cleaned)
        configured.add("orphan_excludes")

    periodic_raw = values.get("periodic")
    periodic = dict(DEFAULT_PERIODIC)
    if periodic_raw is not None:
        if not isinstance(periodic_raw, Mapping):
            raise LayoutError(f"{key('periodic')} must be an object of period → pattern")
        unknown = sorted(set(periodic_raw) - set(PERIODS))
        if unknown:
            raise LayoutError(
                f"{key('periodic')} has unknown period(s): {', '.join(map(str, unknown))} "
                f"(allowed: {', '.join(PERIODS)})"
            )
        for period, pattern in periodic_raw.items():
            if pattern is None:
                continue
            name = (
                f"PERIODIC_{period.upper()}_PATH" if env_style else f"{prefix}periodic.{period}"
            )
            periodic[period] = _clean_pattern(pattern, name=name, period=period)
            configured.add(f"periodic.{period}")
    kwargs["periodic"] = tuple((period, periodic[period]) for period in PERIODS)

    return VaultLayout(configured=frozenset(configured), **kwargs)  # type: ignore[arg-type]


def layout_from_env(environ: Mapping[str, str] | None = None) -> VaultLayout:
    """Single-vault layout from ``TEMPLATES_DIR``, ``PERIODIC_<PERIOD>_PATH``,
    ``ORPHAN_EXCLUDES``, ``ENTRY_NOTE`` and ``CONVENTIONS_PATH``. Unset or
    blank variables keep the default."""
    env = os.environ if environ is None else environ

    def get(name: str) -> str | None:
        value = env.get(name, "")
        return value if value.strip() else None

    values: dict[str, object] = {
        "templates_dir": get("TEMPLATES_DIR"),
        "conventions_path": get("CONVENTIONS_PATH"),
        "entry_note": get("ENTRY_NOTE"),
    }
    raw_excludes = env.get("ORPHAN_EXCLUDES")
    if raw_excludes is not None and raw_excludes.strip():
        values["orphan_excludes"] = [part for part in raw_excludes.split(",") if part.strip()]
    periodic = {
        period: get(f"PERIODIC_{period.upper()}_PATH")
        for period in PERIODS
        if get(f"PERIODIC_{period.upper()}_PATH") is not None
    }
    if periodic:
        values["periodic"] = periodic
    return _build(values, prefix="", env_style=True)


def layout_from_json(raw_entry: Mapping[str, object], *, vault_name: str) -> VaultLayout:
    """Layout keys of one ``vaults.json`` entry (all optional)."""
    values = {
        name: raw_entry.get(name)
        for name in (
            "templates_dir",
            "conventions_path",
            "entry_note",
            "orphan_excludes",
            "periodic",
        )
    }
    return _build(values, prefix=f"vault {vault_name!r} ", env_style=False)

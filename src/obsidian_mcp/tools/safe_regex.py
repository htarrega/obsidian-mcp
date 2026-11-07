"""ReDoS-bounded compilation of caller-supplied regular expressions.

Tools that accept a user regex (search_notes mode='regex', patch_note_text and
find_replace_in_vault mode='regex') compile it through ``compile_user_regex``.
It enforces a maximum pattern length (REGEX_MAX_LENGTH) and runs every match
with a per-call timeout (REGEX_TIMEOUT_SECONDS) using the third-party
``regex`` engine, whose default (VERSION0) syntax and replacement templates
are compatible with the stdlib ``re`` module. Callers abort the whole
operation on the first timeout, so a catastrophic pattern costs at most one
timeout rather than one per line/file.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import regex

from ..config import get_config


class RegexTooLongError(ValueError):
    """A user-supplied regex is longer than REGEX_MAX_LENGTH."""


class RegexTimeoutError(ValueError):
    """A user-supplied regex exceeded REGEX_TIMEOUT_SECONDS on one match."""


# stdlib re flag -> regex flag (same bit values today, mapped explicitly anyway)
_FLAG_MAP = {
    re.IGNORECASE: regex.IGNORECASE,
    re.MULTILINE: regex.MULTILINE,
    re.DOTALL: regex.DOTALL,
    re.VERBOSE: regex.VERBOSE,
    re.ASCII: regex.ASCII,
}


class SafePattern:
    """Wraps a compiled ``regex`` pattern; the methods used by the tools
    mirror ``re.Pattern`` but always pass the configured timeout."""

    def __init__(self, compiled: Any, timeout: float) -> None:
        self._compiled = compiled
        self._timeout = timeout
        self.pattern = compiled.pattern

    def _run(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, timeout=self._timeout, **kwargs)
        except TimeoutError as exc:
            raise RegexTimeoutError(
                f"Regex took longer than {self._timeout:g}s on a single match and was aborted "
                "(possible catastrophic backtracking); simplify the pattern"
            ) from exc

    def search(self, string: str) -> Any:
        return self._run(self._compiled.search, string)

    def findall(self, string: str) -> list:
        return self._run(self._compiled.findall, string)

    def sub(self, repl: Any, string: str, count: int = 0) -> str:
        return self._run(self._compiled.sub, repl, string, count=count)

    def subn(self, repl: Any, string: str, count: int = 0) -> tuple[str, int]:
        return self._run(self._compiled.subn, repl, string, count=count)


def compile_user_regex(pattern: str, flags: int = 0) -> SafePattern:
    """Compile a caller-supplied regex with length and time limits.

    Raises RegexTooLongError (a ValueError) if the pattern is too long and
    ValueError if it is invalid.
    """
    cfg = get_config()
    if len(pattern) > cfg.regex_max_length:
        raise RegexTooLongError(
            f"Regex is too long ({len(pattern)} characters; REGEX_MAX_LENGTH is "
            f"{cfg.regex_max_length})"
        )
    regex_flags = 0
    for re_flag, regex_flag in _FLAG_MAP.items():
        if flags & re_flag:
            regex_flags |= regex_flag
    try:
        compiled = regex.compile(pattern, regex_flags | regex.VERSION0)
    except (regex.error, ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"Invalid regex: {exc}") from exc
    return SafePattern(compiled, cfg.regex_timeout_seconds)

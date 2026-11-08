"""ReDoS-bounded compilation of caller-supplied regular expressions.

Tools that accept a user regex (search_notes mode='regex', patch_note_text and
find_replace_in_vault mode='regex') compile it through ``compile_user_regex``.
It enforces a maximum pattern length (REGEX_MAX_LENGTH) and runs every match
with a per-call timeout (REGEX_TIMEOUT_SECONDS) using the third-party
``regex`` engine, whose default (VERSION0) syntax and replacement templates
are compatible with the stdlib ``re`` module. Callers abort the whole
operation on the first timeout, so a catastrophic pattern costs at most one
timeout rather than one per line/file.

The match timeout does not cover *compilation*: nested counted repetitions
such as ``(((a{50}){50}){50}){20}`` (23 characters) make the engine allocate
hundreds of megabytes while compiling. ``check_repetition_limits`` therefore
scans the pattern statically before it is compiled and rejects counted
repetitions whose bound exceeds REGEX_MAX_REPEAT, that nest deeper than
MAX_COUNTED_NESTING, or whose nested bounds multiply past
MAX_REPETITION_PRODUCT.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import regex

from ..config import get_config

# Nested counted repetitions (``{n}``/``{n,m}``/``{n,}`` with an upper bound
# above 1) deeper than this are rejected. ``*``, ``+`` and ``?`` do not count:
# they are not expanded at compile time.
MAX_COUNTED_NESTING = 2
# Upper bound for the product of nested repetition counts of any atom.
MAX_REPETITION_PRODUCT = 10_000
# Digits in a single repeat bound beyond which it is "too big" without int().
_MAX_BOUND_DIGITS = 9


class RegexTooLongError(ValueError):
    """A user-supplied regex is longer than REGEX_MAX_LENGTH."""


class RegexTooComplexError(ValueError):
    """A user-supplied regex repeats too much to be compiled safely."""


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

# ``{n}``, ``{n,}``, ``{,m}``, ``{n,m}`` (the ``regex`` module accepts all four).
_BRACE_QUANT_RE = re.compile(r"\{(\d*)(?:(,)(\d*))?\}")
# An inline flag group that turns on verbose mode, e.g. ``(?x)`` or ``(?ix)``.
_INLINE_VERBOSE_RE = re.compile(r"\(\?[a-zA-Z]*x[a-zA-Z]*[):]")
# Escapes that may take a ``{...}`` argument: \p{L}, \P{L}, \N{NAME}, \x{263a}.
_BRACE_ESCAPES = frozenset("pPNx")


class _Cost:
    """Worst-case repetition cost of an atom: product of the counted bounds
    applied to it and how many counted repetitions are nested."""

    __slots__ = ("depth", "product")

    def __init__(self, product: int = 1, depth: int = 0) -> None:
        self.product = product
        self.depth = depth

    def merge(self, other: _Cost) -> None:
        self.product = max(self.product, other.product)
        self.depth = max(self.depth, other.depth)


class _Frame:
    """One open group: the largest cost of anything inside it so far."""

    __slots__ = ("inner",)

    def __init__(self) -> None:
        self.inner = _Cost()


def _skip_class(pattern: str, i: int) -> int:
    """``pattern[i] == '['``; return the index just past the closing ``]``."""
    n = len(pattern)
    i += 1
    if i < n and pattern[i] == "^":
        i += 1
    if i < n and pattern[i] == "]":  # a leading ']' is a literal
        i += 1
    while i < n:
        char = pattern[i]
        if char == "\\":
            i += 2
        elif char == "[" and pattern.startswith("[:", i):
            end = pattern.find(":]", i + 2)
            i = end + 2 if end != -1 else i + 1
        elif char == "]":
            return i + 1
        else:
            i += 1
    return n


def _skip_escape(pattern: str, i: int) -> int:
    """``pattern[i] == '\\'``; return the index just past the escape."""
    n = len(pattern)
    if i + 1 < n and pattern[i + 1] in _BRACE_ESCAPES and pattern.startswith("{", i + 2):
        end = pattern.find("}", i + 3)
        return end + 1 if end != -1 else n
    return i + 2


def check_repetition_limits(pattern: str, max_repeat: int, *, verbose: bool = False) -> None:
    """Reject patterns whose counted repetitions are expensive to *compile*.

    A conservative static scan (it over-approximates: alternatives inside a
    group are costed as their maximum, and anything it does not understand is
    treated as a literal). Raises :class:`RegexTooComplexError` when

    * any ``{n}``/``{n,m}``/``{n,}`` bound is larger than ``max_repeat``;
    * counted repetitions with an upper bound above 1 nest deeper than
      MAX_COUNTED_NESTING;
    * the product of nested repetition bounds exceeds MAX_REPETITION_PRODUCT.
    """
    verbose = verbose or bool(_INLINE_VERBOSE_RE.search(pattern))
    stack: list[_Frame] = [_Frame()]
    last: _Cost | None = None  # cost of the atom a quantifier would apply to
    after_quantifier = False
    n = len(pattern)
    i = 0

    def apply(bound: int, counted: bool) -> None:
        nonlocal last
        if last is None:  # nothing to repeat: compile() reports the error
            return
        cost = _Cost(last.product * max(bound, 1), last.depth + (1 if counted else 0))
        if cost.depth > MAX_COUNTED_NESTING:
            raise RegexTooComplexError(
                f"Regex nests counted repetitions ({{n}}) more than {MAX_COUNTED_NESTING} "
                "levels deep; simplify the pattern"
            )
        if cost.product > MAX_REPETITION_PRODUCT:
            raise RegexTooComplexError(
                f"Regex nested repetitions multiply to more than {MAX_REPETITION_PRODUCT}; "
                "simplify the pattern"
            )
        last = cost
        stack[-1].inner.merge(cost)

    while i < n:
        char = pattern[i]
        if verbose and char in " \t\n\r\f\v":
            i += 1
            continue
        if verbose and char == "#":
            end = pattern.find("\n", i)
            i = n if end == -1 else end + 1
            continue

        if char in "*+?":
            if after_quantifier and char in "?+":  # lazy / possessive modifier
                after_quantifier = False
                i += 1
                continue
            apply(1, counted=False)
            after_quantifier = True
            i += 1
            continue
        if char == "{":
            match = _BRACE_QUANT_RE.match(pattern, i)
            if match and (match.group(1) or match.group(3)):
                low, comma, high = match.groups()
                for digits in (low, high):
                    if digits and (len(digits) > _MAX_BOUND_DIGITS or int(digits) > max_repeat):
                        raise RegexTooComplexError(
                            f"Regex repetition count {match.group(0)} is larger than "
                            f"REGEX_MAX_REPEAT ({max_repeat})"
                        )
                upper = int(high) if comma and high else int(low or 0)
                # {n,} repeats unboundedly at match time; its compile cost is n.
                apply(max(upper, 1), counted=bool(comma and not high) or upper > 1)
                after_quantifier = True
                i = match.end()
                continue
            # Not a quantifier: a literal '{'.

        after_quantifier = False
        if char == "\\":
            i = _skip_escape(pattern, i)
            last = _Cost()
        elif char == "[":
            i = _skip_class(pattern, i)
            last = _Cost()
        elif char == "(":
            if pattern.startswith("(?#", i):  # comment group
                end = pattern.find(")", i)
                i = n if end == -1 else end + 1
                continue
            stack.append(_Frame())
            last = None
            i += 1
        elif char == ")":
            if len(stack) > 1:
                group = stack.pop().inner
                stack[-1].inner.merge(group)
                last = _Cost(group.product, group.depth)
            else:
                last = None
            i += 1
        elif char == "|":
            last = None
            i += 1
        else:
            last = _Cost()
            i += 1


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
    """Compile a caller-supplied regex with length, repetition and time limits.

    Raises RegexTooLongError (a ValueError) if the pattern is too long,
    RegexTooComplexError (a ValueError) if its counted repetitions are too
    large to compile safely, and ValueError if it is invalid.
    """
    cfg = get_config()
    if len(pattern) > cfg.regex_max_length:
        raise RegexTooLongError(
            f"Regex is too long ({len(pattern)} characters; REGEX_MAX_LENGTH is "
            f"{cfg.regex_max_length})"
        )
    check_repetition_limits(
        pattern, cfg.regex_max_repeat, verbose=bool(flags & re.VERBOSE)
    )
    regex_flags = 0
    for re_flag, regex_flag in _FLAG_MAP.items():
        if flags & re_flag:
            regex_flags |= regex_flag
    try:
        compiled = regex.compile(pattern, regex_flags | regex.VERSION0)
    except (regex.error, ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"Invalid regex: {exc}") from exc
    except (MemoryError, RecursionError) as exc:
        raise ValueError("Regex is too complex to compile; simplify the pattern") from exc
    return SafePattern(compiled, cfg.regex_timeout_seconds)

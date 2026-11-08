"""Secret redaction for everything the ``ob`` wrapper returns or logs.

``ob`` output can contain the account e-mail, auth tokens and the vault
encryption key. Every string that leaves this package — tool results, the
log ring buffer, error messages and server log records — passes through
:func:`redact` (free text) or :func:`redact_obj` (parsed JSON) first.

The rules are deliberately conservative: a false positive costs a little
log readability, a false negative leaks a credential.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

MASK = "[REDACTED]"
EMAIL_MASK = "[REDACTED_EMAIL]"

# Environment variables whose *values* are masked verbatim wherever they appear.
_SECRET_ENV_VARS = ("OBSIDIAN_AUTH_TOKEN",)
# Literal secrets registered at runtime (e.g. the ob auth_token file content),
# masked verbatim wherever they appear. Never returned by any function.
_LITERAL_SECRETS: set[str] = set()
_LITERAL_LOCK = threading.Lock()
_MIN_LITERAL_LEN = 4
_MAX_SECRET_FILE_BYTES = 64 * 1024

# The lookbehind anchors matches at the start of a run, keeping the scan
# linear on long runs without an '@' (output can be up to 256 KB).
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,256}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[^\s\"',;]+")
# ``name=value`` / ``name: value`` / ``"name": "value"`` for secret-looking
# names. A quoted value is masked up to its closing quote (escapes honoured);
# an unquoted one up to the next separator, or to the end of the line for
# names whose values may contain spaces (passwords, passphrases, cookies).
_SEP = r"[\"']?\s*[:=]\s*"
_QUOTED = r"\"(?P<dq>(?:[^\"\\\r\n]|\\.)*)\"|'(?P<sq>(?:[^'\\\r\n]|\\.)*)'"
# Unambiguous secret names: matched even inside identifiers such as
# ``client_secret`` or ``OAUTH_TOKEN`` (only letters/digits break them).
_STRONG_PREFIX = r"(?<![A-Za-z0-9])"
_STRONG_SUFFIX = r"(?![A-Za-z0-9])"
# Generic names (``key``, ``session``, ``cookie``): only at the start of a
# word-ish field (start, whitespace, quote, ``{``, ``,``, ``;``, ``&``, ``?``,
# ``(``, ``[``), so a path like ``notes/key=value.md`` is left alone.
_WEAK_PREFIX = r"(?<![^\s\"'{,;&?(\[])"
_EOL_NAMES = r"password|passwd|pwd|passphrase|secret"
_VALUE_NAMES = (
    r"token|auth[_-]?token|access[_-]?token|refresh[_-]?token|api[_-]?key|"
    r"encryption[_-]?key|encryptionkey|salt"
)
_WEAK_EOL_NAMES = r"(?:set[_-]?)?cookie"
_WEAK_VALUE_NAMES = r"key|session(?:[_-]?(?:id|token|key))?"
_KEY_VALUE_RES = (
    re.compile(
        rf"(?i){_STRONG_PREFIX}(?:{_EOL_NAMES}){_STRONG_SUFFIX}{_SEP}"
        rf"(?:{_QUOTED}|(?P<bare>[^\r\n]+))"
    ),
    re.compile(
        rf"(?i){_STRONG_PREFIX}(?:{_VALUE_NAMES}){_STRONG_SUFFIX}{_SEP}"
        rf"(?:{_QUOTED}|(?P<bare>[^\s\"',;}}&]+))"
    ),
    re.compile(
        rf"(?i){_WEAK_PREFIX}(?:{_WEAK_EOL_NAMES}){_STRONG_SUFFIX}{_SEP}"
        rf"(?:{_QUOTED}|(?P<bare>[^\r\n]+))"
    ),
    re.compile(
        rf"(?i){_WEAK_PREFIX}(?:{_WEAK_VALUE_NAMES}){_STRONG_SUFFIX}{_SEP}"
        rf"(?:{_QUOTED}|(?P<bare>[^\s\"',;}}&]+))"
    ),
)
# Long opaque runs: candidates for tokens, keys, hex digests, base64 blobs.
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=_\-])[A-Za-z0-9+/=_\-]{24,}")
_HEX_RE = re.compile(r"[0-9a-fA-F]+")

# JSON keys whose values are always dropped, whatever they look like: any
# key *containing* one of these fragments (case-insensitive), plus exact
# matches of _SENSITIVE_KEYS.
_SENSITIVE_KEY_FRAGMENTS = (
    "key",
    "token",
    "secret",
    "pass",
    "pwd",
    "salt",
    "cookie",
    "session",
)
_SENSITIVE_KEYS = frozenset({"email"})
# JSON keys holding vault identifiers. Obsidian vault ids are 32 hex chars,
# which the long-token rule would otherwise mask; an id is not a credential
# and is needed to tell vaults apart, so it is kept when it looks like one.
_ID_KEYS = frozenset({"id", "vaultid"})
_ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")
# JSON keys holding local filesystem paths (exempt from the long-token rule).
_PATH_KEYS = frozenset({"path", "vaultpath", "configdir"})


def _is_sensitive_key(normalised: str) -> bool:
    bare = normalised.replace("_", "")
    if bare in _ID_KEYS:
        return False
    return bare in _SENSITIVE_KEYS or any(frag in bare for frag in _SENSITIVE_KEY_FRAGMENTS)


def register_secret(value: str) -> None:
    """Mask ``value`` verbatim in all future redactions (if long enough)."""
    value = value.strip()
    if len(value) >= _MIN_LITERAL_LEN:
        with _LITERAL_LOCK:
            _LITERAL_SECRETS.add(value)


def register_secret_file(path: Path) -> bool:
    """Register the content of a secret file (e.g. ob's ``auth_token``).

    Returns whether a secret was registered; the content itself is never
    returned or logged. Missing/unreadable files are ignored.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_SECRET_FILE_BYTES)
    except OSError:
        return False
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) < _MIN_LITERAL_LEN:
        return False
    register_secret(text)
    for line in text.splitlines():
        register_secret(line)
    return True


def clear_registered_secrets() -> None:
    """Forget all registered literal secrets (tests)."""
    with _LITERAL_LOCK:
        _LITERAL_SECRETS.clear()


def _mask_value(match: re.Match[str]) -> str:
    for group in ("dq", "sq", "bare"):
        if match.group(group) is not None:
            start, end = match.span(group)
            break
    else:  # pragma: no cover - one alternative always matches
        return match.group(0)
    whole = match.group(0)
    offset = match.start()
    return whole[: start - offset] + MASK + whole[end - offset :]


def _looks_secret(token: str) -> bool:
    stripped = token.strip("=")
    if len(stripped) < 24:
        return False
    if _HEX_RE.fullmatch(stripped):
        return True
    has_upper = any(c.isupper() for c in stripped)
    has_lower = any(c.islower() for c in stripped)
    has_digit = any(c.isdigit() for c in stripped)
    return has_upper and has_lower and has_digit


def _mask_long_token(match: re.Match[str]) -> str:
    token = match.group(0)
    return MASK if _looks_secret(token) else token


def redact(text: str, *, long_tokens: bool = True) -> str:
    """Mask credentials, e-mails and opaque secrets in free text.

    ``long_tokens=False`` skips the opaque-run heuristic; it is used only for
    JSON values under filesystem-path keys, which it would otherwise mangle.
    """
    if not text:
        return text
    literals = {os.environ.get(name, "") for name in _SECRET_ENV_VARS}
    with _LITERAL_LOCK:
        literals |= _LITERAL_SECRETS
    # Longest first, so a secret containing another is masked whole.
    for value in sorted(literals, key=len, reverse=True):
        if len(value) >= _MIN_LITERAL_LEN:
            text = text.replace(value, MASK)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {MASK}", text)
    for pattern in _KEY_VALUE_RES:
        text = pattern.sub(_mask_value, text)
    text = _EMAIL_RE.sub(EMAIL_MASK, text)
    if not long_tokens:
        return text
    return _LONG_TOKEN_RE.sub(_mask_long_token, text)


def _normalise_key(key: object) -> str:
    return str(key).replace("-", "_").lower()


def redact_obj(value: Any, *, _key: str | None = None) -> Any:
    """Recursively redact parsed JSON, dropping values under secret keys."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalised = _normalise_key(key)
            if _is_sensitive_key(normalised):
                result[str(key)] = MASK
            else:
                result[str(key)] = redact_obj(item, _key=normalised)
        return result
    if isinstance(value, list):
        return [redact_obj(item, _key=_key) for item in value]
    if isinstance(value, str):
        bare_key = _key.replace("_", "") if _key is not None else None
        if bare_key in _ID_KEYS and _ID_RE.fullmatch(value):
            return value
        return redact(value, long_tokens=bare_key not in _PATH_KEYS)
    return value

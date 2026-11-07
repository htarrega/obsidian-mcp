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
from typing import Any

MASK = "[REDACTED]"
EMAIL_MASK = "[REDACTED_EMAIL]"

# Environment variables whose *values* are masked verbatim wherever they appear.
_SECRET_ENV_VARS = ("OBSIDIAN_AUTH_TOKEN",)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[^\s\"',;]+")
# ``name=value`` / ``name: value`` / ``"name": "value"`` for secret-looking names.
_KEY_VALUE_RE = re.compile(
    r"(?i)(\"?\b(?:password|passwd|pwd|passphrase|secret|token|auth[_-]?token|"
    r"access[_-]?token|refresh[_-]?token|api[_-]?key|encryption[_-]?key|"
    r"encryptionkey|salt)\b\"?\s*[:=]\s*\"?)([^\s\"',;}&]+)"
)
# Long opaque runs: candidates for tokens, keys, hex digests, base64 blobs.
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=_\-])[A-Za-z0-9+/=_\-]{24,}")
_HEX_RE = re.compile(r"[0-9a-fA-F]+")

# JSON keys whose values are always dropped, whatever they look like.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "secret",
        "token",
        "authtoken",
        "auth_token",
        "accesstoken",
        "access_token",
        "refreshtoken",
        "refresh_token",
        "apikey",
        "api_key",
        "key",
        "encryptionkey",
        "encryption_key",
        "salt",
        "email",
    }
)
# JSON keys holding vault identifiers. Obsidian vault ids are 32 hex chars,
# which the long-token rule would otherwise mask; an id is not a credential
# and is needed to tell vaults apart, so it is kept when it looks like one.
_ID_KEYS = frozenset({"id", "vaultid"})
_ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")
# JSON keys holding local filesystem paths (exempt from the long-token rule).
_PATH_KEYS = frozenset({"path", "vaultpath", "configdir"})


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
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name, "")
        if len(value) >= 4:
            text = text.replace(value, MASK)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}{MASK}", text)
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
            if normalised in _SENSITIVE_KEYS or normalised.replace("_", "") in _SENSITIVE_KEYS:
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

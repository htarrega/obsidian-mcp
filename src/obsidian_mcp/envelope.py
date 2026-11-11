"""Canonical response envelope shared by every MCP tool.

Every tool returns a JSON **object** built through one of the constructors
below — never a bare list, string, or hand-rolled dict. That gives clients a
single shape to parse regardless of which tool they called:

    {
      "success": true,
      "path": "02-Areas/monari/ticket-123.md",
      "revision": "sha256:ab12…",
      "data": {...},      # tool-specific payload
      "meta": {...}       # action, dry_run, count, truncated, …
    }

Rules
-----
* ``success`` is always present and always a boolean — never a ``status`` string.
* ``path`` is present for every single-item result and omitted for vault-wide
  and list results, which are not addressed by one path.
* ``revision`` is present on every read and every successful write of a single
  file, and is the opaque token to feed back as ``expected_revision`` for
  compare-and-swap (see :mod:`obsidian_mcp.storage.revisions`).
* ``data`` is always present so clients can index into it unconditionally.
* ``meta`` is present only when there is something to say. The operation verb
  of a write lives in ``meta.action`` — operation metadata, not payload.

Hard failures are MCP error results (``isError: true``), never a successful
envelope. Their structured content is built by :func:`error_result` and still
fits :data:`ENVELOPE_SCHEMA` — ``success: false``, an empty ``data``, a stable
machine-readable ``code`` (one of :data:`ERROR_CODES`) and the human
``message`` — so a client that validates every result against the advertised
schema does not choke on errors (see :mod:`obsidian_mcp.errors`). The
per-item ``success``/``error`` fields of :func:`batch_result` are a different
thing: a batch reports partial failure in-band, because some of its items may
have succeeded.
"""

from __future__ import annotations

import copy
from typing import Any

# Stable error codes carried by every tool error result (``code``). Clients
# branch on these; the human ``message`` next to them is free text.
ERROR_CODES: tuple[str, ...] = (
    "invalid_argument",  # bad path, bad value, wrong file type, bad regex, ...
    "not_found",  # the note/file/folder does not exist
    "already_exists",  # the target path is already taken
    "permission_denied",  # outside the readable/writable part of the vault
    "permanent_delete_disabled",  # permanent delete asked, ALLOW_PERMANENT_DELETE off
    "revision_conflict",  # expected_revision no longer matches the file
    "precondition_required",  # strict mode: overwriting needs expected_revision
    "unavailable",  # transient: e.g. the vault index is still building
    "tool_error",  # a deliberate tool error with no more specific code
    "internal",  # unexpected failure; details may be masked
)

# The one output schema every tool advertises. ``data`` and ``meta`` stay
# loosely typed objects: this is the envelope contract, not per-tool payloads.
ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Result envelope shared by every tool. On an error result (isError) "
        "success is false, data is empty and code/message describe the error."
    ),
    "properties": {
        "success": {"type": "boolean"},
        "path": {
            "type": "string",
            "description": "Vault path of the single item addressed; absent for listings.",
        },
        "revision": {
            "type": "string",
            "description": "Opaque token to pass back as expected_revision.",
        },
        "data": {"type": "object", "additionalProperties": True},
        "meta": {
            "type": "object",
            "additionalProperties": True,
            "description": "count, truncated, action, dry_run, ... when there is something to say.",
        },
        "code": {
            "type": "string",
            "enum": list(ERROR_CODES),
            "description": "Error results only: stable machine-readable error code.",
        },
        "message": {
            "type": "string",
            "description": "Error results only: human-readable error message.",
        },
    },
    "required": ["success", "data"],
    "additionalProperties": True,
}


def envelope_output_schema() -> dict[str, Any]:
    """A fresh copy of :data:`ENVELOPE_SCHEMA` to hand to one tool registration."""
    return copy.deepcopy(ENVELOPE_SCHEMA)


def _envelope(
    success: bool,
    *,
    path: str | None = None,
    revision: str | None = None,
    data: Any = None,
    meta: dict | None = None,
) -> dict:
    result: dict[str, Any] = {"success": success}
    if path is not None:
        result["path"] = path
    if revision is not None:
        result["revision"] = revision
    result["data"] = {} if data is None else data
    if meta:
        result["meta"] = meta
    return result


def read_result(
    path: str | None,
    data: Any,
    revision: str | None = None,
    meta: dict | None = None,
) -> dict:
    """A successful read of one item.

    ``path`` may be ``None`` for a vault-wide read (statistics, conventions,
    a tag tree) that no single path addresses.
    """
    return _envelope(True, path=path, revision=revision, data=data, meta=meta)


def list_result(items: list, meta: dict | None = None) -> dict:
    """A successful listing. ``meta.count`` is always filled in.

    Callers add ``truncated`` (and anything else) through ``meta``; an
    explicitly passed ``count`` wins over the derived one.
    """
    return _envelope(True, data={"items": items}, meta={"count": len(items), **(meta or {})})


def write_result(
    path: str,
    action: str,
    revision: str | None = None,
    data: Any = None,
    meta: dict | None = None,
) -> dict:
    """A successful mutation of one item.

    ``action`` is the verb that was performed (``"written"``, ``"patched"``,
    ``"deleted"``, …) and is surfaced as ``meta.action``. ``revision`` is the
    committed revision, and is omitted only when the tool wrote nothing (a
    dry run) or the target no longer exists (a delete).
    """
    return _envelope(
        True,
        path=path,
        revision=revision,
        data=data,
        meta={"action": action, **(meta or {})},
    )


def batch_result(results: list[dict], summary: dict) -> dict:
    """A batch of same-shape mutations, some of which may have failed.

    ``success`` is ``True`` whenever the batch itself ran to completion — it
    is not an error flag. Per-item outcomes live in ``data.results`` (built
    with :func:`batch_item` / :func:`batch_error`) and the tallies in
    ``data.summary``.
    """
    return _envelope(True, data={"summary": summary, "results": results})


def batch_item(path: str, revision: str | None = None, data: Any = None) -> dict:
    """One successful entry inside :func:`batch_result`."""
    item: dict[str, Any] = {"success": True, "path": path}
    if revision is not None:
        item["revision"] = revision
    if data is not None:
        item["data"] = data
    return item


def batch_error(path: str, exc: BaseException) -> dict:
    """One failed entry inside :func:`batch_result`.

    ``error.type`` is the raised exception's class name, so a client can
    branch on it the same way it would on a hard MCP error.
    """
    return {
        "success": False,
        "path": path,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }


def batch_summary(results: list[dict]) -> dict:
    """Tally ``total``/``succeeded``/``failed`` over :func:`batch_result` items."""
    succeeded = sum(1 for item in results if item.get("success"))
    return {
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
    }


def error_result(code: str, message: str, **details: Any) -> dict:
    """Structured content of a tool error result.

    ``details`` adds error-specific keys (a revision conflict keeps its
    ``error``/``path``/``expected_revision``/``actual_revision``).
    """
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code {code!r}")
    return {**details, "success": False, "data": {}, "code": code, "message": message}

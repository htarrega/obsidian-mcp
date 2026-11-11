"""Tool errors: readable messages under masking, plus a stable error code.

With ``mask_error_details=True`` FastMCP turns every non-``FastMCPError``
exception raised by a tool into ``ToolError("Error calling tool 'x'")`` (the
original is kept as ``__cause__``). That hides internals such as filesystem
paths in OS errors or library tracebacks, but it would also hide the
deliberate, user-facing messages this server raises ("Write access denied
for path ...", "Note not found: ...", invalid arguments). This middleware
restores the message for those expected error types only.

It also gives every tool error a machine-readable ``code`` (see
:data:`obsidian_mcp.envelope.ERROR_CODES`). Instead of re-raising, it
returns an error ``ToolResult`` (``isError: true``) whose text content is the
same message a raised error would have produced, and whose structured content
is :func:`obsidian_mcp.envelope.error_result` — ``{success: false, data: {},
code, message, ...}``. Masked errors keep the generic message and get the
code ``internal``.
"""

from __future__ import annotations

from fastmcp.exceptions import (
    DisabledError,
    FastMCPError,
    NotFoundError,
    ToolError,
    ValidationError,
)
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

from .domain.index import IndexBuildingError
from .domain.models import PreconditionRequiredError, RevisionConflictError
from .envelope import error_result
from .storage.policy import PermanentDeleteDisabledError

# OS-generated errors for these mean "the path you asked about is wrong", which
# the caller needs to know; only the generic strerror is shown, never the
# filename the OS attached.
_EXPECTED_OS_ERRORS = (
    FileNotFoundError,
    FileExistsError,
    IsADirectoryError,
    NotADirectoryError,
    PermissionError,
)

# Most specific first: PreconditionRequiredError and
# PermanentDeleteDisabledError are PermissionErrors.
_CODES: tuple[tuple[type[BaseException] | tuple[type[BaseException], ...], str], ...] = (
    (RevisionConflictError, "revision_conflict"),
    (PreconditionRequiredError, "precondition_required"),
    (PermanentDeleteDisabledError, "permanent_delete_disabled"),
    (PermissionError, "permission_denied"),
    (FileNotFoundError, "not_found"),
    (FileExistsError, "already_exists"),
    ((IsADirectoryError, NotADirectoryError), "invalid_argument"),
    (IndexBuildingError, "unavailable"),
    ((ValueError, ValidationError), "invalid_argument"),
)


def user_facing_message(exc: BaseException) -> str | None:
    """The message to show the caller for ``exc``, or None to keep it masked.

    Our own code raises OSError subclasses with a single message argument
    (errno is None) — those are shown as is. OS-generated ones carry errno and
    a filename: an expected kind is reduced to its type and strerror, anything
    else (I/O errors, ...) stays masked.
    """
    if isinstance(exc, OSError):
        if exc.errno is None:
            return str(exc)
        if isinstance(exc, _EXPECTED_OS_ERRORS):
            return f"{type(exc).__name__}: {exc.strerror or 'error'}"
        return None
    if isinstance(exc, (ValueError, IndexBuildingError)):
        return str(exc)
    return None


def error_code(exc: BaseException | None) -> str:
    """The stable error code for ``exc`` (``internal`` when nothing matches)."""
    if exc is not None:
        for types, code in _CODES:
            if isinstance(exc, types):
                return code
    return "internal"


def _details(exc: BaseException | None) -> dict:
    """Extra structured keys an error type already defines (conflicts)."""
    if isinstance(exc, (RevisionConflictError, PreconditionRequiredError)):
        return exc.to_dict()
    return {}


def tool_error_result(code: str, message: str, cause: BaseException | None = None) -> ToolResult:
    """An MCP error result: ``message`` as text, the code as structured content."""
    return ToolResult(
        content=[TextContent(type="text", text=message)],
        structured_content=error_result(code, message, **_details(cause)),
        is_error=True,
    )


def conflict_result(exc: RevisionConflictError | PreconditionRequiredError) -> ToolResult:
    """Error result for an optimistic-concurrency failure (see _mutation_boundary).

    Keeps the historical payload (``error``, ``path``, ``expected_revision``,
    ``actual_revision``) and adds the envelope's ``success``/``data``/``code``/
    ``message``.
    """
    return tool_error_result(error_code(exc), str(exc), exc)


class UserFacingErrorMiddleware(Middleware):
    """Turns tool failures into coded error results (see module docstring).

    Register it before any other tool middleware so it also sees errors raised
    by them (e.g. vault selection). ``mask_error_details`` only governs those
    raw middleware exceptions; tool exceptions arrive already masked or not by
    FastMCP itself.
    """

    def __init__(self, mask_error_details: bool = True) -> None:
        self.mask_error_details = mask_error_details

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = getattr(context.message, "name", "?")
        prefix = f"Error calling tool {name!r}"
        try:
            return await call_next(context)
        except (NotFoundError, DisabledError):
            # Unknown or disabled tool: a protocol-level miss, not a tool error.
            raise
        except ToolError as exc:
            text = str(exc)
            cause = exc.__cause__
            wrapped = text == prefix or text.startswith(f"{prefix}: ")
            if text == prefix and cause is not None:
                # Masked by FastMCP: restore the message of expected errors.
                message = user_facing_message(cause)
                if message is not None:
                    text = f"{prefix}: {message}"
                    return tool_error_result(error_code(cause), text, cause)
                return tool_error_result("internal", text)
            code = error_code(cause)
            if not wrapped and code == "internal":
                # Raised deliberately by a tool; its message is the point.
                code = "tool_error"
            return tool_error_result(code, text, cause)
        except ValidationError as exc:
            return tool_error_result("invalid_argument", str(exc), exc)
        except FastMCPError as exc:
            return tool_error_result("tool_error", str(exc), exc)
        except Exception as exc:
            # Raised outside FastMCP's tool wrapper (another middleware).
            message = user_facing_message(exc)
            if message is None and not self.mask_error_details:
                message = str(exc)
            if message is None:
                return tool_error_result("internal", prefix)
            return tool_error_result(error_code(exc), message, exc)

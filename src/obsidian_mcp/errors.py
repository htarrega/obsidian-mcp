"""Keep user-facing tool errors readable when FastMCP error masking is on.

With ``mask_error_details=True`` FastMCP turns every non-``FastMCPError``
exception raised by a tool into ``ToolError("Error calling tool 'x'")`` (the
original is kept as ``__cause__``). That hides internals such as filesystem
paths in OS errors or library tracebacks, but it would also hide the
deliberate, user-facing messages this server raises ("Write access denied
for path ...", "Note not found: ...", invalid arguments). This middleware
restores the message for those expected error types only.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .domain.index import IndexBuildingError

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


class UserFacingErrorMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        try:
            return await call_next(context)
        except ToolError as exc:
            name = getattr(context.message, "name", "?")
            masked = str(exc) == f"Error calling tool {name!r}"
            cause = exc.__cause__
            message = user_facing_message(cause) if masked and cause is not None else None
            if message is not None:
                raise ToolError(f"Error calling tool {name!r}: {message}") from cause
            raise

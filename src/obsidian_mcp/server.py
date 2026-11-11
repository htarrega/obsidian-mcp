"""FastMCP server entry point."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import logging
import mimetypes
import os
import stat
import threading
from dataclasses import dataclass
from functools import wraps

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError
from fastmcp.server.auth import AccessToken, AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import (
    ConfigError,
    _env_bool,
    get_config,
    load_vaults_file,
    mask_error_details_from_env,
    oauth_hardening_from_env,
    reset_current_identity,
    reset_current_vault,
    set_current_identity,
    set_current_vault,
)
from .domain.index import VaultIndex
from .domain.models import PreconditionRequiredError, RevisionConflictError
from .envelope import (
    batch_item,
    batch_result,
    batch_summary,
    list_result,
    read_result,
    write_result,
)
from .errors import UserFacingErrorMiddleware
from .storage.filesystem import VaultStorage
from .storage.policy import (
    InvalidFileTypeError,
    ReadPermissionError,
    VaultAccessPolicy,
    VaultPathError,
    WritePermissionError,
)
from .storage.watcher import VaultWatcher
from .tools.attachments import (
    AttachmentTooLargeError,
    add_attachment,
    attachment_download_headers,
    create_attachment_token,
    list_attachments,
    read_attachment,
    validate_attachment_path,
    verify_attachment_token,
    write_attachment_bytes,
)
from .tools.audit import get_audit_log, log_write
from .tools.bases import list_bases, patch_base, read_base, write_base
from .tools.canvas import list_canvases, patch_canvas, read_canvas, write_canvas
from .tools.excalidraw import (
    list_excalidraw,
    patch_excalidraw,
    read_excalidraw,
    write_excalidraw,
)
from .tools.folders import (
    create_folder,
    delete_folder,
    list_files,
    list_folder,
    list_trash,
    rename_folder,
    restore_folder,
)
from .tools.kanban import (
    add_kanban_card,
    create_kanban_board,
    delete_kanban_card,
    move_kanban_card,
    read_kanban,
)
from .tools.lint import lint_schema
from .tools.prompts import daily_note_prompt, weekly_review_prompt
from .tools.query import (
    find_broken_links,
    get_backlinks,
    get_link_graph,
    get_orphans,
    get_outgoing_links,
    get_periodic_note,
    get_tag_tree,
    get_tasks_page,
    get_vault_stats,
    list_all_tags,
    query_notes,
    resolve_alias,
    resolve_note_ref,
)
from .tools.read import (
    get_note_outline,
    get_recent_notes,
    list_notes,
    read_note,
    render_note,
    search_notes,
)
from .tools.similarity import find_similar_notes
from .tools.templates import create_from_template, list_templates_info
from .tools.vault_layout import vault_conventions
from .tools.write import (
    append_to_note,
    batch_edit,
    copy_note,
    delete_note,
    find_replace_in_vault,
    manage_tags,
    move_note,
    patch_frontmatter,
    patch_frontmatter_batch,
    patch_note,
    patch_note_text,
    prepend_to_note,
    restore_note,
    set_task_status,
    write_note,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


# MCP tool annotations. Without them clients assume the spec defaults
# (destructive and open-world) for every tool. Every tool works only on the
# vault, so none is open-world.
def _hints(*, read_only: bool, destructive: bool = False, idempotent: bool) -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


_READ = _hints(read_only=True, idempotent=True)
# Changes or removes existing content; repeating it may change more.
_EDIT = _hints(read_only=False, destructive=True, idempotent=False)
# Replaces content with what is given; repeating it changes nothing more.
_OVERWRITE = _hints(read_only=False, destructive=True, idempotent=True)
# Only adds; repeating it adds again.
_ADD = _hints(read_only=False, idempotent=False)
# Only adds or sets a state; repeating it changes nothing more.
_CREATE_ONLY = _hints(read_only=False, idempotent=True)

_TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    **dict.fromkeys(
        (
            "list_notes_tool", "read_note_tool", "get_outline_tool", "get_outgoing_links_tool",
            "get_recent_notes_tool", "search_notes_tool", "find_similar_notes_tool",
            "get_backlinks_tool", "get_vault_conventions_tool", "get_audit_log_tool",
            "list_vaults_tool", "lint_schema_tool", "get_broken_links_tool", "get_orphans_tool",
            "get_link_graph_tool", "get_vault_stats_tool", "list_all_tags_tool", "get_tasks_tool",
            "get_periodic_note_tool", "resolve_alias_tool", "query_notes_tool",
            "list_attachments_tool", "read_attachment_tool", "list_templates_tool",
            "list_folder_tool", "list_files_tool", "list_trash_tool", "list_canvases_tool",
            "read_canvas_tool", "list_excalidraw_tool", "read_excalidraw_tool",
            "read_kanban_tool", "list_bases_tool", "read_base_tool",
        ),
        _READ,
    ),
    **dict.fromkeys(
        (
            "patch_note_tool", "patch_note_text_tool", "batch_edit_tool", "move_note_tool",
            "find_replace_in_vault_tool", "delete_tool", "rename_folder_tool",
            "patch_canvas_tool", "patch_excalidraw_tool", "patch_base_tool",
            "move_kanban_card_tool", "delete_kanban_card_tool",
        ),
        _EDIT,
    ),
    **dict.fromkeys(
        (
            "write_note_tool", "patch_frontmatter_tool", "manage_tags_tool",
            "create_from_template_tool", "add_attachment_tool", "write_canvas_tool",
            "write_excalidraw_tool", "write_base_tool", "create_kanban_board_tool",
        ),
        _OVERWRITE,
    ),
    **dict.fromkeys(
        (
            "append_to_note_tool", "prepend_to_note_tool", "add_kanban_card_tool",
            "restore_tool", "create_attachment_token_tool",
        ),
        _ADD,
    ),
    **dict.fromkeys(
        ("copy_note_tool", "create_folder_tool", "set_task_status_tool"), _CREATE_ONLY
    ),
}


def _tool(fn):
    """Register ``fn`` as a tool with its annotations from _TOOL_ANNOTATIONS
    (a missing entry fails at import, so no tool ships without hints)."""
    return mcp.tool(annotations=_TOOL_ANNOTATIONS[fn.__name__])(fn)


def _mutation_boundary(function):
    """Return expected optimistic-concurrency failures as MCP error results."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (RevisionConflictError, PreconditionRequiredError) as exc:
            return ToolResult(structured_content=exc.to_dict(), is_error=True)

    return wrapped


def _read_envelope(path: str | None, result: dict, meta: dict | None = None) -> dict:
    """Lift a tool-layer read dict into the canonical read envelope.

    The payload's own ``path``/``revision`` keys are promoted to the envelope
    top level rather than duplicated inside ``data``.
    """
    payload = dict(result)
    payload.pop("path", None)
    revision = payload.pop("revision", None)
    return read_result(path, payload, revision=revision, meta=meta)


def _write_envelope(
    result: dict,
    *,
    path_key: str = "path",
    action: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Lift a tool-layer mutation dict into the canonical write envelope.

    The tool layer's legacy ``status`` string becomes ``meta.action``; a
    ``dry_run`` status additionally sets ``meta.dry_run`` so callers can test
    one flag instead of comparing verbs.
    """
    payload = dict(result)
    path = payload.pop(path_key, None)
    verb = action or payload.pop("status", None) or "written"
    payload.pop("status", None)
    revision = payload.pop("revision", None)
    extra = dict(meta or {})
    if verb == "dry_run":
        extra["dry_run"] = True
    return write_result(path, verb, revision=revision, data=payload, meta=extra)


_DEFAULT_INSTRUCTIONS = """\
You are connected to obsidian-mcp, a server for one or more Obsidian vaults.

## Start here
1. Call `get_vault_conventions_tool()` and follow the vault's own conventions
   (folder layout, naming, where new notes go, how notes are linked). With
   `source: "file"` they come from the vault's conventions file; with
   `source: "auto-generated"` they are a summary derived from the vault, so
   look at a few existing notes before writing. Do not introduce structure
   the vault does not already use (frontmatter, tags, headings, templates).
2. If `list_vaults_tool()` returns more than one vault, pick the one the user
   means and pass `vault=<name>` on every call (there is no server-side
   memory of the choice). With a single vault, ignore `vault`.
3. Search (`search_notes_tool`) before creating a note, to avoid duplicates.

Notes are addressed by vault path (`Folder/Note.md`); read-only note tools
also accept a bare note name and resolve it like a [[wikilink]]. Obsidian
shows the file name as the title, so don't start a note with a `# Heading`
that repeats it.

## Results
Every tool returns `{success, path?, revision?, data, meta?}`. Listings put
rows in `data.items`; `meta` carries counts, truncation and, on writes,
`action`/`dry_run`. Real failures (bad path, permission denied, revision
conflict) come back as MCP errors.

## Writing safely
- Reads return a `revision`. Pass it as `expected_revision` to pin a write to
  the bytes you read. On a `revision_conflict`, read again before retrying;
  a blind retry of an append or prepend can add the text twice.
- If the server requires it (`precondition_required`), overwriting an
  existing note needs that `expected_revision`; creating a new note does not.
- Tools with `dry_run` preview into `data.preview`/`data.diff` and write
  nothing; check, then call again with `dry_run=false`.
- A write denied by the server's policy (read-only mode, protected folders)
  is final: don't work around it by writing somewhere else unless the user
  asks.
- Address one task with `set_task_status_tool(path, line, done,
  expected_text)` using the `line` and `text` from `get_tasks_tool` or
  `read_note_tool`; it refuses stale lines.

Tools that are not in your tool list are disabled by the operator; that is
expected, not an error.
"""


def _layout_instructions(layout) -> str:
    """A short note on periodic notes/templates, only when the operator
    configured them for this vault (the defaults are not advertised)."""
    periods = [
        period
        for period, pattern in layout.periodic
        if layout.is_configured(f"periodic.{period}")
    ]
    lines = []
    if periods:
        patterns = ", ".join(
            f"{period}: `{layout.periodic_pattern(period)}`" for period in periods
        )
        lines.append(
            f"- Periodic notes ({patterns}): read them with "
            "`get_periodic_note_tool(period, date)`; a missing one returns "
            "`exists: false` and a `suggested_path`. Don't create one unless asked."
        )
    if layout.is_configured("templates_dir"):
        lines.append(
            f"- Templates live in `{layout.templates_dir}/`: "
            "`list_templates_tool()`, `create_from_template_tool(...)`."
        )
    if not lines:
        return ""
    return "\n\n## Periodic notes and templates\n" + "\n".join(lines) + "\n"


def _load_instructions() -> str:
    try:
        cfg = get_config()
    except ConfigError:
        # Importing the module before the process environment is configured
        # is supported (tests, tooling, and FastMCP discovery).
        return _DEFAULT_INSTRUCTIONS
    if cfg.multi_vault:
        # FastMCP exposes server instructions before request middleware has
        # authenticated an identity and selected one of its allowed vaults.
        # Per-vault conventions remain available through the authorized tool.
        return _DEFAULT_INSTRUCTIONS
    instructions = _DEFAULT_INSTRUCTIONS + _layout_instructions(cfg.layout)
    try:
        conventions_path = cfg.layout.conventions_path
        vault = VaultStorage.from_config(cfg).read_text(conventions_path)
    except (FileNotFoundError, PermissionError, VaultPathError, OSError):
        # Missing or unreadable optional instructions do not prevent startup;
        # descriptor/policy failures are not converted into unrestricted I/O.
        return instructions
    return (
        instructions
        + f"\n\n# Vault-specific instructions ({conventions_path})\n\n"
        + vault
    )


class _APIKeyAuthProvider(TokenVerifier):
    """Static API-key auth, one or more keys. Clients must send:
    Authorization: Bearer <key>. client_id on the returned AccessToken is
    the matched key itself — in multi-vault mode that's what
    VaultResolutionMiddleware looks up in the identities table (single-key
    mode has no such lookup, so the exact client_id value doesn't matter
    there beyond being stable)."""

    def __init__(self, api_keys: list[str]) -> None:
        super().__init__()
        self._keys = api_keys

    async def verify_token(self, token: str) -> AccessToken | None:
        for key in self._keys:
            if hmac.compare_digest(token, key):
                return AccessToken(token=token, client_id=key, scopes=[])
        logger.warning("Rejected request with invalid API key")
        return None


class _RestrictedGitHubVerifier(TokenVerifier):
    """Wraps GitHubProvider's token validator to reject logins not on an allowlist.

    Runs on every authenticated request: OAuthProxy.load_access_token()
    swaps the FastMCP JWT for the upstream GitHub token and re-validates it
    through this verifier each time (the wrapped GitHubTokenVerifier caches
    GitHub's answer for cache_ttl_seconds=300, so the allowlist check itself
    is cheap). GitHubProvider has no concept of restricting which GitHub
    account may authenticate, so any account could otherwise get full vault
    access.
    """

    def __init__(self, base: TokenVerifier, allowed_logins: list[str]) -> None:
        super().__init__()
        self._base = base
        self._allowed_logins = set(allowed_logins)

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await self._base.verify_token(token)
        if result is None:
            return None
        login = str((result.claims or {}).get("login", "")).lower()
        if login not in self._allowed_logins:
            logger.warning("Rejected GitHub login not on allowlist: %s", login or "<unknown>")
            return None
        return result


def _identities_from_env() -> tuple[list[str], list[str]]:
    """(api_keys, allowed_github_logins) — from VAULTS_CONFIG if set, else
    the legacy single-vault env vars. Reads os.environ directly (not
    get_config()) so this module can still be imported without VAULT_PATH
    set (e.g. during testing or linting); a broken VAULTS_CONFIG here is
    swallowed (falls back to empty — no auth configured) rather than
    raised, since Config.__init__ is the actual place that validates it and
    fails loudly at real startup. Don't let a parse error here silently
    grant unauthenticated access, though — an empty result just means no
    verifier gets built at all, so the server refuses to start over the
    network per the has_api_keys/oauth_configured check in Config.__init__."""
    vaults_config_path = os.environ.get("VAULTS_CONFIG", "")
    if vaults_config_path:
        try:
            _vaults, identities = load_vaults_file(vaults_config_path)
        except ConfigError:
            return [], []
        api_keys = [i.value for i in identities if i.type == "api_key"]
        allowed_logins = [i.value for i in identities if i.type == "github_login"]
        return api_keys, allowed_logins

    key = os.environ.get("API_KEY") or os.environ.get("OBSIDIAN_MCP_API_KEY")
    api_keys = [key] if key else []
    allowed_logins = [
        login.strip().lower()
        for login in os.environ.get("OAUTH_GITHUB_ALLOWED_LOGINS", "").split(",")
        if login.strip()
    ]
    return api_keys, allowed_logins


def _build_auth() -> AuthProvider | None:
    api_keys, allowed_logins = _identities_from_env()
    api_key_verifier = _APIKeyAuthProvider(api_keys) if api_keys else None

    client_id = os.environ.get("OAUTH_GITHUB_CLIENT_ID")
    client_secret = os.environ.get("OAUTH_GITHUB_CLIENT_SECRET")
    github_provider = None
    if client_id and client_secret:
        base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

        # Client registrations + encrypted tokens persist under FastMCP's own
        # data directory (FASTMCP_HOME, defaults to a platformdirs path).
        # Mount that directory as a volume in Docker, or set FASTMCP_HOME to a
        # path inside an existing mount, or logins won't survive a restart.
        hardening = oauth_hardening_from_env()
        github_provider = GitHubProvider(
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url,
            # Otherwise every MCP request waits on GitHub /user and /user/repos.
            cache_ttl_seconds=300,
            # Least privilege: only the login is needed (library default is
            # "user", which also grants profile write access).
            required_scopes=list(hardening.github_scopes),
            # Only known MCP client callbacks may receive authorization codes
            # (library default None = any redirect URI is accepted via DCR).
            allowed_client_redirect_uris=list(hardening.allowed_client_redirect_uris),
            # URL-based client IDs (CIMD) make the server fetch client-chosen URLs.
            enable_cimd=hardening.enable_cimd,
            # GitHub OAuth-app tokens carry no expiry, so the issued FastMCP JWT
            # would otherwise live 1 year; this caps it.
            fastmcp_access_token_expiry_seconds=hardening.access_token_expiry_seconds,
            # GitHub returns no refresh_expires_in, so FastMCP-issued refresh
            # tokens would otherwise live 1 year (library default).
            fallback_refresh_token_expiry_seconds=hardening.refresh_token_expiry_seconds,
            require_authorization_consent=True,
        )
        # Relies on OAuthProxy's private _token_validator attribute — the hook
        # load_access_token() uses to validate the upstream token on every
        # request. May break on a fastmcp upgrade; if it does,
        # this will raise AttributeError loudly at startup rather than
        # silently allowing any GitHub account through.
        github_provider._token_validator = _RestrictedGitHubVerifier(
            github_provider._token_validator, allowed_logins
        )
        logger.info("GitHub OAuth enabled (allowed logins: %s)", ", ".join(allowed_logins))

    if github_provider and api_key_verifier:
        logger.info("API key auth enabled (alongside GitHub OAuth)")
        # required_scopes must be cleared here: MultiAuth defaults it to the
        # server's (GitHubProvider's, i.e. OAUTH_GITHUB_SCOPES) and enforces it across
        # every verifier. _APIKeyAuthProvider's tokens carry scopes=[] since
        # a static key has no OAuth scopes, so without this override every
        # API-key request would fail with "insufficient_scope" even though
        # the key itself checked out.
        return MultiAuth(server=github_provider, verifiers=[api_key_verifier], required_scopes=[])
    if github_provider:
        return github_provider
    if api_key_verifier:
        logger.info("API key auth enabled")
        return api_key_verifier
    return None


class _CurrentVaultIndex:
    """Every existing @mcp.tool()/@mcp.resource() call site passes/uses
    `_index` as a single VaultIndex — this proxy lets that keep working
    completely unchanged even though there's now one VaultIndex per
    configured vault. Attribute access is forwarded to whichever vault's
    index the current context resolves to (contextvars, set per tool call
    by VaultResolutionMiddleware in multi-vault mode; the single configured
    vault otherwise)."""

    def __getattr__(self, name: str):
        vault_name = get_config().resolve_vault_name()
        return getattr(_indices[vault_name], name)


def _identity_for_token(cfg, access_token: AccessToken | None) -> object:
    """Find the Identity (config.Identity) matching a given AccessToken —
    GitHub login if the token carries one (OAuth), else the API key itself
    (client_id, see _APIKeyAuthProvider). Raises PermissionError if there's
    no token or no VAULTS_CONFIG entry for it — never silently falls back
    to some default vault for an identity nothing was configured for.

    Split out from _resolve_identity() (which reads the token from FastMCP's
    request context via get_access_token()) so callers that already have an
    AccessToken in hand from doing their own manual verification — the
    /attachments/* custom route, which bypasses FastMCP's normal auth
    middleware entirely — can resolve an identity too, without needing
    get_access_token() to work in a context it doesn't cover.
    """
    if access_token is None:
        raise PermissionError("No authenticated identity available to resolve a vault for")

    login = str((access_token.claims or {}).get("login", "")).lower()
    candidates = [("github_login", login)] if login else []
    candidates.append(("api_key", access_token.client_id))

    for itype, value in candidates:
        if not value:
            continue
        for identity in cfg.identities:
            if identity.type == itype and identity.value == value:
                return identity

    raise PermissionError("This identity has no vault access configured in VAULTS_CONFIG")


def _resolve_identity(cfg) -> object:
    """_identity_for_token() using the current request's AccessToken from
    FastMCP's own context (get_access_token()) — the normal case for
    everything that goes through MCP tool-call/resource-read dispatch."""
    return _identity_for_token(cfg, get_access_token())


def _select_vault(identity, requested: str | None) -> str:
    """Which vault an identity's call actually resolves to.

    requested=None (no vault= argument given): the identity's configured
    "default" — every identity has one automatically if it only has a
    single allowed vault (see load_vaults_file); an identity with several
    allowed vaults and no explicit "default" in VAULTS_CONFIG must pass
    vault= explicitly, every single call, rather than have the server
    silently guess which one it means.
    requested=<name>: must be one of the identity's allowed vaults.
    """
    if requested is None:
        if identity.default is None:
            raise PermissionError(
                f"This identity has access to multiple vaults ({identity.vaults}) and no "
                "default is configured — pass vault=<name> explicitly on this call"
            )
        return identity.default
    if requested not in identity.vaults:
        raise PermissionError(f"This identity does not have access to vault {requested!r}")
    return requested


class VaultResolutionMiddleware(Middleware):
    """In multi-vault mode, resolves which vault a tool call/resource read
    operates on and sets the current-vault contextvar for its duration —
    every tools/*.py function then transparently sees the right
    get_config().vault_path/write_paths/exclude_paths/read_only and the
    right VaultIndex (via _CurrentVaultIndex above), without any of them
    needing to know multi-vault exists. A no-op pass-through in
    single-vault mode (VAULTS_CONFIG unset).

    Every @mcp.tool() has an optional `vault: str | None = None` parameter
    (mechanical addition, not used by the tool bodies themselves — this
    middleware is the only thing that reads it, from the raw call
    arguments, before the tool function ever runs). Omit it to use the
    calling identity's default vault; pass it to operate on a different one
    of that identity's allowed vaults for just this one call. See
    list_vaults_tool for discovering which vaults + which one is default
    for the current identity. Resource reads (on_read_resource) have no
    such parameter to read (ReadResourceRequestParams carries a uri, not
    tool-style arguments) and always use the identity's default.

    Not covered here: /health doesn't go through MCP tool-call dispatch, so
    this middleware never runs for it — it stays scoped to
    Config.default_vault_name regardless of which identity is calling (it
    exposes no vault content, just liveness, so this doesn't leak anything).
    /attachments/* is also a custom route rather than tool-call dispatch,
    but does its own equivalent vault resolution by hand — see
    attachment_route below — since it's the one HTTP route that genuinely
    needs it.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        cfg = get_config()
        if not cfg.multi_vault:
            return await call_next(context)

        identity = _resolve_identity(cfg)
        if getattr(context.message, "name", None) == "list_vaults_tool":
            # This identity-only discovery call does not touch vault content.
            # In particular, it must work for identities intentionally
            # configured with several vaults and no default.
            return await call_next(context)
        requested = None
        arguments = getattr(context.message, "arguments", None)
        if arguments:
            requested = arguments.get("vault")
        vault_name = _select_vault(identity, requested)

        token = set_current_vault(vault_name)
        identity_token = set_current_identity(identity)
        try:
            return await call_next(context)
        finally:
            reset_current_identity(identity_token)
            reset_current_vault(token)

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        cfg = get_config()
        if not cfg.multi_vault:
            return await call_next(context)

        identity = _resolve_identity(cfg)
        vault_name = _select_vault(identity, None)

        token = set_current_vault(vault_name)
        identity_token = set_current_identity(identity)
        try:
            return await call_next(context)
        finally:
            reset_current_identity(identity_token)
            reset_current_vault(token)


# Initialized in main() — empty at import time so the module can be imported
# without VAULT_PATH/VAULTS_CONFIG set (e.g. during testing or linting).
_cfg = None
_indices: dict[str, VaultIndex] = {}
_watchers: dict[str, VaultWatcher] = {}
_index = _CurrentVaultIndex()

mcp = FastMCP(
    name="obsidian-mcp",
    instructions=_load_instructions(),
    auth=_build_auth(),
    mask_error_details=mask_error_details_from_env(),
)
mcp.add_middleware(VaultResolutionMiddleware())
mcp.add_middleware(UserFacingErrorMiddleware())


# Gates which optional plugin-format and high-impact mutation tool groups get
# registered below, so disabled tools never appear in the client's tool list.
# Deliberately not the real Config object (that needs VAULT_PATH,
# see _build_auth() above) — just the feature flags, read straight from the
# environment.
@dataclass(frozen=True)
class _FeatureFlags:
    enable_canvas: bool
    enable_excalidraw: bool
    enable_kanban: bool
    enable_bases: bool
    enable_move: bool
    enable_folder_rename: bool
    enable_bulk_replace: bool
    enable_delete: bool

    @classmethod
    def from_env(cls) -> _FeatureFlags:
        """Read named flags without constructing the full vault config."""
        def enabled(name: str) -> bool:
            return os.environ.get(name, "false").lower() in ("1", "true", "yes")

        return cls(
            enable_canvas=enabled("ENABLE_CANVAS"),
            enable_excalidraw=enabled("ENABLE_EXCALIDRAW"),
            enable_kanban=enabled("ENABLE_KANBAN"),
            enable_bases=enabled("ENABLE_BASES"),
            enable_move=enabled("ENABLE_MOVE"),
            enable_folder_rename=enabled("ENABLE_FOLDER_RENAME"),
            enable_bulk_replace=enabled("ENABLE_BULK_REPLACE"),
            enable_delete=enabled("ENABLE_DELETE"),
        )


_feature_flags = _FeatureFlags.from_env()

# Optional Obsidian Headless (`ob`) sync tools — see obsidian_mcp/ob. The
# package is imported only when ENABLE_OB is set; otherwise nothing changes.
# Same rule as obsidian_mcp.ob.settings.ObFlags (whitespace-tolerant), kept
# inline so the ob package is not imported when the flag is off.
_ob_enabled = os.environ.get("ENABLE_OB", "false").strip().lower() in ("1", "true", "yes")
if _ob_enabled:
    from .ob import register_ob_tools

    register_ob_tools(mcp)


# ── Prompts ───────────────────────────────────────────────────────────────────

@mcp.prompt()
def weekly_review() -> str:
    """Summarize the past week: overdue/due-soon tasks, daily note highlights."""
    return weekly_review_prompt()


@mcp.prompt()
def daily_note(date: str = "today") -> str:
    """Open or create a daily note, carrying over yesterday's open tasks.
    date: 'today' | 'yesterday' | 'YYYY-MM-DD'."""
    return daily_note_prompt(date=date)


# ── Read ──────────────────────────────────────────────────────────────────────

@_tool
def list_notes_tool(folder: str = "", include_meta: bool = False, vault: str | None = None) -> dict:
    """List all Markdown notes in the vault (or a subfolder).
    Set include_meta=True to get title, tags, status, created per note.
    data.items is a list of paths, or of {path, title, tags, status, created,
    created_source, mtime} when include_meta=True (created falls back to file
    birth time/mtime; created_source: 'frontmatter'|'birthtime'|'mtime')."""
    return list_result(list_notes(folder, include_meta=include_meta))


@_tool
def read_note_tool(
    path: str,
    mode: str = "full",
    depth: int = 1,
    vault: str | None = None,
) -> dict:
    """Read one note. `path` is a vault path ('Folder/Note.md') or a bare
    note name ('Note', case-insensitive, resolved like a [[wikilink]]).

    mode: 'full' (default) — data: {content, frontmatter, tags, aliases,
    wikilinks, block_refs, callouts, tasks, inline_fields} | 'outline' —
    structure only (headings, block_refs, frontmatter_keys, word/line counts)
    | 'rendered' — data.rendered with ![[embeds]] inlined (depth 0-2).

    The envelope `path` is the resolved note; meta.resolved_from/candidates
    appear when a name was resolved. `revision` (full/outline) is what to
    pass as expected_revision when writing."""
    if mode not in ("full", "outline", "rendered"):
        raise ValueError(f"Unknown mode: {mode!r}; expected 'full', 'outline' or 'rendered'")
    resolved, meta = _resolve_note(path)
    meta["mode"] = mode
    if mode == "full":
        return _read_envelope(resolved, read_note(resolved), meta=meta)
    if mode == "outline":
        return _read_envelope(resolved, get_note_outline(resolved), meta=meta)
    meta["depth"] = depth
    return read_result(resolved, {"rendered": render_note(resolved, depth=depth)}, meta=meta)


def _resolve_note(ref: str) -> tuple[str, dict]:
    """resolve_note_ref() plus the meta keys that say a name was resolved."""
    resolved, others = resolve_note_ref(ref, _index)
    meta: dict = {}
    if resolved != ref.strip():
        meta["resolved_from"] = ref
    if others:
        meta["candidates"] = others
    return resolved, meta


@_tool
def get_outline_tool(path: str, vault: str | None = None) -> dict:
    """Headings of one note: data.items [{level, text, line}] (1-based file
    lines; headings inside code blocks are ignored). `path` may be a note
    name, like read_note_tool. Cheaper than reading the whole note."""
    resolved, meta = _resolve_note(path)
    outline = get_note_outline(resolved)
    return read_result(
        resolved,
        {"items": outline["headings"]},
        revision=outline["revision"],
        meta={**meta, "count": len(outline["headings"])},
    )


@_tool
def get_outgoing_links_tool(path: str, vault: str | None = None) -> dict:
    """Links going out of one note (wikilinks and ![[embeds]]), resolved like
    Obsidian does. data.items: [{target, heading, embed, kind:
    'note'|'attachment', resolved: path|null, count}]; resolved=null means
    broken. meta: {count, resolved, unresolved}. `path` may be a note name."""
    resolved, meta = _resolve_note(path)
    items = get_outgoing_links(resolved, _index)
    broken = sum(1 for item in items if item["resolved"] is None)
    return read_result(
        resolved,
        {"items": items},
        meta={**meta, "count": len(items), "resolved": len(items) - broken, "unresolved": broken},
    )


@_tool
def get_recent_notes_tool(limit: int = 20, folder: str = "", vault: str | None = None) -> dict:
    """Most recently modified notes first. data.items: [{path, mtime}] (ISO
    8601 UTC). folder limits to one folder; limit 1-200 (default 20)."""
    items = get_recent_notes(limit=limit, folder=folder)
    return list_result(items, meta={"folder": folder} if folder else None)


@_tool
def search_notes_tool(
    query: str,
    tag: str | None = None,
    mode: str = "exact",
    limit: int = 20,
    frontmatter_filter: dict | None = None,
    field: str | None = None,
    threshold: float = 0.8,
    max_snippets_per_note: int = 2,
    context_lines: int = 1,
    vault: str | None = None,
) -> dict:
    """Full-text search with snippets and relevance ranking.
    mode: 'exact' (default) | 'regex' | 'fuzzy'. exact/fuzzy ignore case and
    accents ("Siplan" finds "Siplán").
    data.items: [{path, score, snippets: [{line, context: [{line, text, match}]}], tags}].
    max_snippets_per_note (1-10, default 2), context_lines around each match
    (0-5, default 1); overlapping windows are merged, long lines clipped.
    frontmatter_filter: combine with the text search in one call — same shape
    as query_notes_tool's (plain value = exact match, or {"$ne": v} /
    {"$nin": [...]} / {"$exists": bool}).
    field: None/'body' (default, search note content) | 'filename' (match
    only the file name).
    threshold: fuzzy-match similarity cutoff 0-1 (only used when mode='fuzzy';
    lower = looser matches, higher = less noise)."""
    items = search_notes(
        query, tag=tag, mode=mode, limit=limit,
        frontmatter_filter=frontmatter_filter, field=field, threshold=threshold,
        max_snippets_per_note=max_snippets_per_note, context_lines=context_lines,
    )
    return list_result(items, meta={"truncated": len(items) >= limit})


@_tool
def find_similar_notes_tool(
    text: str,
    limit: int = 5,
    exclude_path: str | None = None,
    min_score: float = 0.1,
    vault: str | None = None,
) -> dict:
    """Notes similar to `text` (TF-IDF over the vault's vocabulary), e.g. to
    spot a duplicate before creating a note. exclude_path skips one note;
    min_score 0-1. data.items: [{path, score}], most similar first."""
    return list_result(
        find_similar_notes(text, _index, limit=limit, exclude_path=exclude_path, min_score=min_score)
    )


# ── Write ─────────────────────────────────────────────────────────────────────

@_tool
@_mutation_boundary
def write_note_tool(
    path: str,
    content: str,
    dry_run: bool = False,
    expected_revision: str | None = None,
    create_only: bool = False,
    vault: str | None = None,
) -> dict:
    """Create or overwrite a note with `content`. Overwriting may require
    expected_revision (from a read); create_only=True refuses to overwrite.
    Existing frontmatter is kept if `content` has none
    (data.frontmatter_preserved). data.diff is the unified diff;
    dry_run=True previews without writing."""
    result = write_note(
        path,
        content,
        index=_index,
        dry_run=dry_run,
        expected_revision=expected_revision,
        create_only=create_only,
    )
    if result.get("status") != "dry_run":
        log_write("write_note_tool", path, "wrote note")
    return _write_envelope(result)


@mcp.tool(title="Edit note section", annotations=_EDIT)
@_mutation_boundary
def patch_note_tool(
    path: str,
    section: str,
    new_content: str,
    mode: str = "replace",
    target_type: str = "heading",
    dry_run: bool = False,
    expected_revision: str | None = None,
    vault: str | None = None,
) -> dict:
    """Edit one section of a note, addressed by its heading instead of exact
    text. section: the heading text, exact and case-sensitive ('Ideas' or
    '## Ideas'); the section runs to the next heading of the same or higher
    level. Fails if the heading is missing.
    mode: 'replace' (default, keeps the heading) | 'insert_before' |
    'insert_after' (right under the heading) | 'append' (end of the section).
    target_type: 'heading' (default) | 'block_ref' (section='^block-id').
    expected_revision pins the write to a read. dry_run=True previews
    data {preview, diff} without writing."""
    result = patch_note(
        path,
        section,
        new_content,
        mode=mode,
        target_type=target_type,
        index=_index,
        dry_run=dry_run,
        expected_revision=expected_revision,
    )
    if result.get("status") != "dry_run":
        log_write("patch_note_tool", path, f"patched section {section!r} ({mode})")
    return _write_envelope(result)


@_tool
@_mutation_boundary
def patch_note_text_tool(
    path: str,
    find: str,
    replace: str,
    mode: str = "exact",
    count: int = 1,
    dry_run: bool = False,
    expected_revision: str | None = None,
    vault: str | None = None,
) -> dict:
    """Find and replace text in one note — the cheapest targeted edit.
    mode: 'exact' (default) | 'regex'. count: max replacements (default 1;
    0 = all). Fails if `find` does not match. dry_run=True previews
    {replacements, preview, diff} without writing."""
    result = patch_note_text(
        path,
        find,
        replace,
        mode=mode,
        count=count,
        dry_run=dry_run,
        index=_index,
        expected_revision=expected_revision,
    )
    if result.get("status") != "dry_run":
        log_write("patch_note_text_tool", path, f"replaced {result.get('replacements')} match(es)")
    return _write_envelope(result)


def _delete_to_trash_by_default() -> bool:
    """DELETE_MODE: 'trash' (default) moves deleted items to .trash/;
    'permanent' deletes them outright (needs ALLOW_PERMANENT_DELETE)."""
    mode = os.environ.get("DELETE_MODE", "trash").strip().lower() or "trash"
    if mode not in ("trash", "permanent"):
        raise ValueError(f"DELETE_MODE must be 'trash' or 'permanent', not {mode!r}")
    return mode == "trash"


@_mutation_boundary
def delete_tool(
    path: str,
    trash: bool | None = None,
    expected_revision: str | None = None,
    vault: str | None = None,
) -> dict:
    """Delete a note or a folder — whichever `path` resolves to.

    trash omitted: the server's DELETE_MODE decides (permanent deletion, or
    a move to .trash/). trash=True moves it to .trash/ (recover it with
    restore_tool; list_trash_tool has the names); trash=False deletes it
    permanently. Deleting a folder takes its whole subtree with it.

    expected_revision pins a note's bytes so a concurrent edit isn't
    discarded, and is therefore only valid for a note; a folder has no single
    revision. data: {trash} plus meta.kind ('note' or 'folder')."""
    if trash is None:
        trash = _delete_to_trash_by_default()
    storage = VaultStorage.from_config()
    target = storage.resolve_delete(path, permanent=not trash)
    if not storage.exists(target.relative, read=False):
        raise FileNotFoundError(f"Path not found: {path!r}")

    if stat.S_ISDIR(storage.stat(target.relative, read=False).st_mode):
        if expected_revision is not None:
            raise ValueError(
                "expected_revision pins one note's bytes; a folder has no single "
                f"revision, and {path!r} is a folder"
            )
        result = delete_folder(path, trash=trash)
        kind = "folder"
    else:
        result = delete_note(
            path, trash=trash, index=_index, expected_revision=expected_revision
        )
        kind = "note"

    log_write("delete_tool", target.relative, f"deleted {kind} (trash={trash})")
    return _write_envelope(result, meta={"kind": kind})


@_mutation_boundary
def restore_tool(trashed_name: str, to_path: str, vault: str | None = None) -> dict:
    """Restore a note or a folder from .trash/ — whichever `trashed_name` is.

    trashed_name: the bare name as it sits under .trash/ (see list_trash_tool);
    a collision at delete time may have appended a random suffix, so it's
    often not the original name.
    to_path: where to put it back. The original location isn't recoverable
    from the trash entry alone, so you choose the destination.

    data: {from} plus notes_restored for a folder; meta.kind is 'note' or
    'folder'."""
    storage = VaultStorage.from_config()
    if storage.trash_info(trashed_name).is_dir:
        result = restore_folder(trashed_name, to_path, index=_index)
        kind = "folder"
    else:
        result = restore_note(trashed_name, to_path, index=_index)
        kind = "note"

    log_write("restore_tool", to_path, f"restored {kind} from .trash/{trashed_name}")
    return _write_envelope(
        result,
        path_key="path" if kind == "folder" else "to",
        meta={"kind": kind},
    )


if _feature_flags.enable_delete:
    _tool(delete_tool)
    _tool(restore_tool)


def find_replace_in_vault_tool(
    search: str,
    replace: str,
    mode: str = "exact",
    folder: str = "",
    dry_run: bool = True,
    vault: str | None = None,
) -> dict:
    """Find and replace text across every note in the vault (or a subfolder).
    mode: 'exact' (default, literal substring) | 'regex'.
    dry_run=True (default) only previews matches.
    Always run once with dry_run=True first, then dry_run=False to actually write.
    .trash/ and EXCLUDE_PATHS are always skipped; write-protected files
    (READ_ONLY or outside WRITE_PATHS) are skipped and listed under
    data.summary.skipped_write_protected rather than aborting the whole run.
    This is a batch result: data.results holds one item per affected note
    (with {match_count, preview} on a dry run) and data.summary the tallies
    ({total, succeeded, failed, dry_run, total_matches | total_replacements,
    skipped_write_protected})."""
    result = find_replace_in_vault(search, replace, mode=mode, folder=folder, dry_run=dry_run, index=_index)
    if result.get("dry_run"):
        items = [
            batch_item(
                match["path"],
                data={"match_count": match["match_count"], "preview": match["preview"]},
            )
            for match in result.get("matches", [])
        ]
        summary = {
            **batch_summary(items),
            "dry_run": True,
            "total_matches": result.get("total_matches", 0),
            "skipped_write_protected": result.get("skipped_write_protected", []),
        }
        return batch_result(items, summary)

    log_write(
        "find_replace_in_vault_tool",
        folder or None,
        f"replaced in {len(result.get('replaced_in', []))} note(s)",
    )
    items = [batch_item(path) for path in result.get("replaced_in", [])]
    summary = {
        **batch_summary(items),
        "dry_run": False,
        "total_replacements": result.get("total_replacements", 0),
        "skipped_write_protected": result.get("skipped_write_protected", []),
    }
    return batch_result(items, summary)


if _feature_flags.enable_bulk_replace:
    _tool(find_replace_in_vault_tool)


@_tool
@_mutation_boundary
def append_to_note_tool(
    path: str,
    content: str,
    section: str | None = None,
    create: bool = True,
    expected_revision: str | None = None,
    vault: str | None = None,
) -> dict:
    """Append content to a note without reading and rewriting the whole file.
    section: optional heading to append under. create=True creates the note if missing."""
    result = append_to_note(
        path,
        content,
        section=section,
        create=create,
        index=_index,
        expected_revision=expected_revision,
    )
    log_write("append_to_note_tool", path, "appended content" + (f" under {section!r}" if section else ""))
    return _write_envelope(result)


@_tool
@_mutation_boundary
def prepend_to_note_tool(
    path: str,
    content: str,
    create: bool = True,
    expected_revision: str | None = None,
    vault: str | None = None,
) -> dict:
    """Insert content at the top of a note's body — e.g. a new dated section
    on a newest-on-top log. Goes after frontmatter (if any) and after a
    leading '# Title' line (if any); otherwise at line 1. Content is inserted
    verbatim (a trailing newline is added if missing). create=True creates
    the note if missing."""
    result = prepend_to_note(
        path, content, create=create, index=_index, expected_revision=expected_revision
    )
    log_write("prepend_to_note_tool", result["path"], "prepended content")
    return _write_envelope(result)


@_tool
@_mutation_boundary
def set_task_status_tool(
    path: str,
    line: int,
    done: bool,
    expected_text: str,
    expected_revision: str | None = None,
    vault: str | None = None,
) -> dict:
    """Check (done=true) or uncheck one task, addressed by its 1-based file
    `line` as returned by get_tasks_tool/read_note_tool. expected_text is the
    task's text; the call is refused if that line is not a task or its text
    differs (stale line number), so re-read and retry. Only the checkbox
    changes. data: {line, text (updated line), done}; meta.action is
    'task_updated' or 'unchanged'."""
    result = set_task_status(
        path,
        line,
        done,
        expected_text,
        index=_index,
        expected_revision=expected_revision,
    )
    if result["status"] != "unchanged":
        log_write(
            "set_task_status_tool",
            result["path"],
            f"line {line} marked {'done' if done else 'open'}",
        )
    return _write_envelope(result)


@_tool
@_mutation_boundary
def patch_frontmatter_tool(
    path: str | None = None,
    updates: dict | None = None,
    merge_arrays: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
    paths: list[str] | None = None,
    vault: str | None = None,
) -> dict:
    """Update specific YAML frontmatter keys without touching the note body.

    Pass `path` for one note, or `paths` to apply the same `updates` to
    several notes in one call — exactly one of the two.

    merge_arrays=True merges list values (e.g. tags); False replaces them.
    dry_run=True previews data {preview, diff, updated_keys} without writing —
    check it, then call again with dry_run=False.

    With `path`: data carries {updated_keys, diff} and the envelope a
    `revision`. With `paths`: one note failing doesn't abort the rest — this
    returns the batch envelope, with data.results holding one item per note
    ({success, path, revision} or {success: false, path, error}) and
    data.summary the {total, succeeded, failed} tally. Always check
    data.summary.failed. `expected_revision` pins a single note's bytes and so
    can't be combined with `paths`."""
    if (path is None) == (paths is None):
        raise ValueError("Pass exactly one of path=<str> or paths=<list of str>")
    if updates is None:
        raise ValueError("updates is required")

    if paths is not None:
        if expected_revision is not None:
            raise ValueError(
                "expected_revision pins one note's bytes; it cannot be combined "
                "with paths. Patch those notes one call at a time to pin each."
            )
        result = patch_frontmatter_batch(
            [{"path": p, "updates": updates, "merge_arrays": merge_arrays} for p in paths],
            index=_index,
            dry_run=dry_run,
        )
        if not dry_run:
            for entry in result["results"]:
                if entry.get("success"):
                    log_write(
                        "patch_frontmatter_tool",
                        entry.get("path"),
                        f"updated keys: {entry.get('data', {}).get('updated_keys')}",
                    )
        return batch_result(result["results"], result["summary"])

    result = patch_frontmatter(
        path,
        updates,
        merge_arrays=merge_arrays,
        index=_index,
        dry_run=dry_run,
        expected_revision=expected_revision,
    )
    if result.get("status") != "dry_run":
        log_write("patch_frontmatter_tool", path, f"updated keys: {list(updates.keys())}")
    return _write_envelope(result)


@_tool
@_mutation_boundary
def manage_tags_tool(
    path: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    expected_revision: str | None = None,
    vault: str | None = None,
) -> dict:
    """Add or remove tags on a note. Updates frontmatter tags array and strips
    inline #tag occurrences from the body. Returns {added, removed}."""
    result = manage_tags(
        path, add=add, remove=remove, index=_index, expected_revision=expected_revision
    )
    log_write("manage_tags_tool", path, f"+{add or []} -{remove or []}")
    return _write_envelope(result)


@_mutation_boundary
def move_note_tool(
    from_path: str, to_path: str, dry_run: bool = False, vault: str | None = None
) -> dict:
    """Move or rename a note and rewrite every link pointing to it: [[wikilinks]]
    and Markdown [text](path) links/embeds, keeping each link's style (%20 or
    <>, relative or full path, .md or not). The note's own relative links are
    fixed for its new folder; code blocks are left alone. All or nothing.
    Never overwrites. Linking notes are rewritten under a lock but not pinned
    to an expected_revision. data.updated_links_in lists them; dry_run=True
    returns data.links [{path, count, wikilinks, markdown}] without changing
    anything. data.reference_links_not_updated lists notes whose "[ref]: path"
    definitions point at the note (those are not rewritten)."""
    result = move_note(from_path, to_path, index=_index, dry_run=dry_run)
    if result.get("status") == "dry_run":
        return _write_envelope(result, path_key="to")
    log_write("move_note_tool", result["to"], f"moved from {result['from']}")
    return _write_envelope(result, path_key="to")


if _feature_flags.enable_move:
    mcp.tool(title="Move or rename note", annotations=_EDIT)(move_note_tool)


@mcp.tool(title="Copy note", annotations=_CREATE_ONLY)
@_mutation_boundary
def copy_note_tool(
    from_path: str, to_path: str, dry_run: bool = False, vault: str | None = None
) -> dict:
    """Copy a note to a new path on the server (exact bytes, nothing re-sent).
    Never overwrites. Links are not changed. data.name_conflicts lists notes
    sharing the new file name ([[name]] links to it would be ambiguous)."""
    result = copy_note(from_path, to_path, index=_index, dry_run=dry_run)
    if result.get("status") != "dry_run":
        log_write("copy_note_tool", result["path"], f"copied from {result['from']}")
    return _write_envelope(result)


@mcp.tool(title="Batch edit notes", annotations=_EDIT)
@_mutation_boundary
def batch_edit_tool(edits: list[dict], dry_run: bool = False, vault: str | None = None) -> dict:
    """Several edits on one or more notes, all or nothing (max 50). Edits run
    in order and build on each other; if any fails, nothing is written.
    Each edit: {"path", "op", ...} with op:
      "replace"  find, replace, mode 'exact'|'regex', count (default 1, 0=all)
      "section"  section (heading text), new_content, mode
                 'replace'|'insert_before'|'insert_after'|'append',
                 target_type 'heading'|'block_ref' (as patch_note_tool)
      "append"   content, optional section (heading); creates a missing note
      "prepend"  content (after frontmatter and a leading H1); creates if missing
      "write"    content (whole note; creates or overwrites)
    Optional per edit: expected_revision, to pin that note to a read.
    An error names the failing edit (edits[i]) and means nothing was written.
    data.notes: [{path, created, diff, revision}] (diffs over 4000 chars are
    cut, diff_truncated=true); data.unchanged; dry_run=True previews."""
    result = batch_edit(edits, index=_index, dry_run=dry_run)
    if result["status"] != "dry_run":
        for note in result["notes"]:
            log_write("batch_edit_tool", note["path"], f"batch of {result['edits']} edit(s)")
    return _write_envelope(result, path_key="_none")


# ── Query / Graph ─────────────────────────────────────────────────────────────

@_tool
def get_backlinks_tool(path: str, vault: str | None = None) -> dict:
    """Notes that link to the given note (alias-aware). `path` may be a note
    name, like read_note_tool. data.items is a list of note paths."""
    try:
        resolved, meta = _resolve_note(path)
    except FileNotFoundError:
        # Links to a note that does not exist (yet) still have sources.
        resolved, meta = path, {}
    return list_result(get_backlinks(resolved, _index), meta=meta or None)


@_tool
def get_vault_conventions_tool(vault: str | None = None) -> dict:
    """Return the vault's conventions. data: {conventions (Markdown), source, path}.
    source "file": the vault's conventions file at `path`. source
    "auto-generated": no such file, so a summary of folders, note counts,
    entry note and frontmatter usage derived from the vault."""
    return read_result(None, vault_conventions(_index))


@_tool
def get_audit_log_tool(
    path: str | None = None,
    tool: str | None = None,
    since: str | None = None,
    limit: int = 50,
    vault: str | None = None,
) -> dict:
    """Write-tool audit trail, most recent first. Optional filters: path,
    tool, since (ISO timestamp, inclusive). data.items: [{timestamp, tool,
    path, summary}]."""
    entries = get_audit_log(path=path, tool=tool, since=since, limit=limit)
    return list_result(entries, meta={"truncated": len(entries) >= limit})


@_tool
def list_vaults_tool() -> dict:
    """Vaults this identity may access: data.items [{name, description,
    is_default}]. With several, pass vault=<name> on other tools (omitted =
    the default one)."""
    cfg = get_config()
    if not cfg.multi_vault:
        vault = cfg.vaults[cfg.default_vault_name]
        return list_result(
            [{"name": vault.name, "description": vault.description, "is_default": True}]
        )

    identity = _resolve_identity(cfg)
    return list_result([
        {
            "name": name,
            "description": cfg.vaults[name].description,
            "is_default": name == identity.default,
        }
        for name in identity.vaults
    ])


@_tool
def lint_schema_tool(vault: str | None = None) -> dict:
    """Check frontmatter values against the enums declared under a
    "Frontmatter Schema" heading in the conventions file. data: {schema,
    violations: [{path, field, found, expected_enum}]}; empty when the vault
    declares no schema."""
    return read_result(None, lint_schema(_index))


@_tool
def get_broken_links_tool(vault: str | None = None) -> dict:
    """Find wikilinks/embeds that resolve to no vault file (Obsidian rules:
    case-insensitive, attachments matched by file name vault-wide; links in
    code and %% comments ignored).
    data.items: [{source, link, kind: 'note'|'attachment', suggestion}] —
    suggestion is the closest existing path or null.
    meta.warnings: links whose bare name matches several files."""
    report = find_broken_links(_index)
    return list_result(report["items"], meta={"warnings": report["warnings"]})


@_tool
def get_orphans_tool(exclude_folders: list[str] | None = None, vault: str | None = None) -> dict:
    """Find notes that no other note links to. data.items is a list of paths.
    exclude_folders defaults to the vault's configured orphan excludes; the
    configured entry note is never listed. Names without '/' match a folder
    at any depth, entries with '/' are rooted prefixes."""
    layout = get_config().layout
    excludes = exclude_folders if exclude_folders else list(layout.orphan_excludes)
    return list_result(
        get_orphans(_index, exclude_folders=excludes, entry_note=layout.entry_note),
        meta={"excluded_folders": excludes, "entry_note": layout.entry_note},
    )


@_tool
def get_link_graph_tool(
    root: str,
    depth: int = 2,
    direction: str = "both",
    vault: str | None = None,
) -> dict:
    """Return a traversable link graph starting from a note.
    direction: 'outgoing' | 'incoming' | 'both'.
    `path` is the resolved root note; data: {nodes: [{path, title, tags}],
    edges: [{from, to, type}]}."""
    result = get_link_graph(root, _index, depth=depth, direction=direction)
    return read_result(
        result.get("root", root),
        {key: value for key, value in result.items() if key != "root"},
        meta={"depth": depth, "direction": direction},
    )


@_tool
def get_vault_stats_tool(vault: str | None = None) -> dict:
    """Return vault statistics: note count, link count, orphans, broken links,
    most_linked: top 5 [{path, backlinks}] (notes with >=1 backlink).
    Vault-wide, so the envelope carries no `path`."""
    return read_result(None, get_vault_stats(_index))


@_tool
def list_all_tags_tool(
    sort_by: str = "count",
    mode: str = "flat",
    vault: str | None = None,
) -> dict:
    """Return every tag in the vault.

    mode:
    - 'flat' (default) — data.items: [{tag, count}], with note counts.
      sort_by: 'count' (descending, default) | 'name' (alphabetical).
    - 'tree' — data.tree: the same tags as a nested mapping that follows the
      slash hierarchy (e.g. konzept → python, ki → llm). sort_by is ignored.
    """
    if mode == "flat":
        return list_result(list_all_tags(_index, sort_by=sort_by), meta={"mode": mode})
    if mode == "tree":
        return read_result(None, {"tree": get_tag_tree(_index)}, meta={"mode": mode})
    raise ValueError(f"Unknown mode: {mode!r}; expected 'flat' or 'tree'")


@_tool
def get_tasks_tool(
    status: str = "open",
    folder: str = "",
    tag: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    limit: int = 100,
    offset: int = 0,
    dedupe: bool = False,
    group_by: str = "none",
    exclude_folders: list[str] | None = None,
    path_glob: str | None = None,
    summary_only: bool = False,
    vault: str | None = None,
) -> dict:
    """Return checkbox tasks from across the vault, paged.
    status: 'open' | 'done' | 'all'. folder: only this folder;
    exclude_folders: skip these (bare name = any segment, 'a/b' = prefix);
    path_glob: fnmatch pattern on the note path. tag: notes with this tag.
    due_before/due_after: 'YYYY-MM-DD', inclusive, on the 📅 due date.
    limit (default 100, max 1000) / offset page the results.
    dedupe: collapse the same text repeated in one note (e.g. worklogs) into
    its last occurrence, adding occurrences/first_line/last_line.
    group_by: 'none' | 'source' (data.items: [{source, tasks}]).
    summary_only: per-note counts [{source, open, done, total}] instead.
    Items: {text, done, source, line, due, recurrence, priority, done_date};
    Tasks-plugin emoji markers are stripped from text into those fields.
    meta: {total, returned, truncated, offset, limit}."""
    page = get_tasks_page(
        _index,
        status=status,
        folder=folder,
        tag=tag,
        due_before=due_before,
        due_after=due_after,
        limit=limit,
        offset=offset,
        dedupe=dedupe,
        group_by=group_by,
        exclude_folders=exclude_folders,
        path_glob=path_glob,
        summary_only=summary_only,
    )
    return list_result(page["items"], meta=page["meta"])


@_tool
def get_periodic_note_tool(period: str = "daily", date: str = "today", vault: str | None = None) -> dict:
    """Read or preview a periodic note at the vault's configured path pattern.
    period: 'daily' | 'weekly' | 'monthly' | 'quarterly' | 'yearly'.
    date: 'today' | 'yesterday' | 'YYYY-MM-DD'.
    data: {period, date, exists, pattern, configured, content, frontmatter, tasks}.
    When missing: exists=false plus suggested_path, template (or null) and a
    hint; nothing is created."""
    result = get_periodic_note(_index, period=period, date_str=date)
    return _read_envelope(result.get("path"), result)


@_tool
def resolve_alias_tool(name: str, vault: str | None = None) -> dict:
    """Resolve a note alias, stem, link target or attachment file name
    (e.g. 'diagram.png') to its real vault path.
    `path` and data.resolved are null (and `path` omitted) if not found."""
    resolved = resolve_alias(name, _index)
    return read_result(resolved, {"name": name, "resolved": resolved})


@_tool
def query_notes_tool(
    tags: list[str] | None = None,
    status: str | None = None,
    frontmatter_filter: dict | None = None,
    inline_field_filter: dict | None = None,
    sort_by: str = "path",
    sort_desc: bool = False,
    limit: int = 50,
    folder: str = "",
    title_contains: str | None = None,
    path_glob: str | None = None,
    mtime_after: str | None = None,
    mtime_before: str | None = None,
    min_words: int | None = None,
    has_tasks: bool | None = None,
    vault: str | None = None,
) -> dict:
    """Dataview-like query: filter notes by tags, status, frontmatter, or inline fields.
    tags: all must match (AND). sort_by: 'path'|'title'|'created'|'mtime'.
    inline_field_filter: match Dataview inline fields (key:: value syntax).
    Frontmatter-free filters: title_contains (title or filename, case/accent-
    insensitive), path_glob (fnmatch, '*' crosses '/'), mtime_after/mtime_before
    (ISO date or datetime, inclusive), min_words, has_tasks.
    created falls back to file birth time/mtime; created_source says which
    ('frontmatter'|'birthtime'|'mtime'). data.items: [{path, title, tags,
    status, created, created_source, mtime, frontmatter, inline_fields}]."""
    items = query_notes(
        _index,
        tags=tags,
        status=status,
        frontmatter_filter=frontmatter_filter,
        inline_field_filter=inline_field_filter,
        sort_by=sort_by,
        sort_desc=sort_desc,
        limit=limit,
        folder=folder,
        title_contains=title_contains,
        path_glob=path_glob,
        mtime_after=mtime_after,
        mtime_before=mtime_before,
        min_words=min_words,
        has_tasks=has_tasks,
    )
    return list_result(items, meta={"truncated": len(items) >= limit})


# ── Attachments ───────────────────────────────────────────────────────────────

@_tool
def list_attachments_tool(folder: str = "", vault: str | None = None) -> dict:
    """List all non-Markdown files in the vault: images, PDFs, audio, etc.
    data.items: [{path, size_bytes, mime_type, mtime}]."""
    return list_result(list_attachments(folder))


@_tool
def read_attachment_tool(path: str, vault: str | None = None) -> dict:
    """Read an attachment file. data.content is a UTF-8 string for text files,
    or base64-encoded bytes for binary ones (images, PDFs) — data.mime_type
    and data.encoding say which."""
    return _read_envelope(path, read_attachment(path))


@_tool
def add_attachment_tool(path: str, content_base64: str, vault: str | None = None) -> dict:
    """Write a binary attachment (image, PDF, etc.) to the vault from base64-encoded content.
    data: {size_bytes, mime_type}."""
    return _write_envelope(add_attachment(path, content_base64))


@_tool
def create_attachment_token_tool(path: str, method: str = "PUT", expires_in: int = 300, vault: str | None = None) -> dict:
    """Mint a short-lived token for raw GET/PUT /attachments/{path} (no
    base64 through the chat). method: 'PUT' | 'GET'; expires_in seconds
    (default 300, max 3600). Use data.url as-is when present; otherwise call
    /attachments/{path}?exp={expires_at}&sig={sig}&vault={vault}. Multi-vault
    mode requires an api_key identity. data: {method, vault, expires_at,
    sig, url?}."""
    cfg = get_config()
    vault_name = cfg.resolve_vault_name()
    if cfg.multi_vault:
        identity = _resolve_identity(cfg)
        if identity.type != "api_key":
            raise PermissionError(
                "Scoped attachment tokens require an api_key identity — this request "
                "authenticated as a github_login, which has no static secret to sign "
                "with. Use a plain 'Authorization: Bearer <token>' request against "
                "/attachments/* instead."
            )
        signing_key = identity.value
    else:
        signing_key = cfg.api_key
        if not signing_key:
            raise ValueError("API_KEY is not configured on this server; attachment tokens require it")
    return _read_envelope(
        path,
        create_attachment_token(
            path, signing_key=signing_key, vault=vault_name, method=method, expires_in=expires_in
        ),
    )


async def _check_bearer_token(request: Request, cfg) -> AccessToken | None:
    """Returns the AccessToken for a valid Authorization: Bearer header, or
    None if missing/invalid. Manual verification, not FastMCP's own auth
    middleware — this is a @mcp.custom_route, which bypasses that pipeline
    entirely, so get_access_token() won't see anything for this request."""
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return None
    if cfg.api_key and hmac.compare_digest(token, cfg.api_key):
        return AccessToken(token=token, client_id=cfg.api_key, scopes=[])
    if mcp.auth is not None:
        return await mcp.auth.verify_token(token)
    return None


def _check_scoped_token(request: Request, cfg, method: str, path: str) -> str | None:
    """The vault name from _scoped_token_grant(), without the identity."""
    return _scoped_token_grant(request, cfg, method, path)[0]


def _scoped_token_grant(
    request: Request, cfg, method: str, path: str
) -> tuple[str | None, object | None]:
    """Returns the vault name a valid scoped token (?exp=&sig=[&vault=])
    grants access to for this exact path+method, or None if missing/
    invalid/expired. Tries every api_key identity's own value as the HMAC
    signing key in multi-vault mode (there's no bearer header here to say
    up front which identity minted it), or the single global API_KEY in
    single-vault mode. A key matching the signature but not actually
    allowed the requested vault is treated the same as no match. The matched
    identity comes back alongside the vault name so the request runs under
    that identity's own path policy."""
    exp = request.query_params.get("exp")
    sig = request.query_params.get("sig")
    if not exp or not sig:
        return None, None
    vault_param = request.query_params.get("vault", cfg.default_vault_name)

    if cfg.multi_vault:
        candidates = [(identity.value, identity) for identity in cfg.identities if identity.type == "api_key"]
    else:
        candidates = [(cfg.api_key, None)] if cfg.api_key else []

    for signing_key, identity in candidates:
        if not verify_attachment_token(signing_key, method, path, vault_param, exp, sig):
            continue
        if identity is not None and vault_param not in identity.vaults:
            continue
        return vault_param, identity
    return None, None


@mcp.custom_route("/health", methods=["GET"])
async def health_route(request: Request) -> Response:
    """Unauthenticated liveness/readiness check for Docker HEALTHCHECK,
    uptime monitors, etc. Returns no vault content, so no auth is required.

    Returns {status: "starting"|"ok", index_ready, reconciliation telemetry}.
    Returns 503 until the initial index build and vault-wide reconciliation
    finish, then 200 while the built index remains usable. A later per-note
    reconciliation error is exposed as telemetry without discarding readiness.
    In multi-vault mode, telemetry is for Config.default_vault_name because
    this unauthenticated route exposes no per-identity vault details.
    """
    if _cfg is None or not _indices:
        return JSONResponse({"status": "starting"}, status_code=503)
    index = _indices[_cfg.default_vault_name]
    ready = index.is_ready()
    return JSONResponse(
        {
            "status": "ok" if ready else "starting",
            "index_ready": ready,
            **index.reconcile_status(),
        },
        status_code=200 if ready else 503,
    )


@mcp.custom_route("/attachments/{path:path}", methods=["PUT", "GET"])
async def attachment_route(request: Request) -> Response:
    """Direct binary upload/download, outside the MCP tool-call channel.

    add_attachment_tool/read_attachment_tool move file content as base64
    inside a tool call/result, which forces the bytes through whatever
    client/model is driving the MCP session — expensive and risky for large
    or many files. This route lets a client PUT/GET raw bytes straight to/from
    disk instead. Accepts the server's static bearer token, a valid GitHub
    OAuth access token (if configured), or a short-lived scoped token from
    create_attachment_token_tool (?exp=&sig=), so callers never need to be
    handed the long-lived master key.

    Usage:
        curl -X PUT --data-binary @file.png \\
            -H "Authorization: Bearer <API_KEY>" http://host:port/attachments/path/to/file.png
        curl -o file.png \\
            -H "Authorization: Bearer <API_KEY>" http://host:port/attachments/path/to/file.png

    Multi-vault mode: this route doesn't go through VaultResolutionMiddleware
    (it's a plain Starlette route, not MCP tool-call dispatch), so vault
    resolution is done by hand here — a bearer token resolves to its
    identity's default vault (override with ?vault=<name>, same rule as the
    vault= tool argument: must be one of that identity's allowed vaults); a
    scoped token from create_attachment_token_tool carries its vault baked
    into the signature already.
    """
    cfg = get_config()
    path = request.path_params["path"]
    # Starlette auto-adds HEAD to every GET route. HEAD is a read: it uses the
    # GET auth/read policy and never reaches the write branch. Anything else
    # besides GET/PUT is rejected outright.
    is_head = request.method == "HEAD"
    method = "GET" if is_head else request.method
    if method not in ("GET", "PUT"):
        return JSONResponse(
            {"error": "method not allowed"}, status_code=405, headers={"Allow": "GET, HEAD, PUT"}
        )

    # Authenticate and choose a vault before any path-policy check so callers
    # cannot probe protected path boundaries through response differences.
    vault_name: str | None = None
    identity = None
    access_token = await _check_bearer_token(request, cfg)
    if access_token is not None:
        if cfg.multi_vault:
            try:
                identity = _identity_for_token(cfg, access_token)
                requested = request.query_params.get("vault")
                vault_name = _select_vault(identity, requested)
            except PermissionError as exc:
                return JSONResponse({"error": str(exc)}, status_code=403)
        else:
            vault_name = cfg.default_vault_name
    else:
        vault_name, identity = _scoped_token_grant(request, cfg, method, path)

    if vault_name is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    context_token = set_current_vault(vault_name)
    identity_token = set_current_identity(identity)
    try:
        storage = VaultStorage.from_config(cfg)
        try:
            path = validate_attachment_path(path, write=method == "PUT")
        except InvalidFileTypeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except (VaultPathError, ReadPermissionError, WritePermissionError):
            return JSONResponse({"error": "forbidden"}, status_code=403)

        if method == "GET":
            try:
                data = storage.read_bytes(path)
            except FileNotFoundError:
                return JSONResponse({"error": "Attachment not found"}, status_code=404)
            mime, _ = mimetypes.guess_type(path)
            return Response(
                b"" if is_head else data,
                media_type=mime or "application/octet-stream",
                headers={"Content-Length": str(len(data)), **attachment_download_headers(path)},
            )

        if method != "PUT":  # defensive: only PUT may reach the write branch
            return JSONResponse({"error": "method not allowed"}, status_code=405)

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                return JSONResponse({"error": "Invalid Content-Length"}, status_code=400)
            if declared_length < 0:
                return JSONResponse({"error": "Invalid Content-Length"}, status_code=400)
            if declared_length > cfg.max_attachment_bytes:
                return JSONResponse({"error": "Attachment too large"}, status_code=413)

        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > cfg.max_attachment_bytes:
                return JSONResponse({"error": "Attachment too large"}, status_code=413)
            chunks.append(chunk)
        data = b"".join(chunks)
        try:
            result = write_attachment_bytes(path, data)
        except AttachmentTooLargeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=413)
        except (ValueError, InvalidFileTypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except (VaultPathError, WritePermissionError):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(result)
    finally:
        reset_current_identity(identity_token)
        reset_current_vault(context_token)


# ── Templates ─────────────────────────────────────────────────────────────────

@_tool
def list_templates_tool(vault: str | None = None) -> dict:
    """List template files in the vault's configured templates folder.
    data.items is a list of template paths; meta.templates_dir is the folder,
    and meta.hint explains an empty result (e.g. the folder doesn't exist)."""
    info = list_templates_info()
    meta = {"templates_dir": info["templates_dir"]}
    if "hint" in info:
        meta["hint"] = info["hint"]
    return list_result(info["items"], meta=meta)


@_tool
@_mutation_boundary
def create_from_template_tool(
    template_path: str,
    output_path: str,
    variables: dict | None = None,
    expected_revision: str | None = None,
    create_only: bool = False,
    vault: str | None = None,
) -> dict:
    """Render a template and write it as a new note. template_path: a vault
    path, or a name relative to the configured templates folder.
    Built-in variables: {{date}}, {{time}}, {{title}}, {{week}}, {{month}}, {{year}}, {{weekday}}.
    Supports format specs: {{date:YYYY-MM}} → '2026-07'.
    Custom variables passed in 'variables' dict override built-ins.
    Unknown {{vars}} are preserved as-is.
    `path` is the written note; data: {template, variables}."""
    return _write_envelope(
        create_from_template(
            template_path,
            output_path,
            variables=variables,
            index=_index,
            expected_revision=expected_revision,
            create_only=create_only,
        ),
        path_key="output",
    )


# ── Canvas ────────────────────────────────────────────────────────────────────

if _feature_flags.enable_canvas:

    @_tool
    def list_canvases_tool(vault: str | None = None) -> dict:
        """List all Obsidian Canvas (.canvas) files in the vault.
        data.items is a list of .canvas paths."""
        return list_result(list_canvases())

    @_tool
    def read_canvas_tool(path: str, vault: str | None = None) -> dict:
        """Read an Obsidian Canvas file.
        data: {nodes: [{id, type, text, file, x, y}], edges: [{from, to, label}]}."""
        return _read_envelope(path, read_canvas(path))

    @_tool
    @_mutation_boundary
    def write_canvas_tool(
        path: str,
        nodes: list[dict] | None = None,
        edges: list[dict] | None = None,
        expected_revision: str | None = None,
        create_only: bool = False,
        vault: str | None = None,
    ) -> dict:
        """Create or fully overwrite an Obsidian Canvas file.
        Node fields: type ('text'|'file'|'group'|'link'), x, y, width, height.
        Text nodes: text. File nodes: file (vault path). Link nodes: url.
        Edge fields: fromNode, toNode, label (optional). IDs are auto-generated if omitted.
        data: {nodes, edges} (counts)."""
        return _write_envelope(
            write_canvas(
                path,
                nodes=nodes,
                edges=edges,
                expected_revision=expected_revision,
                create_only=create_only,
            )
        )

    @_tool
    @_mutation_boundary
    def patch_canvas_tool(
        path: str,
        add_nodes: list[dict] | None = None,
        update_nodes: list[dict] | None = None,
        delete_node_ids: list[str] | None = None,
        add_edges: list[dict] | None = None,
        delete_edge_ids: list[str] | None = None,
        expected_revision: str | None = None,
        vault: str | None = None,
    ) -> dict:
        """Atomically update an existing canvas without rewriting the whole file.
        update_nodes: each dict must include 'id'. delete_node_ids also removes
        all edges connected to those nodes. data: {nodes, edges} (counts)."""
        return _write_envelope(
            patch_canvas(
                path,
                add_nodes=add_nodes,
                update_nodes=update_nodes,
                delete_node_ids=delete_node_ids,
                add_edges=add_edges,
                delete_edge_ids=delete_edge_ids,
                expected_revision=expected_revision,
            )
        )


# ── Excalidraw ────────────────────────────────────────────────────────────────

if _feature_flags.enable_excalidraw:

    @_tool
    def list_excalidraw_tool(vault: str | None = None) -> dict:
        """List all Obsidian Excalidraw (*.excalidraw.md) files in the vault.
        data.items is a list of *.excalidraw.md paths."""
        return list_result(list_excalidraw())

    @_tool
    def read_excalidraw_tool(path: str, vault: str | None = None) -> dict:
        """Read an Obsidian Excalidraw file.
        data: {elements, app_state, files}."""
        return _read_envelope(path, read_excalidraw(path))

    @_tool
    @_mutation_boundary
    def write_excalidraw_tool(
        path: str,
        elements: list[dict] | None = None,
        app_state: dict | None = None,
        expected_revision: str | None = None,
        create_only: bool = False,
        vault: str | None = None,
    ) -> dict:
        """Create or fully overwrite an Excalidraw file.
        Element fields: type ('rectangle'|'ellipse'|'text'|'arrow'|'freedraw'|...), x, y,
        width, height. Element 'id' is auto-generated if omitted.
        data: {elements} (count)."""
        return _write_envelope(
            write_excalidraw(
                path,
                elements=elements,
                app_state=app_state,
                index=_index,
                expected_revision=expected_revision,
                create_only=create_only,
            )
        )

    @_tool
    @_mutation_boundary
    def patch_excalidraw_tool(
        path: str,
        add_elements: list[dict] | None = None,
        update_elements: list[dict] | None = None,
        delete_element_ids: list[str] | None = None,
        expected_revision: str | None = None,
        vault: str | None = None,
    ) -> dict:
        """Atomically update an existing Excalidraw file without rewriting the whole file.
        update_elements: each dict must include 'id'.
        data: {elements} (count)."""
        return _write_envelope(
            patch_excalidraw(
                path,
                add_elements=add_elements,
                update_elements=update_elements,
                delete_element_ids=delete_element_ids,
                index=_index,
                expected_revision=expected_revision,
            )
        )


# ── Kanban ────────────────────────────────────────────────────────────────────

if _feature_flags.enable_kanban:

    @_tool
    def read_kanban_tool(path: str, vault: str | None = None) -> dict:
        """Read an Obsidian Kanban board (requires kanban-plugin in frontmatter).
        data: {plugin, columns: [{name, cards: [{text, done}]}], total_cards}."""
        return _read_envelope(path, read_kanban(path))

    @_tool
    @_mutation_boundary
    def create_kanban_board_tool(
        path: str,
        columns: list[str],
        expected_revision: str | None = None,
        create_only: bool = False,
        vault: str | None = None,
    ) -> dict:
        """Create a new Kanban board with the given column names.
        data: {columns}."""
        return _write_envelope(
            create_kanban_board(
                path,
                columns,
                index=_index,
                expected_revision=expected_revision,
                create_only=create_only,
            )
        )

    @_tool
    @_mutation_boundary
    def add_kanban_card_tool(
        path: str,
        column: str,
        text: str,
        done: bool = False,
        expected_revision: str | None = None,
        vault: str | None = None,
    ) -> dict:
        """Add a card to a Kanban column. Card is inserted at the top of the column.
        data: {column, card, done}."""
        return _write_envelope(
            add_kanban_card(
                path,
                column,
                text,
                done=done,
                index=_index,
                expected_revision=expected_revision,
            )
        )

    @_tool
    @_mutation_boundary
    def move_kanban_card_tool(
        path: str,
        card_text: str,
        from_column: str,
        to_column: str,
        done: bool | None = None,
        expected_revision: str | None = None,
        vault: str | None = None,
    ) -> dict:
        """Move a card from one column to another. done=true/false updates the tick state.
        data: {card, from, to}."""
        return _write_envelope(
            move_kanban_card(
                path,
                card_text,
                from_column,
                to_column,
                done=done,
                index=_index,
                expected_revision=expected_revision,
            )
        )

    @_tool
    @_mutation_boundary
    def delete_kanban_card_tool(
        path: str,
        card_text: str,
        column: str | None = None,
        expected_revision: str | None = None,
        vault: str | None = None,
    ) -> dict:
        """Delete a card from the Kanban board. column limits the search to one column.
        data: {card}."""
        return _write_envelope(
            delete_kanban_card(
                path,
                card_text,
                column=column,
                index=_index,
                expected_revision=expected_revision,
            )
        )


# ── Bases ─────────────────────────────────────────────────────────────────────

if _feature_flags.enable_bases:

    @_tool
    def list_bases_tool(vault: str | None = None) -> dict:
        """List all Obsidian Bases (.base) files in the vault.
        data.items is a list of .base paths."""
        return list_result(list_bases())

    @_tool
    def read_base_tool(path: str, vault: str | None = None) -> dict:
        """Read an Obsidian Bases file.
        data: {filters, formulas, properties, views}."""
        return _read_envelope(path, read_base(path))

    @_tool
    @_mutation_boundary
    def write_base_tool(
        path: str,
        filters: dict | None = None,
        formulas: dict | None = None,
        properties: dict | None = None,
        views: list[dict] | None = None,
        expected_revision: str | None = None,
        create_only: bool = False,
        vault: str | None = None,
    ) -> dict:
        """Create or fully overwrite a .base file.
        filters: boolean tree ({and:[...]}, {or:[...]}, {not:...}) or a single
        string statement, e.g. 'status != "done"' or 'file.hasTag("book")'.
        formulas: name -> expression string. properties: name -> {displayName}.
        views: list of {type, name, limit, filters, order, groupBy, summaries};
        'type' (e.g. 'table'|'cards'|'list') is required per view.
        data: {views, known_properties} — known_properties is collected from
        existing .base files in the vault to keep naming consistent."""
        return _write_envelope(
            write_base(
                path,
                filters=filters,
                formulas=formulas,
                properties=properties,
                views=views,
                index=_index,
                expected_revision=expected_revision,
                create_only=create_only,
            )
        )

    @_tool
    @_mutation_boundary
    def patch_base_tool(
        path: str,
        update_formulas: dict | None = None,
        delete_formula_keys: list[str] | None = None,
        update_properties: dict | None = None,
        delete_property_keys: list[str] | None = None,
        set_filters: dict | None = None,
        add_views: list[dict] | None = None,
        update_views: list[dict] | None = None,
        delete_view_names: list[str] | None = None,
        expected_revision: str | None = None,
        vault: str | None = None,
    ) -> dict:
        """Atomically update an existing .base file without rewriting it wholesale.
        update_formulas/update_properties are merged by key. set_filters replaces
        the whole filters block. update_views: each dict must include 'name'.
        data: {views} (count)."""
        return _write_envelope(
            patch_base(
                path,
                update_formulas=update_formulas,
                delete_formula_keys=delete_formula_keys,
                update_properties=update_properties,
                delete_property_keys=delete_property_keys,
                set_filters=set_filters,
                add_views=add_views,
                update_views=update_views,
                delete_view_names=delete_view_names,
                index=_index,
                expected_revision=expected_revision,
            )
        )


# ── Folders ───────────────────────────────────────────────────────────────────

@_tool
def list_folder_tool(path: str = "", recursive: bool = False, max_depth: int | None = None, vault: str | None = None) -> dict:
    """List the contents of a vault folder (non-hidden items only).
    path='': root of the vault.
    recursive=False (default): immediate contents only — data: {folders, files}.
    recursive=True: full tree dump in one call — data: {tree: {folders: {name: tree}, files: [...]}}.
    max_depth limits how many levels deep to descend (None = unlimited); folders
    cut by the limit carry truncated=true and children_count (direct subfolders
    + files), so they are not confused with empty folders."""
    result = list_folder(path, recursive=recursive, max_depth=max_depth)
    return _read_envelope(result.get("path", path), result, meta={"recursive": recursive})


@_tool
def list_files_tool(folder: str = "", extension: str | None = None, vault: str | None = None) -> dict:
    """List every file in the vault (or a subfolder), any type — not just
    notes/attachments/bases/canvases (e.g. .lock files, stray non-Markdown
    files). extension filters by suffix without the dot (e.g. "lock",
    "canvas"); omit for everything. Hidden files/folders are skipped.
    data.items is a list of vault-relative paths."""
    return list_result(list_files(folder, extension=extension))


@_tool
def create_folder_tool(path: str, vault: str | None = None) -> dict:
    """Create a folder (and any missing parents) in the vault.
    Returns {path, status}."""
    result = create_folder(path)
    log_write("create_folder_tool", path, "created folder")
    return _write_envelope(result)


def rename_folder_tool(from_path: str, to_path: str, vault: str | None = None) -> dict:
    """Rename or move a vault folder. Rewrites path-based wikilinks in all
    notes that reference notes inside the moved folder.
    Returns {from, to, notes_moved, updated_links_in}."""
    result = rename_folder(from_path, to_path, index=_index)
    log_write("rename_folder_tool", to_path, f"renamed from {from_path}")
    return _write_envelope(result, path_key="to", action="renamed")


if _feature_flags.enable_folder_rename:
    _tool(rename_folder_tool)


def list_trash_tool(vault: str | None = None) -> dict:
    """List items sitting in .trash/ (from delete_tool with trash=True).
    Names here are what restore_tool expects as trashed_name.
    data.items: [{name, type, size_bytes, mtime}]."""
    return list_result(list_trash()["items"])


if _feature_flags.enable_delete:
    _tool(list_trash_tool)


# ── MCP Resources ─────────────────────────────────────────────────────────────

@mcp.resource("vault://notes/{path}")
def vault_note_resource(path: str) -> str:
    """Raw content of a vault note — use as context without calling a tool."""
    try:
        return VaultStorage.from_config().read_text(path)
    except FileNotFoundError as exc:
        raise ResourceError("Vault note not found") from exc
    except (ReadPermissionError, PermissionError) as exc:
        raise ResourceError("Access denied for vault note") from exc
    except VaultPathError as exc:
        raise ResourceError("Invalid vault note path") from exc
    except (ConfigError, OSError) as exc:
        raise ResourceError("Unable to read vault note") from exc


@mcp.resource("vault://stats")
def vault_stats_resource() -> dict:
    """Current vault statistics (note count, links, orphans, broken links)."""
    return get_vault_stats(_index)


@mcp.resource("vault://tags")
def vault_tags_resource() -> list:
    """All tags in the vault with note counts, sorted by frequency."""
    return list_all_tags(_index, sort_by="count")


# ── Exposure filters ──────────────────────────────────────────────────────────
# Applied last, after every registration above (including register_ob_tools),
# so an operator can trim the advertised surface without touching the
# ENABLE_* groups: TOOLS_ALLOWLIST keeps only the named tools, TOOLS_DENYLIST
# then removes more, ENABLE_PROMPTS / ENABLE_RESOURCES drop the generic
# prompts and vault:// resources. Removed components are unregistered, so they
# can be neither listed nor called. Runtime services (index, watcher, the ob
# supervisor) do not depend on which tools are exposed.

@dataclass(frozen=True)
class _ExposureSettings:
    allowlist: frozenset[str] | None
    denylist: frozenset[str]
    enable_prompts: bool
    enable_resources: bool

    @classmethod
    def from_env(cls) -> _ExposureSettings:
        def names(variable: str) -> frozenset[str] | None:
            raw = os.environ.get(variable, "")
            if not raw.strip():
                return None
            return frozenset(name.strip() for name in raw.split(",") if name.strip())

        return cls(
            allowlist=names("TOOLS_ALLOWLIST"),
            denylist=names("TOOLS_DENYLIST") or frozenset(),
            enable_prompts=_env_bool("ENABLE_PROMPTS", True),
            enable_resources=_env_bool("ENABLE_RESOURCES", True),
        )


def _run_provider_call(factory):
    """Run a provider coroutine from synchronous module code, also when an
    event loop is already running (tests reload this module from async
    tests). Listing the local provider is pure in-memory work."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


def _apply_exposure_filters(server: FastMCP, settings: _ExposureSettings) -> dict:
    """Unregister tools/prompts/resources per ``settings``. Returns what was
    removed plus allowlist/denylist names that match no registered tool
    (logged as a warning, never fatal: a typo must not take the server down)."""
    provider = server.local_provider
    registered = {tool.name for tool in _run_provider_call(provider.list_tools)}
    removed: set[str] = set()
    if settings.allowlist is not None:
        removed |= registered - settings.allowlist
    removed |= registered & settings.denylist
    for name in sorted(removed):
        provider.remove_tool(name)

    unknown_allowed = sorted((settings.allowlist or frozenset()) - registered)
    unknown_denied = sorted(settings.denylist - registered)
    if unknown_allowed:
        logger.warning(
            "TOOLS_ALLOWLIST names unknown or disabled tool(s): %s", ", ".join(unknown_allowed)
        )
    if unknown_denied:
        logger.warning(
            "TOOLS_DENYLIST names unknown or disabled tool(s): %s", ", ".join(unknown_denied)
        )

    removed_prompts: list[str] = []
    if not settings.enable_prompts:
        for prompt in _run_provider_call(provider.list_prompts):
            provider.remove_prompt(prompt.name)
            removed_prompts.append(prompt.name)
    removed_resources: list[str] = []
    if not settings.enable_resources:
        for resource in _run_provider_call(provider.list_resources):
            provider.remove_resource(str(resource.uri))
            removed_resources.append(str(resource.uri))
        for template in _run_provider_call(provider.list_resource_templates):
            provider.remove_template(template.uri_template)
            removed_resources.append(template.uri_template)

    if removed or removed_prompts or removed_resources:
        logger.info(
            "Exposure filters: %d tool(s) exposed, %d hidden; prompts %s; resources %s",
            len(registered - removed),
            len(removed),
            "off" if not settings.enable_prompts else "on",
            "off" if not settings.enable_resources else "on",
        )
    return {
        "removed_tools": sorted(removed),
        "unknown_allowlist": unknown_allowed,
        "unknown_denylist": unknown_denied,
        "removed_prompts": removed_prompts,
        "removed_resources": removed_resources,
    }


_exposure = _apply_exposure_filters(mcp, _ExposureSettings.from_env())


# ── Startup ───────────────────────────────────────────────────────────────────

def main() -> None:
    global _cfg
    _cfg = get_config()
    for name, vault in _cfg.vaults.items():
        context_token = set_current_vault(name)
        try:
            # The index and watcher are shared by every identity, so their
            # policy stays the vault's own: per-identity read scoping is a
            # per-call filter on results, not a narrower index.
            policy = VaultAccessPolicy.from_config(
                _cfg, apply_identity_overrides=False
            )
        finally:
            reset_current_vault(context_token)
        VaultStorage(policy).probe_create_only_support()
        index = VaultIndex(vault.path, exclude_paths=vault.exclude_paths, policy=policy)
        watcher = VaultWatcher(
            vault.path,
            debounce_ms=_cfg.watcher_debounce_ms,
            reconcile_interval=_cfg.index_reconcile_interval,
            policy=policy,
        )
        _indices[name] = index
        _watchers[name] = watcher

        def initialize_index(
            index: VaultIndex = index,
            watcher: VaultWatcher = watcher,
            name: str = name,
        ) -> None:
            try:
                index.build(publish_ready=False)
                watcher.start(on_change=index.update, on_reconcile=index.reconcile)
                index.reconcile()
                index.mark_ready()
            except Exception:
                logger.exception("Initial index build/reconciliation failed for vault %s", name)

        threading.Thread(target=initialize_index, daemon=True).start()
    if _ob_enabled:
        from .ob import start_ob

        start_ob()  # validates OB_* settings; child is stopped at exit
    if _cfg.multi_vault:
        logger.info("Multi-vault mode: %d vault(s) configured (%s)", len(_cfg.vaults), ", ".join(_cfg.vaults))
    if _cfg.transport == "stdio":
        logger.info("Starting obsidian-mcp (transport=stdio)")
        mcp.run(transport=_cfg.transport)
    else:
        logger.info(
            "Starting obsidian-mcp (transport=%s, host=%s, port=%d)",
            _cfg.transport, _cfg.host, _cfg.port,
        )
        mcp.run(transport=_cfg.transport, host=_cfg.host, port=_cfg.port)


if __name__ == "__main__":
    main()

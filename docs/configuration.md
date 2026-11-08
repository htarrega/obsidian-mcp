# Configuration

Copy `.env.example` to `.env` and set your vault path. Every variable below
has inline documentation in `.env.example` too.

```env
VAULT_PATH=/path/to/your/obsidian/vault
```

## Path policy

```env
# READ_ONLY=true            # safe default for network Compose deployments
# READ_PATHS=Notes/,Inbox/  # restrict all reads to specific rooted scopes
# WRITE_PATHS=Notes/,Inbox/ # restrict writes to specific folders
# DENY_READ_PATHS=.obsidian/,.trash/ # security boundary for all reads
# DENY_WRITE_PATHS=.obsidian/,.trash/,_AI_INSTRUCTIONS.md
```

Slash-suffixed policy entries such as `Notes/` cover that directory and its
descendants. An entry without a trailing slash grants or denies only the
exact path. For compatibility, native configuration still permits
unrestricted writes when both `READ_ONLY=false` and `WRITE_PATHS` is empty;
choose that combination explicitly, not as a network default. Use a
case-sensitive filesystem for authoritative path scopes (deny rules are
case-folded defensively for common local case-insensitive filesystems).

> **Docker default:** the Compose configuration defaults `READ_ONLY=true`,
> while a native `Config` invocation defaults to `false`. Existing Docker
> deployments that intentionally write must explicitly set
> `READ_ONLY=false`, paired with a narrow `WRITE_PATHS`. For a nested scope
> such as `deep/nested/`, create `deep/` beforehand — MCP may create the
> configured `nested/` scope and descendants, but never ancestors above the
> configured write boundary.

`READ_PATHS` is an optional allowlist using the same rooted exact/recursive
syntax. When set, direct reads, listings, search, index construction, and
resources are limited to those scopes; `DENY_READ_PATHS` still takes
precedence. Ancestor directories may be traversed only to reach an allowed
scope and do not expose sibling content.

`EXCLUDE_PATHS` (default `private/,.obsidian/,.trash/`) uses the same rooted
matching rules but is only a discovery filter, not an access-control
boundary. `private/` hides that root directory and its descendants, while it
does not hide `Projects/private/`.

Revision-aware note mutation tools must read the target to determine
existence, preserve frontmatter, calculate diffs, or derive an incremental
edit. They therefore reject paths covered by `DENY_READ_PATHS` even if
`WRITE_PATHS` also contains the path. Avoid overlapping those scopes for
note workflows.

## Write preconditions

Direct note reads return an opaque `sha256:...` revision. Pass it as
`expected_revision` when replacing or appending to an existing note so an
edit landed by Obsidian Sync during the client's think time is reported as
a conflict instead of silently overwritten.

```env
# REQUIRE_WRITE_PRECONDITIONS=true  # require a read revision before full overwrite
```

Network Compose configurations enable this by default. Incremental
patch/tag/frontmatter tools always protect the exact version they read
internally, regardless of this flag. This is optimistic concurrency, not
exactly-once execution: after a lost append response, re-read and verify
the result before retrying without the old revision. See
[Response Envelope](response-envelope.md).

## Deletion

```env
# ALLOW_PERMANENT_DELETE=false
```

`delete_tool`/`restore_tool`/`list_trash_tool` are only registered when
`ENABLE_DELETE=true` (see [Optional tool groups](#optional-tool-groups)
below). Permanent (non-trash) deletion is a separate, further opt-in on top
of that.

## Attachments

```env
# MAX_ATTACHMENT_BYTES=26214400  # 25 MiB default, applies to MCP and HTTP writes
```

## Security hardening

All optional; the defaults are the hardened setting.

| Variable | Default | Effect |
|---|---|---|
| `MASK_ERROR_DETAILS` | `true` | Hide details of unexpected tool exceptions from MCP clients. Expected errors (invalid arguments, policy denials such as "Write access denied", note not found) still return their message; OS errors lose their filename. |
| `REGEX_MAX_LENGTH` | `500` | Maximum length of a caller-supplied regex (`search_notes_tool` `mode=regex`, `patch_note_text_tool` and `find_replace_in_vault_tool` `mode=regex`). |
| `REGEX_TIMEOUT_SECONDS` | `2` | Per-match time limit for those regexes. The first timeout aborts the whole call, and nothing is written. |
| `OAUTH_GITHUB_SCOPES` | `read:user` | GitHub scopes requested and required (comma-separated). Only the login is needed. |
| `OAUTH_ALLOWED_CLIENT_REDIRECT_URIS` | `https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback,http://localhost:*,http://127.0.0.1:*` | MCP client redirect URIs the OAuth server accepts (comma-separated; `*` matches any port, or any subdomain as `*.example.com`). Add your client's callback here if it is not claude.ai or a local tool. |
| `OAUTH_ENABLE_CIMD` | `false` | Allow URL-based client IDs (Client ID Metadata Documents), which make the server fetch client-chosen URLs. |
| `OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS` | `2592000` (30 days) | Lifetime of the access token the server issues to MCP clients after GitHub login. GitHub OAuth-app tokens never expire, so without this the token lived 1 year. |

The `OAUTH_*` variables only apply when GitHub OAuth is configured (see
[Remote Setup](remote-setup.md)). Invalid values stop the server at startup.

## Indexing and sync

```env
# WATCHER_DEBOUNCE_MS=100
# INDEX_RECONCILE_INTERVAL=900  # full Markdown hash sweep every 15 minutes
```

Watcher events are debounced to absorb sync write storms. The index
additionally hashes readable, indexable Markdown on the reconcile interval
to repair missed filesystem events (Obsidian Sync, git checkouts). PDFs,
images, other attachments, excluded paths, and Excalidraw files are not
hashed. Reconciliation telemetry (readiness, last run time/duration/error)
is exposed on `/health` — see [Docker & Deployment](deployment.md#health-check).

## Application state (outside the vault)

```env
# LOCK_PATH=/data/locks
# AUDIT_LOG_PATH=/data/audit.jsonl
```

Lock files and the audit log are application state, not vault content, and
must live outside `VAULT_PATH`. Native default for locks is
`<system-temp>/obsidian-mcp-locks`; Docker uses `/data/locks`. The audit log
defaults beneath `LOCK_PATH` natively and to `/data/audit.jsonl` in Docker;
the final log file is opened without following symlinks. The best-effort
JSONL audit log records every write-tool call
(`{timestamp, tool, path, summary}`); query it with `get_audit_log_tool`
(pass `path=` for one note's history).

New files and directories use the normal `0666`/`0777` creation modes
filtered by the MCP process umask. Atomic overwrites preserve the existing
file's permission bits. In a shared sync deployment, run the MCP and sync
daemon with compatible UID/GID and umask settings so both can continue
reading and updating new notes — the home-server Compose profile exposes
these as `PUID`/`PGID` (see [Docker & Deployment](deployment.md)).

## Optional tool groups

```env
# ENABLE_CANVAS=true      # .canvas file tools
# ENABLE_EXCALIDRAW=true  # *.excalidraw.md file tools
# ENABLE_KANBAN=true      # Kanban board tools
# ENABLE_BASES=true       # .base file tools (Obsidian core plugin, 1.9.0+)

# ENABLE_MOVE=true             # move_note_tool
# ENABLE_FOLDER_RENAME=true    # rename_folder_tool
# ENABLE_BULK_REPLACE=true     # find_replace_in_vault_tool
# ENABLE_DELETE=true           # delete_tool, restore_tool, list_trash_tool
```

Each defaults to `false`. A disabled group's tools aren't just refused at
call time — they're never registered, so they don't appear in a connected
client's tool list at all. Their underlying Python functions remain
available for local/unit-test use regardless of the flag.

## Transport and network

```env
# TRANSPORT=stdio    # stdio (default), http (recommended for network use), or sse (legacy)
# HOST=0.0.0.0
# PORT=8000
```

See [Remote Setup](remote-setup.md) for auth (`API_KEY`, `PUBLIC_BASE_URL`,
`OAUTH_GITHUB_*`) and [Multi-Vault Setup](multi-vault.md) for `VAULTS_CONFIG`.

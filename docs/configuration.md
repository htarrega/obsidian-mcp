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
| `REGEX_MAX_REPEAT` | `100` | Largest `{n}` / `{n,m}` / `{n,}` repetition count accepted in those regexes. Checked statically before compiling, because the match timeout does not cover compilation and nested counted repetitions such as `(((a{50}){50}){50}){20}` allocate hundreds of MB at compile time. Counted repetitions may also nest at most 2 levels deep, and nested bounds may multiply to at most 10,000. |
| `OAUTH_GITHUB_SCOPES` | `read:user` | GitHub scopes requested and required (comma-separated). Only the login is needed. |
| `OAUTH_ALLOWED_CLIENT_REDIRECT_URIS` | `https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback,http://localhost:*,http://127.0.0.1:*` | MCP client redirect URIs the OAuth server accepts (comma-separated; `*` matches any port, or any subdomain as `*.example.com`). Add your client's callback here if it is not claude.ai or a local tool. |
| `OAUTH_ENABLE_CIMD` | `false` | Allow URL-based client IDs (Client ID Metadata Documents), which make the server fetch client-chosen URLs. |
| `OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS` | `2592000` (30 days) | Lifetime of the access token the server issues to MCP clients after GitHub login. GitHub OAuth-app tokens never expire, so without this the token lived 1 year. |
| `OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS` | `7776000` (90 days) | Lifetime of the refresh token the server issues to MCP clients. GitHub returns no refresh-token expiry, so without this it lived 1 year; after it expires the client has to log in again. |

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

## Vault layout

Where this vault keeps its templates, periodic notes, conventions file and
entry note, and which folders orphan detection skips. All optional; the
defaults reproduce the historical layout, so existing vaults need nothing.

```env
# TEMPLATES_DIR=Templates
# PERIODIC_DAILY_PATH=Journal/{{date:YYYY-MM-DD}}.md
# PERIODIC_WEEKLY_PATH=Journal/Weekly/{{date:GGGG-[W]WW}}.md
# PERIODIC_MONTHLY_PATH=Journal/Monthly/{{date:YYYY-MM}}.md
# PERIODIC_QUARTERLY_PATH=Journal/Quarterly/{{date:YYYY-[Q]Q}}.md
# PERIODIC_YEARLY_PATH=Journal/Yearly/{{date:YYYY}}.md
# ORPHAN_EXCLUDES=Journal,Templates
# ENTRY_NOTE=                       # e.g. Home.md — never reported as an orphan
# CONVENTIONS_PATH=_AI_INSTRUCTIONS.md
```

| Variable | Default | Used by |
|---|---|---|
| `TEMPLATES_DIR` | `Templates` | `list_templates_tool`, `create_from_template_tool` (a bare template name resolves inside it), periodic-note previews (`<TEMPLATES_DIR>/Daily-Note-Template.md`, …). A missing folder gives an empty list plus `meta.hint`, not an error. |
| `PERIODIC_<PERIOD>_PATH` | see above | `get_periodic_note_tool`. `<PERIOD>` is `DAILY`, `WEEKLY`, `MONTHLY`, `QUARTERLY` or `YEARLY`. |
| `ORPHAN_EXCLUDES` | `Journal,Templates` | `get_orphans_tool` when called without `exclude_folders`. Comma-separated. |
| `ENTRY_NOTE` | unset | Never listed by `get_orphans_tool`; named in the auto-generated conventions summary. `.md` is appended if missing. |
| `CONVENTIONS_PATH` | `_AI_INSTRUCTIONS.md` | `get_vault_conventions_tool`, `lint_schema_tool`, and the single-vault startup instructions. |

**Periodic patterns** use the Obsidian Daily/Periodic Notes syntax: one or
more `{{date:FORMAT}}` placeholders with moment.js tokens (`YYYY`, `YY`,
`Q`, `MMMM`, `MMM`, `MM`, `M`, `DD`, `D`, `DDDD`, `dddd`, `ddd`, `GGGG`,
`gggg`, `WW`, `W`, `ww`, `w`, `E`, `e`; `[text]` is a literal). Weeks are
always ISO weeks. A bare `{{date}}` uses the period's default format, and
`.md` is appended if the pattern doesn't end with it. Folders may be part
of the format, e.g. `Journal/{{date:YYYY}}/{{date:MM-DD}}.md`. When the
note for a date doesn't exist, `get_periodic_note_tool` returns
`exists: false`, `suggested_path`, the template it would preview (or
`null`) and a `hint` — it never creates the note itself, and the hint says
when the path comes from the default pattern (the vault may not use
periodic notes at all).

**Orphan excludes**: an entry without `/` matches a folder of that name at
any depth (the historical behaviour: `Journal` also skips
`Areas/Journal/`); an entry containing `/` is a rooted prefix
(`(400) Personal/Temporary`). Matching ignores case and Unicode
normalisation (NFC/NFD). Setting `ORPHAN_EXCLUDES` replaces the default
list; an explicit `exclude_folders` argument replaces it for that call.

**Conventions**: if the file at `CONVENTIONS_PATH` is missing or empty,
`get_vault_conventions_tool` returns `source: "auto-generated"` with a
structural summary (top-level folders and note counts, entry note or the
most-linked note, frontmatter fields and how many notes have frontmatter,
attachment count, layout settings) instead of an empty string; a real file
returns `source: "file"`. The summary contains paths and counts only, never
note text, and is computed over what the caller may read.

All layout values are vault-relative paths validated at startup by the same
normaliser as the path-policy rules: absolute paths and `..` escapes stop
the server with a configuration error. Spaces, parentheses and accents are
fine. Layout settings never grant access — reads still go through the
read/deny policy. When `DENY_WRITE_PATHS` is unset, its default protects
the configured `CONVENTIONS_PATH` instead of `_AI_INSTRUCTIONS.md`; if you
set `DENY_WRITE_PATHS` explicitly, include the conventions file yourself.

In multi-vault mode the same settings are per-vault keys in `vaults.json`
(see [Multi-Vault](multi-vault.md#vault-layout)).

A per-vault `.obsidian-mcp.json` file inside the vault is deliberately not
supported: the vault is content the AI (and every synced device) can edit,
so server behaviour stays in the environment / `vaults.json`.

### Notes without frontmatter

`query_notes_tool` and `list_notes_tool(include_meta=True)` report
`created` from the frontmatter when present, otherwise from the file's
birth time (where the OS exposes one) or its mtime, with
`created_source: "frontmatter" | "birthtime" | "mtime"`; `sort_by="created"`
uses the same value. Mtime-based values are local ISO datetimes, and after
a sync they reflect when the file was last written on this server, not
when the note was first written. `query_notes_tool` also filters without
frontmatter: `title_contains` (title or filename, case- and
accent-insensitive), `path_glob` (fnmatch, `*` also matches `/`),
`mtime_after` / `mtime_before` (ISO date or datetime, inclusive; a date
covers the whole day), `min_words`, and `has_tasks`.

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

## Obsidian Headless sync (ob)

```env
# ENABLE_OB=true              # supervise `ob sync --continuous` + read-only tools
# ENABLE_OB_OPS=true          # sync_restart
# ENABLE_OB_ADMIN=false       # sync_set_* (ignored when READ_ONLY=true)
# OB_BIN=ob
# OB_VAULT_PATH=/path/to/vault  # defaults to VAULT_PATH; required with VAULTS_CONFIG
# OB_LOG_LINES=1000
# OB_COMMAND_TIMEOUT=60
# OB_RESTART_BACKOFF_MAX=300
# OB_RESTART_COOLDOWN=60
```

| Variable | Default | Meaning |
|---|---|---|
| `ENABLE_OB` | `false` | Master switch: the server supervises one `ob sync --continuous` child and registers `sync_status`, `sync_logs`, `ob_info`, `list_remote_vaults`. When false the `obsidian_mcp.ob` package is not even imported. Accepts `1`/`true`/`yes` (case-insensitive, surrounding whitespace ignored), like the other `ENABLE_OB*` flags. |
| `ENABLE_OB_OPS` | `false` | Adds `sync_restart`. |
| `ENABLE_OB_ADMIN` | `false` | Adds the persistent `sync_set_*` tools; ignored when `READ_ONLY=true`. |
| `OB_BIN` | `ob` | Path or name of the `ob` executable. |
| `OB_VAULT_PATH` | `VAULT_PATH` | Vault to sync. Required in multi-vault mode. |
| `OB_LOG_LINES` | `1000` | Lines kept in the in-memory, redacted sync log (max `sync_logs` returns). |
| `OB_COMMAND_TIMEOUT` | `60` | Seconds before a one-shot `ob` command is killed. |
| `OB_RESTART_BACKOFF_MAX` | `300` | Maximum seconds between restarts of a crashing sync child. |
| `OB_RESTART_COOLDOWN` | `60` | `sync_restart` is refused if the child was (re)started less than this many seconds ago; `0` disables it. |

`ob` children run with a minimal environment (`PATH`, `HOME`,
`XDG_CONFIG_HOME`, `LANG`, `LC_ALL`, `OBSIDIAN_AUTH_TOKEN`) so server secrets
never reach them. `OBSIDIAN_AUTH_TOKEN` and the content of
`$XDG_CONFIG_HOME/obsidian-headless/auth_token` are masked in all output. See
the README section "ob CLI wrapper" for the architecture and tool tiers.

## Transport and network

```env
# TRANSPORT=stdio    # stdio (default), http (recommended for network use), or sse (legacy)
# HOST=0.0.0.0
# PORT=8000
```

See [Remote Setup](remote-setup.md) for auth (`API_KEY`, `PUBLIC_BASE_URL`,
`OAUTH_GITHUB_*`) and [Multi-Vault Setup](multi-vault.md) for `VAULTS_CONFIG`.

# obsidian-mcp

[![CI](https://github.com/ykoellmann/obsidian-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ykoellmann/obsidian-mcp/actions/workflows/ci.yml)

An MCP (Model Context Protocol) server for [Obsidian](https://obsidian.md) vaults. Connects Claude (or any MCP client) directly to your vault — read, write, search, navigate links, and manage notes, canvases, and kanban boards.

## Why obsidian-mcp?

The official Obsidian MCP plugin requires the Obsidian desktop app to be running and only works on the same machine. **obsidian-mcp is a standalone server** — no Obsidian app needed.

The intended setup is to host obsidian-mcp on a server or NAS where your vault is continuously synced (via [Syncthing](https://syncthing.net), [git](https://github.com/denolehov/obsidian-git), [rclone](https://rclone.org), or [Obsidian Sync](https://obsidian.md/sync)). Claude then connects to that server over the network, so:

- **Always up to date** — the server sees every change your Obsidian app writes, immediately
- **Access from anywhere** — connect from Claude Desktop, Claude Code, or any MCP client on any machine, without the vault being present locally
- **Multiple clients** — several Claude sessions can read the vault simultaneously; writes are serialized with per-file locking
- **No app dependency** — the server runs headless and starts automatically (systemd, Docker, etc.)

```
[Obsidian app]  ──sync──►  [vault on server]  ◄──MCP──  [Claude on any machine]
  (phone/laptop)              (NAS / VPS)                  (Claude Desktop / Code)
```

## Features

- **Read & Search** — read notes, search full-text (exact/regex/fuzzy, optionally combined with a frontmatter filter or scoped to filenames), render embedded transclusions, inspect note outlines, list every file in the vault regardless of type
- **Duplicate prevention** — `find_similar_notes_tool` ranks notes by TF-IDF similarity so a new note doesn't duplicate an existing one under different wording
- **Schema linting** — `lint_schema_tool` validates frontmatter against the enums declared in your own `_AI_INSTRUCTIONS.md`, plus an optional cron-friendly health-check script
- **Write** — create/overwrite notes (with automatic frontmatter preservation, dry-run previews, and unified diffs), patch sections or anchor-less body text, append content, update frontmatter (single or batch), manage tags, move notes with automatic wikilink rewriting
- **Optimistic concurrency** — every read returns a revision; pass it back to a write to detect edits landed by Obsidian Sync in between
- **Folders** — list (optionally recursive with a full tree dump), create, delete, rename folders; renaming rewrites path-based wikilinks vault-wide
- **Query & Graph** — backlinks, broken links, orphan detection, BFS link graph, vault stats, task collection across vault
- **Dataview-like queries** — filter notes by tags, status, frontmatter fields (exact match or `$ne`/`$in`/`$nin`/`$exists` operators), or inline fields (`key:: value`)
- **Audit log** — every write-tool call is recorded; `get_audit_log_tool` queries it
- **Periodic Notes** — read/preview daily, weekly, monthly, quarterly, yearly journal notes from templates
- **Canvas / Excalidraw / Kanban / Bases** *(each opt-in via its own `ENABLE_*` flag)* — read, create, and patch these Obsidian plugin formats
- **Attachments** — list, read (text or base64), and add binary files
- **Two auth variants** — a static API key (Claude Code, Desktop, curl) and, optionally, GitHub OAuth (claude.ai Web/Mobile Custom Connector) — usable independently or at the same time
- **Multi-vault** *(opt-in)* — serve several fully isolated vaults from one deployment, each identity mapped to only the vault(s) it may access
- **Templates** — render Obsidian templates with built-in (`{{date}}`, `{{title}}`, …) and custom variables
- **MCP Resources & Prompts** — vault notes/stats/tags as MCP resources; `weekly_review`/`daily_note` prompts

One JSON response shape across every tool — see [Response Envelope](docs/response-envelope.md).

## Installation

**Via uvx (no clone needed):**
```bash
VAULT_PATH=/your/vault uvx obsidian-remote-mcp
```

**Via Docker (no Python needed):**
```bash
docker compose up -d   # see docker-compose.yml
```

**From source:**
```bash
git clone https://github.com/ykoellmann/obsidian-mcp.git
cd obsidian-mcp
uv sync
uv run obsidian-remote-mcp
```

## Quick configuration

Copy `.env.example` to `.env` and set your vault path:

```env
VAULT_PATH=/path/to/your/obsidian/vault
```

That's enough for local `stdio` use. For network transports, path
restrictions, optional plugin-format tools, and every other variable, see
**[Configuration](docs/configuration.md)**.

Security settings (all optional, hardened defaults; details in
[Configuration → Security hardening](docs/configuration.md#security-hardening)):

| Variable | Default |
|---|---|
| `MASK_ERROR_DETAILS` | `true` |
| `REGEX_MAX_LENGTH` | `500` |
| `REGEX_TIMEOUT_SECONDS` | `2` |
| `REGEX_MAX_REPEAT` | `100` |
| `OAUTH_GITHUB_SCOPES` | `read:user` |
| `OAUTH_ALLOWED_CLIENT_REDIRECT_URIS` | claude.ai / claude.com MCP callbacks, `http://localhost:*`, `http://127.0.0.1:*` |
| `OAUTH_ENABLE_CIMD` | `false` |
| `OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS` | `2592000` (30 days) |

## Usage with Claude Code / Desktop

Add to your MCP config (`~/.claude/mcp.json` or `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "uv",
      "args": ["--directory", "/path/to/obsidian-mcp", "run", "obsidian-remote-mcp"],
      "env": {
        "VAULT_PATH": "/path/to/your/obsidian/vault"
      }
    }
  }
}
```

## Documentation

| Topic | |
|---|---|
| **[Configuration](docs/configuration.md)** | Every env var: path policy, write preconditions, deletion, indexing/sync, optional tool groups, transport |
| **[Tool Reference](docs/tools.md)** | Every MCP tool, grouped, with the opt-in flags each needs |
| **[Response Envelope](docs/response-envelope.md)** | The `{success, path, revision, data, meta}` shape every tool returns |
| **[Remote Setup](docs/remote-setup.md)** | Running over the network: API key auth and GitHub OAuth for claude.ai |
| **[Multi-Vault Setup](docs/multi-vault.md)** | Serving several isolated vaults from one deployment |
| **[Docker & Deployment](docs/deployment.md)** | Compose, the hardened home-server profile, health checks, the schema-lint cron |
| **[Vault Conventions](docs/vault-conventions.md)** | Teaching the AI your vault's structure via `_AI_INSTRUCTIONS.md` |
| **[Architecture](docs/architecture.md)** | Source layout and the authorization/concurrency layers underneath the tools |
| **[Development](docs/development.md)** | Running tests, linting, and the local HTTP smoke test |

## ob CLI wrapper

Optional, off by default. With `ENABLE_OB=true` the server supervises
[Obsidian Headless](https://www.npmjs.com/package/obsidian-headless) (`npm i -g
obsidian-headless`, command `ob`, tested against 0.0.14) so the vault it
serves is kept in sync with Obsidian Sync, and exposes a few tools to inspect
and steer that sync. With `ENABLE_OB` unset or false the server behaves
exactly as before: the `obsidian_mcp.ob` package is not even imported, no
tool is registered and no process is started.

### Architecture

- **Supervisor** (`src/obsidian_mcp/ob/supervisor.py`): on startup `main()`
  launches one child, `ob sync --path <OB_VAULT_PATH> --continuous`, with the
  vault's parent directory as working directory (ob gets `--path`, so it
  never relies on its cwd), no shell, in its own process group, and a minimal
  environment: only `PATH`, `HOME`, `XDG_CONFIG_HOME`, `LANG`, `LC_ALL` and
  `OBSIDIAN_AUTH_TOKEN` are passed, so server secrets such as
  `OAUTH_GITHUB_CLIENT_SECRET` or `API_KEY` never reach ob or its npm
  dependencies (one-shot commands get the same environment). A monitor
  thread captures stdout+stderr line by line into a ring buffer of
  `OB_LOG_LINES` lines (redacted before storage) and tracks pid, state
  (`starting`/`running`/`backoff`/`failed`/`stopped`), start time, restart
  count, last exit code, last `Fully synced` time and recent error lines.
  Crashes are restarted with exponential backoff (2s doubling up to
  `OB_RESTART_BACKOFF_MAX`, reset after 60s of stable uptime). Exit code 2
  (not logged in) and 3 (no sync configuration) are reported as `failed` with
  a hint and retried only every `OB_RESTART_BACKOFF_MAX` seconds. At most one
  child ever runs. On shutdown (normal exit or SIGTERM) the child gets
  SIGTERM (`ob` then releases `.obsidian/.sync.lock`), and SIGKILL after 10s.
- **Runner** (`runner.py`): one-shot `ob` commands run from an argv list
  checked against a subcommand/flag allowlist, never through a shell, with
  `OB_COMMAND_TIMEOUT` (process group killed on expiry) and a 256 KB output
  cap per stream. `--path` always comes from configuration, never from the
  model.
- **Redaction** (`redact.py`): e-mails, `Bearer …`, `password=`/`token=`/
  `encryptionKey`-style values, long opaque tokens (≥24 chars of hex, or
  mixed-case alphanumerics with digits) and the value of
  `OBSIDIAN_AUTH_TOKEN` are masked in every tool result, log buffer line,
  error message and server log record. JSON keys such as `encryptionKey`,
  `token`, `email` are dropped entirely; vault ids and local paths are kept.
- **Admin changes**: `ob sync-config` takes no lock and the continuous
  process only reads config at start, so every admin tool does *stop child →
  `ob sync-config --path V <flag> <value> --json` → restart child* (the child
  is restarted even if the config command fails).

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ENABLE_OB` | `false` | Master switch: supervisor + read-only tools |
| `ENABLE_OB_OPS` | `false` | Adds the ops tier (`sync_restart`) |
| `ENABLE_OB_ADMIN` | `false` | Adds the admin tier (`sync_set_*`); ignored when `READ_ONLY=true` |
| `OB_BIN` | `ob` | Path or name of the `ob` executable |
| `OB_VAULT_PATH` | `VAULT_PATH` | Vault to sync. **Required** with multi-vault (`VAULTS_CONFIG`): startup fails with a clear error instead of guessing |
| `OB_LOG_LINES` | `1000` | Ring-buffer size (max lines `sync_logs` returns) |
| `OB_COMMAND_TIMEOUT` | `60` | Seconds before a one-shot `ob` command is killed |
| `OB_RESTART_BACKOFF_MAX` | `300` | Maximum seconds between restarts of a crashing sync child |

`READ_ONLY=true` disables only the admin tier; the ops tier depends only on
`ENABLE_OB_OPS`.

### Tools

| Tier | Tool | What it does |
|---|---|---|
| read-only | `sync_status` | Supervisor state + `ob sync-status --json` (vault, mode, conflict strategy, device, file types, configs, excluded folders) |
| read-only | `sync_logs(lines=100)` | Tail of the redacted log buffer, clamped to 1..`OB_LOG_LINES` |
| read-only | `ob_info` | `ob --version`, this vault's `sync-list-local` entry, device name, whether an auth token file/env exists (boolean only). No network |
| read-only | `list_remote_vaults` | `ob sync-list-remote --json` (network) |
| ops | `sync_restart` | SIGTERM the child, wait, relaunch; returns the new status |
| admin | `sync_set_excluded_folders(folders)` | Persistent: replaces excluded folders (relative, no `..`, no leading `/`, no commas; `[]` clears) |
| admin | `sync_set_file_types(types)` | Persistent: `image`, `audio`, `video`, `pdf`, `unsupported` (`[]` clears) |
| admin | `sync_set_configs(categories)` | Persistent: `app`, `appearance`, `appearance-data`, `hotkey`, `core-plugin`, `core-plugin-data`, `community-plugin`, `community-plugin-data` (`[]` clears) |
| admin | `sync_set_conflict_strategy(strategy)` | Persistent: `merge` or `conflict` |

**Never exposed:** `ob login`, `logout`, `sync-setup`, `sync-create-remote`,
`sync-unlink`, `publish-*`, `sync-config --mode`, `--config-dir`,
`--device-name`, or any arbitrary command/flag.

### Operator setup and security notes

- Logging in and linking the vault are done **manually by the operator**, as
  the user the server runs as: `ob login`, then `ob sync-setup --path <vault>`
  (and optionally `ob sync-config …`). The MCP server never does this.
- `ob` stores credentials in plaintext under the server user's home:
  `${XDG_CONFIG_HOME:-$HOME/.config}/obsidian-headless/auth_token` and
  `…/sync/<vaultId>/config.json` (contains the vault `encryptionKey`). The
  wrapper never reads or returns these files (`ob_info` only checks the
  token file exists). Run the server as a **dedicated, unprivileged user**
  whose home holds nothing else, keep that directory `0700`, and make sure
  it is outside every vault and not reachable through the MCP path policy.
- Admin tools persistently change what syncs to/from every device; enable
  `ENABLE_OB_ADMIN` only for trusted clients. In multi-vault mode the ob
  tools act on `OB_VAULT_PATH` for any authenticated identity.
- If the server process is SIGKILLed the child cannot be cleaned up; `ob`
  reclaims its stale lock after 5s on the next start.

## License

MIT

# obsidian-mcp

[![CI](https://github.com/htarrega/obsidian-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/htarrega/obsidian-mcp/actions/workflows/ci.yml)

An MCP (Model Context Protocol) server for [Obsidian](https://obsidian.md)
vaults, written in Python on [FastMCP](https://gofastmcp.com). It connects
Claude (or any MCP client) directly to a vault on disk: read, search, write,
edit, move and copy notes, navigate links, and work with canvases, Excalidraw
drawings, kanban boards and bases.

## Why obsidian-mcp?

The official Obsidian MCP plugin needs the Obsidian desktop app running on
the same machine. **obsidian-mcp is a standalone server**: no Obsidian app
needed. Host it next to a copy of your vault that is kept in sync (by
[Syncthing](https://syncthing.net), [git](https://github.com/denolehov/obsidian-git),
[rclone](https://rclone.org), [Obsidian Sync](https://obsidian.md/sync), or the
built-in [Obsidian Headless supervisor](#ob-cli-wrapper-obsidian-headless-sync))
and connect to it over the network:

- **Always up to date**: a file watcher picks up every change your Obsidian apps sync in
- **Access from anywhere**: Claude Desktop, Claude Code, claude.ai (web/mobile) or any MCP client, without the vault being present locally
- **Multiple clients**: concurrent sessions; writes are serialized with per-file locks and optimistic revisions
- **No app dependency**: runs headless under systemd or Docker

```
[Obsidian app]  ──sync──►  [vault on server]  ◄──MCP──  [Claude on any machine]
  (phone/laptop)              (NAS / VPS)                  (Desktop / Code / claude.ai)
```

## Features

- **Read and search**: notes by path or bare name (resolved like a `[[wikilink]]`), outlines, rendered transclusions, full-text search (exact/regex/fuzzy; exact and fuzzy ignore case and accents), TF-IDF similar notes to avoid duplicates
- **Write and edit**: create/overwrite with frontmatter preservation, edit a section by heading (`patch_note`) or by anchor-less text, append/prepend, toggle one task line, frontmatter and tags, `dry_run` previews with unified diffs
- **Batch edits**: `batch_edit` applies up to 50 edits over one or more notes in order, all or nothing
- **Move and copy**: `copy_note` (create-only) and `move_note`, which is all or nothing, has `dry_run`, rewrites wikilinks *and* Markdown links/embeds keeping each link's style, and refuses names that would make links ambiguous
- **Delete**: to `.trash/` or permanently, chosen by `DELETE_MODE` (opt-in group, see below)
- **Query and graph**: Dataview-like frontmatter/tag/inline-field queries, backlinks, outgoing links, broken links resolved with Obsidian's rules, orphans, link graph, vault stats, paged task collection
- **Plugin formats** *(opt-in)*: Canvas, Excalidraw, Kanban, Bases
- **Also**: attachments, templates, periodic notes, frontmatter schema linting, an audit log of every write, MCP resources and prompts, multi-vault with per-identity isolation
- **MCP tool annotations**: every tool declares `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint=false`, so clients can tell safe calls from destructive ones

Every tool returns the same JSON envelope (see
[Response Envelope](docs/response-envelope.md)). The full list, with the flag
each tool needs, is in the **[Tool Reference](docs/tools.md)**.

## Quick start

**From source:**
```bash
git clone https://github.com/htarrega/obsidian-mcp.git
cd obsidian-mcp
uv sync
VAULT_PATH=/path/to/your/vault uv run obsidian-remote-mcp
```

**Straight from git with uvx** (the package is not published to PyPI):
```bash
VAULT_PATH=/path/to/your/vault uvx --from git+https://github.com/htarrega/obsidian-mcp obsidian-remote-mcp
```

**With Docker:** the image `ghcr.io/htarrega/obsidian-mcp` is built for
`linux/amd64` and `linux/arm64` on every `v*` tag; `docker-compose.yml` uses
it (or swap `image:` for `build: .`). See
[Docker & Deployment](docs/deployment.md).

```bash
cp .env.example .env   # set HOST_VAULT_PATH, API_KEY, ...
docker compose up -d
```

### Claude Code / Claude Desktop (stdio)

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

For HTTP transport with an API key or GitHub OAuth (claude.ai Custom
Connector), see [Remote Setup](docs/remote-setup.md).

## Configuration highlights

`VAULT_PATH` is the only required setting; copy `.env.example` to `.env` for
the rest. Every variable is documented in
**[Configuration](docs/configuration.md)**. The ones that shape what a model
can do:

| Variable | Default | Effect |
|---|---|---|
| `READ_ONLY` | `false` (`true` in `docker-compose.yml`) | Refuse every write |
| `READ_PATHS` / `WRITE_PATHS` / `DENY_READ_PATHS` / `DENY_WRITE_PATHS` | see docs | [Path policy](docs/configuration.md#path-policy) |
| `REQUIRE_WRITE_PRECONDITIONS` | `false` (`true` in Compose) | Overwrites need the `revision` from a prior read |
| `ENABLE_MOVE`, `ENABLE_FOLDER_RENAME`, `ENABLE_BULK_REPLACE`, `ENABLE_DELETE` | `false` | [Optional tool groups](docs/configuration.md#optional-tool-groups); disabled tools are never registered |
| `ENABLE_CANVAS`, `ENABLE_EXCALIDRAW`, `ENABLE_KANBAN`, `ENABLE_BASES` | `false` | Plugin-format tool groups |
| `DELETE_MODE` | `trash` | What `delete_tool` does when the caller omits `trash`; `permanent` also needs `ALLOW_PERMANENT_DELETE=true` ([Deletion](docs/configuration.md#deletion)) |
| `TOOLS_ALLOWLIST` / `TOOLS_DENYLIST` | empty | Trim the [exposed tool surface](docs/configuration.md#exposed-tool-surface) to what a workflow needs |
| `ENABLE_PROMPTS` / `ENABLE_RESOURCES` | `true` | Drop the generic prompts / `vault://` resources |
| `TAGS_SOURCE` | `both` | Read tags from `frontmatter`, `inline` or `both` |
| `ENABLE_OB`, `ENABLE_OB_OPS`, `ENABLE_OB_ADMIN` | `false` | [Obsidian Headless sync](#ob-cli-wrapper-obsidian-headless-sync) tiers |

## Security model

- **Authentication**: a static API key and/or GitHub OAuth for HTTP transports ([Remote Setup](docs/remote-setup.md)); multi-vault maps each identity to only the vaults it may access ([Multi-Vault](docs/multi-vault.md))
- **Path policy**: every read and write is checked against rooted allow/deny scopes; `.obsidian/` and `.trash/` are denied by default in Compose
- **Least surface**: destructive groups (delete, move, folder rename, bulk replace) are opt-in and unregistered when off; the allowlist hides anything else
- **Safe writes**: per-file locks, optimistic `revision` checks, `dry_run` previews, all-or-nothing `batch_edit` and `move_note`, and an audit log of every write
- **Hardened defaults**: masked error details and bounded regex length, repetition and run time ([Security hardening](docs/configuration.md#security-hardening))
- **`ob` isolation**: the sync child gets a minimal environment, never a shell, and all its output is redacted (see below)

## Deployment

- [Docker & Deployment](docs/deployment.md): Compose, the hardened home-server profile, health checks, the schema-lint cron
- [deploy/README.md](deploy/README.md): production deployment on an Oracle Cloud VM with Caddy (TLS), systemd, Obsidian Headless and GitHub OAuth; `deploy/env.example` is the annotated production environment
- [Deployment architecture](docs/deployment-architecture.md): system diagrams (trust boundaries, OAuth, service internals, feedback loops)

## Documentation

| Topic | |
|---|---|
| **[Configuration](docs/configuration.md)** | Every environment variable |
| **[Tool Reference](docs/tools.md)** | Every MCP tool, grouped, with the opt-in flags each needs |
| **[Response Envelope](docs/response-envelope.md)** | The `{success, path, revision, data, meta}` shape every tool returns |
| **[Remote Setup](docs/remote-setup.md)** | API key auth and GitHub OAuth for claude.ai |
| **[Multi-Vault Setup](docs/multi-vault.md)** | Serving several isolated vaults from one deployment |
| **[Vault Conventions](docs/vault-conventions.md)** | Teaching the AI your vault's structure via `_AI_INSTRUCTIONS.md` |
| **[Architecture](docs/architecture.md)** | Source layout and the authorization/concurrency layers |
| **[Development](docs/development.md)** | Tests, linting and the local HTTP smoke test |
| **[Changelog](CHANGELOG.md)** | What changed |

## Development

```bash
uv sync --dev
uv run pytest -q
uv run ruff check src/ tests/
uv run python scripts/run_local_smoke_test.py   # real server + MCP client over HTTP
```

CI (`.github/workflows/ci.yml`) runs ruff and pytest on every push and pull
request to `main`; `release.yml` publishes the Docker image on `v*` tags.

## ob CLI wrapper (Obsidian Headless sync)

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
  `encryptionKey`/`key=`/`cookie:`/`session=`-style values (quoted values up
  to the closing quote; unquoted passwords, passphrases, secrets and cookies
  to the end of the line), long opaque tokens (≥24 chars of hex, or
  mixed-case alphanumerics with digits), the value of `OBSIDIAN_AUTH_TOKEN`
  and the content of ob's `auth_token` file (read once at startup, never
  returned) are masked in every tool result, log buffer line, error message
  and server log record. JSON keys containing `key`, `token`, `secret`,
  `pass`, `salt`, `cookie` or `session`, and `email`, are dropped entirely;
  vault ids and local paths are kept.
- **Admin changes**: `ob sync-config` takes no lock and the continuous
  process only reads config at start, so every admin tool does *stop child →
  `ob sync-config --path V <flag> <value> --json` → restart child* (the child
  is restarted even if the config command fails).

### Configuration

`ENABLE_OB` (supervisor + read-only tools), `ENABLE_OB_OPS` (adds
`sync_restart`) and `ENABLE_OB_ADMIN` (adds the persistent `sync_set_*` tools;
ignored when `READ_ONLY=true`) all default to `false`. `OB_VAULT_PATH`
defaults to `VAULT_PATH` and is required with multi-vault (`VAULTS_CONFIG`).
`OB_BIN`, `OB_LOG_LINES`, `OB_COMMAND_TIMEOUT`, `OB_RESTART_BACKOFF_MAX` and
`OB_RESTART_COOLDOWN` are documented in
[Configuration → Obsidian Headless sync](docs/configuration.md#obsidian-headless-sync-ob).

### Tools

| Tier | Tool | What it does |
|---|---|---|
| read-only | `sync_status` | Supervisor state + `ob sync-status --json` (vault, mode, conflict strategy, device, file types, configs, excluded folders) |
| read-only | `sync_logs(lines=100, exclude_noise=true, filter=None, collapse_repeats=true)` | Tail of the redacted log buffer, clamped to 1..`OB_LOG_LINES`. By default drops heartbeat lines (`Fully synced`, `Connecting...`, `Connection successful. Detecting changes...`) and reports `noise_lines_skipped` + `last_fully_synced_at`; `filter` is a case-insensitive substring (max 200 chars, no control characters, matched on redacted text); consecutive identical lines fold into one `[repeated Nx, last at <ts>]` line |
| read-only | `ob_info` | `ob --version`, this vault's `sync-list-local` entry, device name, whether an auth token file/env exists (boolean only). No network |
| read-only | `list_remote_vaults` | `ob sync-list-remote --json` (network) |
| ops | `sync_restart` | SIGTERM the child, wait, relaunch; returns the new status. Refused within `OB_RESTART_COOLDOWN` seconds of the last (re)start, or if another start/stop is still in progress after 30s |
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

## Origin and license

This project started as a fork of
[ykoellmann/obsidian-mcp](https://github.com/ykoellmann/obsidian-mcp) by
Yannik Koellmann and is now developed independently. Many thanks to the
original author. Released under the [MIT License](LICENSE).

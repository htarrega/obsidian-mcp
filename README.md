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

## License

MIT

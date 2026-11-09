# Multi-Vault Setup

By default obsidian-mcp serves one vault (`VAULT_PATH`). If you need several
completely separate vaults from one deployment — e.g. a private vault and a
work vault, each only reachable by its own identity — set `VAULTS_CONFIG` to
the path of a JSON file instead. Vault path-policy settings (`VAULT_PATH`,
`READ_PATHS`, `WRITE_PATHS`, `DENY_READ_PATHS`, `DENY_WRITE_PATHS`, and
`EXCLUDE_PATHS`) and identity settings (`API_KEY` and
`OAUTH_GITHUB_ALLOWED_LOGINS`) then come from that file. See
[`vaults.json.example`](../vaults.json.example):

```json
{
  "vaults": {
    "private": {"path": "/vaults/private", "exclude_paths": ["private/", ".obsidian/", ".trash/"]},
    "monari":  {"path": "/vaults/monari",  "write_paths": ["02-Areas/monari/"]}
  },
  "identities": [
    {"type": "api_key",      "value": "sk-...",              "vaults": ["private"]},
    {"type": "github_login", "value": "your-github-username", "vaults": ["private", "monari"], "default": "private"}
  ]
}
```

Each vault entry can set `read_paths`, `write_paths`, `deny_read_paths`,
`deny_write_paths`, `exclude_paths`, and `read_only` independently. Path
rules are rooted: a trailing slash includes descendants, while a rule
without one matches only that exact path. `exclude_paths` controls
discovery/indexing; the read/write/deny fields are the access-control
boundary. A single identity can additionally be given a narrower view of one
of its vaults — see [Per-Identity Path Policy](#per-identity-path-policy).

### Vault layout

Each vault entry can also describe its own layout (all optional; absent keys
keep the defaults, see [Configuration → Vault layout](configuration.md#vault-layout)):

```json
"mentat": {
  "path": "/vaults/mentat",
  "templates_dir": "(000) System/Templates",
  "periodic": {"daily": "(400) Personal/Journal/{{date:YYYY-MM-DD}}.md"},
  "orphan_excludes": ["(000) System", "(100) Attachments", "(400) Personal"],
  "entry_note": "(500) Root/Mentat.md",
  "conventions_path": "(000) System/_AI_INSTRUCTIONS.md"
}
```

`periodic` accepts `daily`, `weekly`, `monthly`, `quarterly` and `yearly`.
These are the JSON equivalents of `TEMPLATES_DIR`, `PERIODIC_<PERIOD>_PATH`,
`ORPHAN_EXCLUDES`, `ENTRY_NOTE` and `CONVENTIONS_PATH`, which are ignored
in multi-vault mode. Paths are validated like path-policy rules.

```env
VAULT_PATH=              # unused — vaults.json defines paths instead
VAULTS_CONFIG=/data/vaults.json
TRANSPORT=http
```

Multi-vault mode requires an authenticated network transport (`http`, `sse`,
or `streamable-http`). It cannot be used with `stdio`, because stdio has no
authenticated request identity to map to a vault.

Each `identities` entry is either an **API key** (`Authorization: Bearer
<value>`, same as [Option A](remote-setup.md#option-a-api-key-claude-code-claude-desktop-curl-mcp-remote))
or a **GitHub login** (same allowlist mechanism as
[Option B](remote-setup.md#option-b-github-oauth-claudeai-webmobile-custom-connector) —
set `OAUTH_GITHUB_CLIENT_ID`/`SECRET`/`PUBLIC_BASE_URL` as usual, just skip
`OAUTH_GITHUB_ALLOWED_LOGINS` since `vaults.json` replaces it). Both kinds
can be mixed and used at the same time. Whichever identity a request
authenticates as, every tool call is transparently scoped to that identity's
vault(s) — there is no way to reach a vault an identity isn't listed for.

An identity with more than one entry in `"vaults"` can switch between them:
every tool accepts an optional `vault=<name>` argument for that one call. If
omitted, it uses the identity's configured `"default"`; an identity with
several vaults and no default must pass `vault=` explicitly.
`list_vaults_tool()` returns `[{name, description, is_default}]`
for whichever identity is calling, so an MCP client can discover what it's
allowed to pass — the built-in instructions tell Claude to call it first
and pass `vault=` when the conversation clearly points at a non-default
vault. There's no server-side memory of which vault was picked last; it's
re-selected on every call, same as any other argument.

### Per-Identity Path Policy

By default every identity sees a vault through that vault's own policy. To
give one identity a *narrower* view of a vault it already has access to,
write `"vaults"` as an object instead of an array and attach a policy
override to the vault name:

```json
{
  "vaults": {
    "monari": {"path": "/vaults/monari", "write_paths": ["02-Areas/monari/"]}
  },
  "identities": [
    {"type": "api_key", "value": "sk-me", "vaults": ["monari"]},
    {
      "type": "github_login",
      "value": "your-coworkers-github-username",
      "vaults": {
        "monari": {
          "read_paths": ["02-Areas/monari/"],
          "write_paths": ["02-Areas/monari/shared/"],
          "deny_read_paths": ["02-Areas/monari/salaries/"],
          "deny_write_paths": ["02-Areas/monari/shared/policy.md"],
          "read_only": false
        }
      }
    }
  ]
}
```

Both forms can be mixed freely across identities, and the array form keeps
working exactly as before — `"vaults": ["private", "monari"]` means "inherit
each vault's policy unchanged". An override may omit any field to inherit it;
`{}` is an override that changes nothing.

**An override can only ever restrict, never widen:**

| Field | Merge rule |
| --- | --- |
| `read_paths`, `write_paths` | Intersect. Every override rule must resolve inside the vault's own allowlist; a rule that would widen it is a **config error that fails the server at startup**, not a silently ignored setting. If the vault has no allowlist (the whole vault is in scope), the override simply becomes the scope. |
| `deny_read_paths`, `deny_write_paths` | Union. An override can add denials but cannot drop one the vault declares — omitting a rule does not remove it. |
| `read_only` | OR toward `true`. An identity can force itself read-only on a writable vault; `false` cannot unlock a vault that is read-only. |

Read scoping applies to tool *output*, not just direct reads. The vault index
and file watcher stay vault-wide and identity-agnostic, so each index-backed
tool filters what it is about to return through the same effective policy a
`read_note_tool` call would be checked against: backlinks, orphans, broken
links, the link graph, tag tree, `list_all_tags_tool`, tasks, vault stats,
`query_notes_tool`, `resolve_alias_tool`, `lint_schema_tool` and
`find_similar_notes_tool`. The link graph neither reports nor traverses
*through* an out-of-scope note, so an unreadable hop cannot leak its own path
or the notes it links on to; tag counts, vault stats and similarity scores are
aggregated over the visible notes only.

`/attachments/*` (the direct binary upload/download route) is fully
multi-vault-aware: a plain `Authorization: Bearer` request resolves to that
identity's default vault, or pass `?vault=<name>` in the URL to pick a
different one of its allowed vaults (same rule as the `vault=` tool
argument). `create_attachment_token_tool`'s short-lived scoped tokens
(`?exp=&sig=`) work too, signed against the calling identity's own key and
bound to a specific vault — but only for **api_key** identities, since a
GitHub login has no static secret of its own to sign with; use a plain
`Authorization: Bearer` request for those instead.

> **Known limitation:** `/health` doesn't go through per-request auth/vault
> resolution — it always reports on the first vault listed in
> `vaults.json`, regardless of which identity would be calling. It exposes
> no vault content either way (just process liveness), so this doesn't leak
> anything.

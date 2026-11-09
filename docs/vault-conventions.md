# Vault Conventions (Customization)

Create `_AI_INSTRUCTIONS.md` in your vault root (or wherever
`CONVENTIONS_PATH` points, e.g. `(000) System/_AI_INSTRUCTIONS.md`; per
vault via `conventions_path` in `vaults.json`) to teach the AI how your
specific vault is organized:

```markdown
## Structure
- `Notes/` — evergreen notes and concepts
- `Projects/` — active and archived projects (tag: #project/active, #project/done)
- `Journal/` — daily notes (YYYY-MM-DD.md)

## Frontmatter Schema
- status: active | done | inbox
- tags: nested with / (e.g. #concept/programming)

## Conventions
- Link by stem only, never by full path
- Every note needs a created: date in frontmatter
```

In single-vault mode, the server loads this file at startup and sends it to
the AI as system instructions. Without it, built-in generic Obsidian syntax
guidance is used. Multi-vault servers always use the generic startup
instructions because MCP server instructions are shared by every identity;
use `get_vault_conventions_tool(vault=...)` to load the selected vault's
conventions after authorization. The conventions file is the right
place for everything vault-specific — folder layout, tag schema, naming
conventions, and any workflow rules.

A `## Frontmatter Schema` heading with a fenced `field: value` list is also
what `lint_schema_tool` parses to validate notes against — see
[Tool Reference](tools.md) and the [Health-Check Cron](deployment.md#health-check-cron-frontmatter-schema).

## Without a conventions file

If the file doesn't exist, `get_vault_conventions_tool` does not return an
empty string: it returns `source: "auto-generated"` with a summary derived
from the vault — top-level folders and note counts, the entry note
(`ENTRY_NOTE`, or the most-linked note when none is configured), which
frontmatter fields are used and by how many notes, the share of notes with
frontmatter, the attachment count, and the layout settings in effect. A
real file comes back as `source: "file"`. `lint_schema_tool` then returns an
empty schema plus a `hint`.

## Vault layout

The server-side counterpart of the conventions file tells the tools where
things are: templates folder, periodic-note path patterns, orphan
excludes, the entry (root/index) note and the conventions path. Vaults
that use different folders than the defaults (`Templates/`, `Journal/...`)
should set them — see [Configuration → Vault layout](configuration.md#vault-layout).
Keep the conventions file write-protected (`DENY_WRITE_PATHS`) so the AI
cannot rewrite its own rules.

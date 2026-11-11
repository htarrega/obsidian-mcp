# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Every tool (note tools and `ob` tools) advertises one shared
  `outputSchema`: the response envelope `{success, path?, revision?, data,
  meta?}` (plus `code`/`message` on errors), instead of an opaque object.
  `data` and `meta` stay free-form objects.
- Machine-readable error codes. Every tool error is an MCP error result
  (`isError: true`) whose structured content is `{success: false, data: {},
  code, message}`; `code` is one of `invalid_argument`, `not_found`,
  `already_exists`, `permission_denied`, `permanent_delete_disabled`,
  `revision_conflict`, `precondition_required`, `unavailable`, `tool_error`,
  `internal`. Masked errors (`MASK_ERROR_DETAILS`) keep their generic message
  and get `internal`. The text content of an error is unchanged, and
  `revision_conflict` / `precondition_required` keep their existing keys
  (`error`, `path`, `expected_revision`, `actual_revision`).
- `list_folder_tool(recursive=true, max_depth=N)` marks folders cut by the
  depth limit with `truncated: true` and `children_count`, so they are no
  longer indistinguishable from empty folders.
- `get_tasks_tool`: `limit` (default 100, max 1000), `offset`, `dedupe`,
  `group_by`, `exclude_folders`, `path_glob`, `summary_only`; `meta` now
  carries `total`, `returned`, `truncated` and `offset`.
- `TAGS_SOURCE=frontmatter|inline|both` (default `both`).
- `get_broken_links_tool` items carry `kind` and `suggestion`; ambiguous
  names are reported in `meta.warnings`.
- `TOOLS_ALLOWLIST` / `TOOLS_DENYLIST` trim the exposed tool surface after
  every registration (including the `ob` tools); hidden tools are
  unregistered, unknown names only log a warning. `ENABLE_PROMPTS` /
  `ENABLE_RESOURCES` (default `true`) drop the generic prompts and the
  `vault://` resources. Defaults keep the previous surface.
- Verb-style tools: `get_outline_tool` (headings with file lines),
  `get_outgoing_links_tool` (resolved/broken links and embeds of a note),
  `prepend_to_note_tool` (insert at the top of the body, after frontmatter
  and a leading H1), `set_task_status_tool` (check/uncheck one task line,
  refused on a stale line or text mismatch) and `get_recent_notes_tool`.
- `read_note_tool` and `get_backlinks_tool` accept a bare note name,
  resolved like a wikilink (`meta.resolved_from`, `meta.candidates`).
- `deploy/env.example` ships an annotated production `TOOLS_ALLOWLIST`
  (27 tools) with prompts and resources off.
- `copy_note_tool`: copy a note on the server (exact bytes, create-only,
  `dry_run`); `data.name_conflicts` flags notes that share the new name.
- `batch_edit_tool`: up to 50 edits (`replace`, `section`, `append`,
  `prepend`, `write`) over one or more notes, applied in order and all or
  nothing. Every edit is checked before anything is written, and notes
  already written are restored if a later write fails. Per-edit
  `expected_revision`, `dry_run`, diffs capped at 4000 characters.
- `move_note_tool` also rewrites Markdown links and embeds to the moved note
  (`[text](My%20note.md)`, `[text](<My note.md>)`, raw balanced parentheses),
  keeping each link's style: encoding, `<>`, relative or full path, `.md` or
  not, `#heading`/`#^block` and `"title"`. The moved note's own relative links
  are fixed for its new folder. Links in code, `%%` comments and (for Markdown
  links) frontmatter are left alone, wikilinks in code included; `[ref]: path`
  definitions are reported in `data.reference_links_not_updated`.
- `move_note_tool` gained `dry_run` (`data.links` with counts per note),
  rewrites the moved note's own self-links, `[[note.md]]` and table-escaped
  `[[note\|alias]]`, and refuses a new name another note already uses (or an
  old name shared with another note, whose bare links would be ambiguous).
- Every tool declares MCP tool annotations (`readOnlyHint`,
  `destructiveHint`, `idempotentHint`, `openWorldHint=false`) from one table
  in `server.py`; a tool missing from it fails at import. `patch_note_tool`,
  `move_note_tool`, `copy_note_tool` and `batch_edit_tool` also have titles.
- `DELETE_MODE=trash|permanent` (default `trash`): what `delete_tool` does
  when the caller omits `trash`. `permanent` needs `ALLOW_PERMANENT_DELETE`.
- Production (`deploy/env.example`) enables every tool group (delete with
  permanent deletion by default (`DELETE_MODE=permanent`), move, folder rename, bulk replace, canvas, excalidraw,
  kanban, bases, every `ob` tier) with no `TOOLS_ALLOWLIST` filter, and turns
  prompts and resources on.
- Production (`deploy/env.example`): `ENABLE_MOVE=true`, and `patch_note`,
  `batch_edit`, `move_note` and `copy_note` are allowlisted.
- Inline Markdown links and embeds (`[text](dest)`, `![alt](dest)`) count as
  links: backlinks, orphans, broken links, link graph and vault stats now see
  them (percent-encoded, `<...>` and `./`/`../` relative destinations; URLs
  and links in code are ignored). `get_outgoing_links_tool` and
  `get_broken_links_tool` items gain `format: wikilink|markdown`.

### Changed

- **Breaking:** `get_vault_stats_tool` / `vault://stats` `most_linked` is now
  `[{path, backlinks}]` instead of bare paths, and lists only notes with at
  least one backlink. Bare paths were easy to misread in vaults with numbered
  folders such as `(300) Permanent`.
- `search_notes_tool` returns tighter snippets: new `max_snippets_per_note`
  (default 2, was a fixed 3) and `context_lines` (default 1, was a fixed 2);
  overlapping windows are merged and lines are clipped to 240 characters
  around the match. The score now sums over every matching line. Exact and
  fuzzy matching ignore accents as well as case (`Siplan` finds `Siplán`).
- `sync_logs` drops `ob` heartbeat lines by default (`exclude_noise=true`,
  reporting `noise_lines_skipped` and `last_fully_synced_at`), accepts a
  case-insensitive substring `filter`, and collapses consecutive identical
  lines (`collapse_repeats=true`). `lines` counts lines after filtering.
- The server instructions are short and convention-neutral: no tool
  reference, example frontmatter or tag advice; they point to
  `get_vault_conventions_tool`, keep the revision/precondition guidance and
  mention periodic notes/templates only when configured. Long tool
  docstrings were trimmed.
- Task `line` numbers (`get_tasks_tool`, `read_note_tool`) are 1-based file
  lines; they used to count from the end of the frontmatter.
- `read_note_tool` on a missing note now says `Note not found: '<path>'`.
- FastMCP Python clients: because tools now declare a typed output schema,
  `CallToolResult.data` is a generated object (attribute access) instead of
  a dict; `structured_content` is the unchanged dict.

### Fixed

- `patch_note_text_tool` and `find_replace_in_vault_tool` in `exact` mode
  now insert the replacement literally; a backslash in it (`C:\path`) used
  to raise an error or be rewritten as a regex escape.
- `move_note_tool` is all or nothing: it used to rewrite the links first and
  move after, so a failed move left links pointing to a missing note. It now
  also rewrites folder-qualified links (`[[Folder/note]]`) and skips notes
  whose links don't change.
- Section edits accept the heading with its `#` marks (`## Ideas`).
- Broken links: attachments and partial paths resolve by file name across
  the whole vault, `[[Foo.excalidraw]]` resolves to `Foo.excalidraw.md`, and
  links in fenced code (``` and ~~~), inline code, `%% comments %%` and
  data such as `[[1,320,320,3]]` are ignored. `resolve_alias_tool` resolves
  attachment names.
- Tags: URL fragments, Markdown link targets, numeric-only tags (`#14/`), hex
  colors and hex identifiers are no longer tags; `#` must follow whitespace
  or start a line, so `[#todo]` is no longer a tag (Obsidian behaviour).
- Tasks: `*`, `+` and numbered bullets and tasks inside callouts are parsed;
  tasks in code blocks and comments are ignored.
- Note outlines (`read_note_tool(mode="outline")`) no longer report `#`
  lines inside code blocks, comments or frontmatter as headings.


## [2.1.2] - 2026-11-01

### Fixed

- Heading link anchors (`[[Note#Heading]]`, `![[Note#Heading]]`) were extracted
  as inline tags, flooding `list_all_tags_tool` with the first words of
  headings. Wikilinks and embeds are now ignored when scanning for inline tags
  ([#11](https://github.com/ykoellmann/obsidian-mcp/issues/11),
  [#12](https://github.com/ykoellmann/obsidian-mcp/pull/12)).
- A tag in single brackets (`[#todo]`) is now recognised as a tag.

## [2.1.1] - 2026-10-08

### Fixed

- `get_audit_log_tool` advertised a wrapped array `outputSchema` (from a stale
  `-> list[dict]` annotation) while returning the standard envelope, so clients
  that validate structured content, such as Claude Code, rejected every call
  ([#10](https://github.com/ykoellmann/obsidian-mcp/issues/10)).

## [2.1.0] - 2026-09-16

### Added — per-identity path policy overrides

`identities[].vaults` in `vaults.json` now accepts an object mapping a vault
name to a policy override (`read_paths`, `write_paths`, `deny_read_paths`,
`deny_write_paths`, `read_only`) in addition to the existing array of vault
names, which keeps working unchanged and means "inherit each vault's policy".

An override can only restrict: allowlists intersect (a rule outside the
vault's own scope fails the server at startup), denylists union, and
`read_only` ORs toward `true`. The vault index and file watcher stay
vault-wide, so read scoping is applied per call when a tool formats its
response — backlinks, orphans, broken links, the link graph, tag tree,
`list_all_tags_tool`, tasks, vault stats, `query_notes_tool`,
`resolve_alias_tool`, `lint_schema_tool` and `find_similar_notes_tool` can no
longer name (or traverse through) a note outside the caller's read scope. See
[Per-Identity Path Policy](README.md#per-identity-path-policy).

## [2.0.0] — Tool Surface v2

A breaking release. **Every tool's response shape changed**, and 11 tools were
removed or merged into others. With all optional groups enabled the tool count
drops from 65 to 56. Clients that parse tool results or call the removed tools
need updating; the parameters of the tools that remain are unchanged except
where noted.

### Changed — every response now uses one envelope

Tools used to each return their own ad-hoc shape: a dict here, a bare list
there, a bare string from `render_note_tool`, and a `status: "written"` string
that callers compared against. They now all return one object, built through
the shared constructors in the new `obsidian_mcp.envelope` module:

```json
{
  "success": true,
  "path": "02-Areas/monari/ticket-123.md",
  "revision": "sha256:ab12…",
  "data": { "...": "tool-specific payload" },
  "meta": { "...": "action, dry_run, count, truncated, …" }
}
```

- `success` is always present and always a boolean. The `status` string is gone
  from the tool surface; a write's verb moved to `meta.action`, and a dry run
  additionally sets `meta.dry_run`.
- `path` is present for single-item results, omitted for vault-wide and list
  results.
- `data` is always present. Listings moved their rows into `data.items`, with
  `meta.count` and, where applicable, `meta.truncated`.
- `revision` is returned by every read and every successful single-file write.
- Batch results report per-item outcomes in `data.results`
  (`{success, path, revision}` or `{success: false, path, error}`, where
  `error.type` is the raised exception's class name) and tallies in
  `data.summary`. Top-level `success` only reports that the batch ran — check
  `data.summary.failed`.
- Hard failures (bad path, permission denied, revision conflict) still arrive
  as MCP errors. `success: false` is used only for per-item batch failures.

### Added

- **Optimistic concurrency.** Every write tool accepts an optional
  `expected_revision`. Pass the `revision` from a previous read to pin the
  write to those exact bytes; if the file changed in between, the write fails
  with a distinguishable `revision_conflict` error instead of silently
  discarding the other change. `create_only` requires the target to be absent,
  committed race-free via `linkat`.
- **Index reconciliation.** A periodic full content-revision sweep repairs
  index drift from edits the watcher missed (Obsidian Sync, git checkouts).
  Telemetry is exposed on `/health`. Watcher events are debounced per path.
- New configuration: `REQUIRE_WRITE_PRECONDITIONS` (require
  `expected_revision` on every overwrite), `INDEX_RECONCILE_INTERVAL`,
  `WATCHER_DEBOUNCE_MS`.
- `patch_note_tool` gained `dry_run`, which it lacked while the other patch and
  write tools had it.

### Removed — tools that duplicated another tool

| Removed | Use instead |
| --- | --- |
| `get_notes_by_tag_tool` | `query_notes_tool(tags=[...])` |
| `get_daily_note_tool` | `get_periodic_note_tool(period="daily")` |
| `get_note_history_tool` | `get_audit_log_tool(path=...)` |

### Changed — tools merged behind a parameter

| Removed | Merged into |
| --- | --- |
| `get_note_outline_tool`, `render_note_tool` | `read_note_tool(path, mode="full" \| "outline" \| "rendered", depth=1)` |
| `get_tag_tree_tool` | `list_all_tags_tool(sort_by, mode="flat" \| "tree")` |
| `patch_frontmatter_batch_tool` | `patch_frontmatter_tool(path=... \| paths=[...])` |
| `delete_note_tool`, `delete_folder_tool` | `delete_tool(path, trash=True)` |
| `restore_note_tool`, `restore_folder_tool` | `restore_tool(trashed_name, to_path)` |

Notes on the merges:

- `read_note_tool`'s `depth` applies to `mode="rendered"` only. Modes `full`
  and `outline` carry a `revision`; a rendered read spans several notes and so
  pins nothing.
- `patch_frontmatter_tool(paths=[...])` applies **one** set of `updates` to
  every listed note. The old batch tool's per-entry `updates` dicts have no
  equivalent — varying updates per note now means one call per distinct set.
  `expected_revision` is rejected alongside `paths`, since one token can only
  pin one note.
- `delete_tool` and `restore_tool` dispatch on what the path actually is, so
  callers no longer need to know whether a target is a file or a directory.
  `meta.kind` reports which branch ran. `expected_revision` is rejected for a
  folder, which has no single revision. Both remain behind `ENABLE_DELETE`,
  which now registers `delete_tool`, `restore_tool` and `list_trash_tool`.
- An unknown `mode` raises a `ValueError` naming the valid values rather than
  silently falling back to a default.

### Unchanged on purpose

`list_notes_tool` / `list_folder_tool` / `list_files_tool` stay separate
(different result shapes), as do `patch_note_tool` / `patch_note_text_tool` /
`append_to_note_tool` (distinct write intentions) and the graph tools
(incompatible signatures). `manage_tags_tool` is not folded into
`patch_frontmatter_tool` because it also strips inline body tags.

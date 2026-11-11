# Tool Reference

Every tool returns one JSON object — the [response envelope](response-envelope.md)
— never a bare list or string. Full parameter documentation is embedded in
the server and shown automatically to connected AI clients; this page is an
index of what exists and where to look for more.

| Category | Tools |
|---|---|
| **Read** | `list_notes`, `list_files`, `read_note` (mode: `full`/`outline`/`rendered`; path or bare note name), `get_outline`, `get_recent_notes`, `search_notes`, `find_similar_notes` |
| **Write** | `write_note`, `patch_note`, `patch_note_text`, `append_to_note`, `prepend_to_note`, `set_task_status`, `patch_frontmatter` (one note via `path=` or many via `paths=`), `manage_tags`, `batch_edit` (several edits on one or more notes, all or nothing), `copy_note`, `move_note`† (rewrites links; all or nothing; `dry_run`), `find_replace_in_vault`† |
| **Delete** | `delete`* (`trash=True` dispatches on file vs. folder), `restore`*, `list_trash`* |
| **Folders** | `list_folder`, `create_folder`, `rename_folder`† |
| **Query** | `query_notes`, `get_backlinks`, `get_outgoing_links`, `get_broken_links`, `get_orphans`, `get_link_graph`, `get_vault_stats`, `get_tasks`, `resolve_alias` |
| **Tags** | `list_all_tags` (mode: `flat`/`tree`; notes for one tag: `query_notes(tags=[...])`) |
| **Periodic** | `get_periodic_note` (period: `daily`/`weekly`/`monthly`/`quarterly`/`yearly`) |
| **Schema & Audit** | `lint_schema`, `get_vault_conventions`, `get_audit_log` |
| **Multi-vault** | `list_vaults` |
| **Canvas** ‡ | `list_canvases`, `read_canvas`, `write_canvas`, `patch_canvas` |
| **Excalidraw** ‡ | `list_excalidraw`, `read_excalidraw`, `write_excalidraw`, `patch_excalidraw` |
| **Kanban** ‡ | `read_kanban`, `create_kanban_board`, `add_kanban_card`, `move_kanban_card`, `delete_kanban_card` |
| **Bases** ‡ | `list_bases`, `read_base`, `write_base`, `patch_base` |
| **Attachments** | `list_attachments`, `read_attachment`, `add_attachment`, `create_attachment_token` |
| **Templates** | `list_templates`, `create_from_template` |

\* Registered only when `ENABLE_DELETE=true`.
† Registered only when its own flag is set: `move_note` needs `ENABLE_MOVE`,
`rename_folder` needs `ENABLE_FOLDER_RENAME`, `find_replace_in_vault` needs
`ENABLE_BULK_REPLACE`. See [Configuration](configuration.md#optional-tool-groups).
‡ Each format is opt-in via its own `ENABLE_*` flag — see
[Configuration](configuration.md#optional-tool-groups). A disabled group's
tools aren't just refused at call time, they're never registered, so they
don't appear in a connected client's tool list at all.

Every tool name above carries a `_tool` suffix on the wire (`read_note_tool`),
except the `ob` sync tools (`sync_status`, ...). An operator can hide any of
them with `TOOLS_ALLOWLIST` / `TOOLS_DENYLIST`, and drop the prompts and
resources below with `ENABLE_PROMPTS` / `ENABLE_RESOURCES` — see
[Configuration](configuration.md#exposed-tool-surface).

## Verb-style tools

These mirror an Obsidian-CLI style workflow (`read`, `outline`, `links`,
`prepend`, `task ... done`, `recents`):

- `read_note`, `get_outline`, `get_outgoing_links` and `get_backlinks` take a
  vault path (`Folder/Note.md`) or a bare note name (`Note`), resolved like a
  `[[wikilink]]`: case-insensitive, alias-aware, the shortest path winning
  when several notes share a name. `meta.resolved_from` and
  `meta.candidates` say when a name was resolved. Write tools always take an
  exact path.
- `get_outline` — headings `[{level, text, line}]` with 1-based file lines;
  headings inside frontmatter, code blocks or `%%` comments are ignored.
- `get_outgoing_links` — one item per distinct link or embed:
  `{target, heading, embed, kind: note|attachment, resolved, count}`;
  `resolved: null` means broken (or outside the caller's read scope).
- `get_recent_notes(limit=20, folder="")` — notes by modification time,
  newest first, skipping `.obsidian/`, `.trash/` and `EXCLUDE_PATHS`.
- `prepend_to_note(path, content)` — inserts `content` verbatim at the top
  of the body: after the frontmatter (and the blank lines after it) and
  after a leading `# Title` line, otherwise at line 1. On a newest-on-top
  log (`## 9 Nov` above `## 6 Nov`) that is exactly where a new day goes.
  Like `append_to_note` it locks the file, honours `expected_revision`,
  write policy and the audit log, and creates a missing note unless
  `create=false`.
- `set_task_status(path, line, done, expected_text)` — swaps only the
  checkbox of the task on that file line. It is refused when the line is
  not a task (or sits in code) or its text no longer matches
  `expected_text` (whitespace/case-insensitive; the clean text from
  `get_tasks` or the raw text both work), which protects against stale line
  numbers. Setting the state a task already has writes nothing
  (`meta.action: unchanged`). No Tasks-plugin `✅` date is added.

## Links, tags and tasks

- `get_broken_links` resolves links like Obsidian: case-insensitive, an
  attachment (`![[diagram.png]]`) or partial path (`[[images/diagram.png]]`)
  matches by file name anywhere in the vault, `[[Foo.excalidraw]]` finds
  `Foo.excalidraw.md`, and links inside code or `%% comments %%` or that are
  really data (`[[1,320,320,3]]`) are ignored. Each item is
  `{source, link, kind: note|attachment, suggestion}`; `meta.warnings` lists
  bare names that match several files. `get_vault_stats` counts the same set,
  and `resolve_alias` accepts attachment names too.
- `get_tasks` is paged: `limit` (default 100, max 1000), `offset`, and
  `meta.{total, returned, truncated, offset}`. `dedupe=true` collapses a task
  repeated in one note (e.g. a daily worklog) into its last occurrence with
  `occurrences`, `first_line`, `last_line`. Also: `group_by` (`none`/`source`),
  `exclude_folders`, `path_glob`, `summary_only` (per-note open/done/total).
  Bullets `-`, `*`, `+` and `1.`, nested or in callouts, are recognised.
  A task's `line` is its 1-based line in the file (frontmatter included),
  ready for `set_task_status`.
- Tag listing follows Obsidian's inline-tag rules and the `TAGS_SOURCE`
  setting — see [Configuration](configuration.md#tags).

## MCP Resources

- `vault://notes/{path}` — a note's content as a resource
- `vault://stats` — vault statistics
- `vault://tags` — the tag tree

## MCP Prompts

- `weekly_review`, `daily_note` — starting points for common workflows

## Output schema and error codes

Every tool advertises the same `outputSchema`: the response envelope
`{success, path?, revision?, data, meta?}`. `data` and `meta` are free-form
objects (their contents are described in each tool's description); a
listing's rows are in `data.items`.

A failed call is an MCP error result (`isError: true`). Its text content is
the human-readable message, and its structured content still fits the
schema: `{success: false, data: {}, code, message}`. Branch on `code`, which
is stable:

| `code` | Meaning |
|---|---|
| `invalid_argument` | Bad path, value, file type, regex or arguments |
| `not_found` | The note, file or folder does not exist |
| `already_exists` | The target path is already taken |
| `permission_denied` | Outside the readable/writable part of the vault (policy, `READ_ONLY`, vault access) |
| `permanent_delete_disabled` | Permanent deletion asked but `ALLOW_PERMANENT_DELETE` is off |
| `revision_conflict` | `expected_revision` no longer matches; also has `error`, `path`, `expected_revision`, `actual_revision` |
| `precondition_required` | Strict mode: overwriting needs `expected_revision`; also has `error`, `path` |
| `unavailable` | Transient, retry later (e.g. the index is still building) |
| `tool_error` | A deliberate tool error with no more specific code (e.g. the `ob` tools) |
| `internal` | Unexpected failure; with `MASK_ERROR_DETAILS` the message is generic |

## Optimistic concurrency

Every read returns a `revision` (`sha256:...`). Pass it back as
`expected_revision` on a write to pin the edit to the exact bytes you read;
if the file changed in between, the write fails with a `revision_conflict`
error instead of silently discarding the other change. `write_note_tool`
also accepts `create_only=true`, which requires the target to be absent and
is committed race-free. See [Response Envelope](response-envelope.md) and
[Configuration](configuration.md#write-preconditions) for
`REQUIRE_WRITE_PRECONDITIONS`.

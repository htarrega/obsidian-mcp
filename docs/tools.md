# Tool Reference

Every tool returns one JSON object — the [response envelope](response-envelope.md)
— never a bare list or string. Full parameter documentation is embedded in
the server and shown automatically to connected AI clients; this page is an
index of what exists and where to look for more.

| Category | Tools |
|---|---|
| **Read** | `list_notes`, `list_files`, `read_note` (mode: `full`/`outline`/`rendered`), `search_notes`, `find_similar_notes` |
| **Write** | `write_note`, `patch_note`, `patch_note_text`, `append_to_note`, `patch_frontmatter` (one note via `path=` or many via `paths=`), `manage_tags`, `move_note`†, `find_replace_in_vault`† |
| **Delete** | `delete`* (`trash=True` dispatches on file vs. folder), `restore`*, `list_trash`* |
| **Folders** | `list_folder`, `create_folder`, `rename_folder`† |
| **Query** | `query_notes`, `get_backlinks`, `get_broken_links`, `get_orphans`, `get_link_graph`, `get_vault_stats`, `get_tasks`, `resolve_alias` |
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
- Tag listing follows Obsidian's inline-tag rules and the `TAGS_SOURCE`
  setting — see [Configuration](configuration.md#tags).

## MCP Resources

- `vault://notes/{path}` — a note's content as a resource
- `vault://stats` — vault statistics
- `vault://tags` — the tag tree

## MCP Prompts

- `weekly_review`, `daily_note` — starting points for common workflows

## Optimistic concurrency

Every read returns a `revision` (`sha256:...`). Pass it back as
`expected_revision` on a write to pin the edit to the exact bytes you read;
if the file changed in between, the write fails with a `revision_conflict`
error instead of silently discarding the other change. `write_note_tool`
also accepts `create_only=true`, which requires the target to be absent and
is committed race-free. See [Response Envelope](response-envelope.md) and
[Configuration](configuration.md#write-preconditions) for
`REQUIRE_WRITE_PRECONDITIONS`.

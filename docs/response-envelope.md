# Response Envelope

Every tool returns one JSON object, built through shared constructors in
`obsidian_mcp.envelope` — never a bare list, string, or ad-hoc dict:

```json
{
  "success": true,
  "path": "02-Areas/monari/ticket-123.md",
  "revision": "sha256:ab12…",
  "data": { "...": "tool-specific payload" },
  "meta": { "...": "action, dry_run, count, truncated, …" }
}
```

- `success` is always present and always a boolean.
- `path` is present for single-item results, omitted for vault-wide and list
  results that aren't addressed by one path.
- `revision` is present on every read and every successful single-file
  write — feed it back as `expected_revision` to pin a later write (see
  [Tool Reference](tools.md#optimistic-concurrency)).
- `data` is always present, so clients can index into it unconditionally.
  Listings put their rows in `data.items`, with `meta.count` and, where
  applicable, `meta.truncated`.
- `meta` is present only when there's something to say. A write's verb lives
  in `meta.action`; a dry run additionally sets `meta.dry_run`.

## Batch results

`patch_frontmatter_tool(paths=[...])` and similar multi-target calls report
per-item outcomes in `data.results` — each entry is either
`{success: true, path, revision}` or `{success: false, path, error}`, where
`error.type` is the raised exception's class name — plus tallies in
`data.summary`. The top-level `success` only reports that the batch ran;
check `data.summary.failed` for partial failure.

## Hard failures

Hard failures (bad path, permission denied, revision conflict) are MCP
error results (`isError: true`), never a successful envelope. Their
structured content is `{success: false, data: {}, code, message}` with a
stable `code` — see [Output schema and error codes](tools.md#output-schema-and-error-codes).
Inside a batch, per-item failures are in-band (`success: false` on the
item), because some items in a batch may have succeeded while others
failed.

See [CHANGELOG.md](../CHANGELOG.md) for the full list of tools removed or
merged when this envelope was introduced in 2.0.0.

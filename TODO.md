# TODO

Changes picked by Pareto's law: the few that shrink the tool surface a model
has to read and choose from.

## 1. Add a `core` tool profile

A default install advertises 40 tools (about 80 with every group enabled),
but a handful do nearly all the work. `TOOLS_ALLOWLIST` already filters the
surface, but the operator has to type every name by hand.

- [ ] Add `TOOLS_PROFILE` (e.g. `core`, `full`; default `full`) read in
      `_ExposureSettings.from_env` and applied in `_apply_exposure_filters`
      (`src/obsidian_mcp/server.py`).
- [ ] Define the `core` set, about 10 tools: `read_note_tool`,
      `search_notes_tool`, `list_folder_tool`, `write_note_tool`,
      `patch_note_tool`, `patch_note_text_tool`, `append_to_note_tool`,
      `query_notes_tool`, `get_backlinks_tool`, `get_vault_conventions_tool`.
- [ ] Decide how it combines with `TOOLS_ALLOWLIST` / `TOOLS_DENYLIST`
      (suggested: the profile is the base set, the lists then add/remove).
- [ ] Document it in `docs/configuration.md` and `.env.example`; add tests
      next to `tests/test_server_exposure.py`.

## 2. Reconsider the per-tool output schema

Measured on a default install, `tools/list` is about 80k characters (roughly
20k tokens). 54% of it is the same 1,093-character envelope schema repeated
on all 40 tools (`output_schema=envelope_output_schema()` in `_tool`, added
in 1cb592f). Names, descriptions and input schemas are about 9k tokens.

- [ ] Check which clients forward `outputSchema` to the model (Claude
      clients are believed not to; unverified).
- [ ] Decide: keep as is, make it opt-out (e.g. `ADVERTISE_OUTPUT_SCHEMA`),
      or drop it and keep the envelope documented in
      `docs/response-envelope.md` only.

## 3. Merge overlapping tools (next major version)

Breaking change, so bundle it with a major bump.

- [ ] `list_notes_tool` / `list_files_tool` / `list_folder_tool` into one
      listing tool with parameters.
- [ ] `append_to_note_tool` / `prepend_to_note_tool` into one tool with a
      `position` parameter.
- [ ] Drop the redundant `_tool` suffix from every tool name
      (`read_note_tool` -> `read_note`), including the names in
      `TOOLS_ALLOWLIST` / `TOOLS_DENYLIST` and the `core` profile.
- [ ] Update `_TOOL_ANNOTATIONS`, `docs/tools.md`, the server instructions
      and `CHANGELOG.md`.

"""MCP tools for the ob sync supervisor, in three tiers.

* read-only (``ENABLE_OB``): ``sync_status``, ``sync_logs``, ``ob_info``,
  ``list_remote_vaults``;
* ops (``ENABLE_OB_OPS``): ``sync_restart``;
* admin (``ENABLE_OB_ADMIN`` and not ``READ_ONLY``): ``sync_set_*``.

Deliberately never exposed: ``login``, ``logout``, ``sync-setup``,
``sync-create-remote``, ``sync-unlink``, ``publish-*``, ``--mode``,
``--config-dir``, ``--device-name``, and any free-form command. The vault
path always comes from configuration, never from the caller.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..envelope import envelope_output_schema, read_result
from .redact import redact, redact_obj
from .runner import ObCommandError, ObError
from .service import ObService, get_service
from .settings import ObFlags, auth_token_file
from .supervisor import SupervisorBusyError
from .validate import (
    CONFIG_CATEGORIES,
    FILE_TYPES,
    ObValidationError,
    conflict_strategy_value,
    enum_list_value,
    excluded_folders_value,
)

FileType = Literal["image", "audio", "video", "pdf", "unsupported"]
ConfigCategory = Literal[
    "app",
    "appearance",
    "appearance-data",
    "hotkey",
    "core-plugin",
    "core-plugin-data",
    "community-plugin",
    "community-plugin-data",
]
ConflictStrategy = Literal["merge", "conflict"]

READ_ONLY_TOOLS = ("sync_status", "sync_logs", "ob_info", "list_remote_vaults")
OPS_TOOLS = ("sync_restart",)
ADMIN_TOOLS = (
    "sync_set_excluded_folders",
    "sync_set_file_types",
    "sync_set_configs",
    "sync_set_conflict_strategy",
)


def _service() -> ObService:
    service = get_service()
    if service is None:
        raise ToolError("ob sync supervisor is not running (server not started with ENABLE_OB)")
    return service


def _sync_config(service: ObService) -> tuple[dict[str, Any] | None, str | None]:
    """``ob sync-status --path V --json`` → (config, error message)."""
    try:
        data = service.runner.run(
            ["sync-status", "--path", service.runner.vault_arg, "--json"]
        ).json()
    except ObError as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "ob sync-status returned unexpected JSON"
    return data, None


# ── read-only tier ────────────────────────────────────────────────────────


def sync_status() -> dict:
    """Status of Obsidian Sync for the configured vault.

    Returns the supervisor state of the background `ob sync --continuous`
    process (state running/starting/backoff/failed/stopped, pid, uptime,
    restart count, last exit code, last "Fully synced" time, recent errors)
    plus the vault's sync configuration from `ob sync-status` (vault name,
    sync mode, conflict strategy, device name, file types, config categories,
    excluded folders). Secrets are never included.
    """
    service = _service()
    config, error = _sync_config(service)
    data: dict[str, Any] = {"supervisor": service.supervisor.status(), "config": config}
    if error:
        data["config_error"] = error
    return read_result(None, data)


# Routine heartbeat lines `ob sync --continuous` prints on every poll (about
# every 30 s). Compared case-insensitively against the message with trailing
# dots/spaces stripped, so only these exact messages are treated as noise.
_NOISE_MESSAGES = frozenset(
    {
        "fully synced",
        "connecting",
        "connected",
        "connection successful",
        "connection successful. detecting changes",
        "detecting changes",
        "waiting for changes",
    }
)
LOG_FILTER_MAX_CHARS = 200


def _split_stamp(line: str) -> tuple[str, str]:
    """``"<iso-ts> message"`` → (ts, message); ts is "" if absent."""
    stamp, sep, message = line.partition(" ")
    if sep and stamp[:4].isdigit() and "T" in stamp:
        return stamp, message
    return "", line


def _is_noise(message: str) -> bool:
    return message.strip().rstrip(". ").lower() in _NOISE_MESSAGES


def _log_filter_value(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ToolError("filter must be a string")
    if len(value) > LOG_FILTER_MAX_CHARS:
        raise ToolError(f"filter is longer than {LOG_FILTER_MAX_CHARS} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ToolError("filter must not contain control characters")
    return value.lower()


def _collapse_repeats(lines: list[str]) -> tuple[list[str], int]:
    """Fold runs of identical messages into one line with a repeat marker."""
    out: list[str] = []
    collapsed = 0
    run_first = run_last = ""
    run_message: str | None = None
    run_count = 0

    def flush() -> None:
        if run_message is None:
            return
        if run_count == 1:
            out.append(run_first)
        else:
            stamp, _ = _split_stamp(run_last)
            suffix = f", last at {stamp}" if stamp else ""
            out.append(f"{run_first} [repeated {run_count}x{suffix}]")

    for line in lines:
        _, message = _split_stamp(line)
        if message == run_message:
            run_count += 1
            run_last = line
            collapsed += 1
            continue
        flush()
        run_first = run_last = line
        run_message = message
        run_count = 1
    flush()
    return out, collapsed


def sync_logs(
    lines: int = 100,
    exclude_noise: bool = True,
    filter: str | None = None,
    collapse_repeats: bool = True,
) -> dict:
    """Most recent output lines of the background `ob sync` process.

    lines: how many lines to return (clamped to 1..OB_LOG_LINES), taken from
    the end after filtering. Lines are timestamped and redacted.
    exclude_noise (default true): drop routine heartbeat lines ("Fully
    synced", "Connecting...", "Connection successful. Detecting changes...");
    data then reports noise_lines_skipped and last_fully_synced_at.
    filter: case-insensitive substring (not a regex) a line must contain.
    collapse_repeats (default true): fold consecutive identical messages into
    one line ending in "[repeated Nx, last at <ts>]".
    """
    service = _service()
    limit = max(1, min(int(lines), service.settings.log_lines))
    needle = _log_filter_value(filter)
    buffered = service.supervisor.logs(service.settings.log_lines)

    kept: list[str] = []
    noise_skipped = 0
    for line in buffered:
        if exclude_noise and _is_noise(_split_stamp(line)[1]):
            noise_skipped += 1
            continue
        if needle is not None and needle not in line.lower():
            continue
        kept.append(line)
    collapsed = 0
    if collapse_repeats:
        kept, collapsed = _collapse_repeats(kept)
    items = kept[-limit:]

    data: dict[str, Any] = {"lines": items}
    if exclude_noise:
        data["noise_lines_skipped"] = noise_skipped
        data["last_fully_synced_at"] = service.supervisor.status().get("last_fully_synced_at")
    meta: dict[str, Any] = {"count": len(items), "limit": limit, "buffered": len(buffered)}
    if collapse_repeats:
        meta["collapsed"] = collapsed
    if needle is not None:
        meta["filter"] = redact(filter)
    return read_result(None, data, meta=meta)


def ob_info() -> dict:
    """Information about the local Obsidian Headless (`ob`) installation.

    Returns the `ob` version, this vault's entry from `ob sync-list-local`,
    the configured device name, and whether an auth token is present (a
    boolean only — the token itself is never read or returned). Makes no
    network calls.
    """
    service = _service()
    errors: dict[str, str] = {}

    version: str | None = None
    try:
        version = service.runner.run(["--version"]).stdout.strip() or None
    except ObError as exc:
        errors["version"] = str(exc)

    local_entry: dict[str, Any] | None = None
    try:
        result = service.runner.run(["sync-list-local", "--json"])
        raw = result.unredacted_json()
        vaults = raw.get("vaults", []) if isinstance(raw, dict) else []
        for entry in vaults:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            try:
                same = Path(entry["path"]).expanduser().resolve() == service.settings.vault_path
            except OSError:
                same = False
            if same:
                local_entry = redact_obj(entry)
                break
    except ObError as exc:
        errors["sync_list_local"] = str(exc)

    config, config_error = _sync_config(service)
    if config_error:
        errors["sync_status"] = config_error

    data: dict[str, Any] = {
        "version": version,
        "vault_path": str(service.settings.vault_path),
        "local_vault": local_entry,
        "device_name": (config or {}).get("deviceName"),
        "auth_token_file_present": auth_token_file().is_file(),
        "auth_token_env_set": bool(os.environ.get("OBSIDIAN_AUTH_TOKEN")),
    }
    if errors:
        data["errors"] = errors
    return read_result(None, data)


def list_remote_vaults() -> dict:
    """List the remote Obsidian Sync vaults of the logged-in account.

    Runs `ob sync-list-remote` (network call). Returns the account's own
    vaults (id, name, region) and vaults shared with it.
    """
    service = _service()
    try:
        data = service.runner.run(["sync-list-remote", "--json"]).json()
    except ObError as exc:
        raise ToolError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ToolError("ob sync-list-remote returned unexpected JSON")
    return read_result(None, data)


# ── ops tier ──────────────────────────────────────────────────────────────


def sync_restart() -> dict:
    """Restart the background `ob sync --continuous` process.

    Sends SIGTERM to the running sync process, waits for it to exit (SIGKILL
    after ~10s), launches a fresh one and returns the new supervisor status.
    Does not change any sync configuration. Refused if the process was
    (re)started less than OB_RESTART_COOLDOWN seconds ago.
    """
    service = _service()
    try:
        service.supervisor.restart()
    except SupervisorBusyError as exc:
        raise ToolError(str(exc)) from exc
    return read_result(None, {"supervisor": service.supervisor.status()}, meta={"action": "restarted"})


# ── admin tier ────────────────────────────────────────────────────────────


def _apply_sync_config(flag: str, value: str, setting: str) -> dict:
    service = _service()
    args = ["sync-config", "--path", service.runner.vault_arg, flag, value, "--json"]
    output: Any = None
    try:
        with service.supervisor.paused():
            try:
                result = service.runner.run(args)
            except ObError as exc:
                raise ToolError(f"sync config not changed: {exc}") from exc
            try:
                output = result.json()
            except ObCommandError:
                output = None
    except SupervisorBusyError as exc:
        raise ToolError(f"sync config not changed: {exc}") from exc
    if isinstance(output, dict) and output:
        config: dict[str, Any] | None = output
        config_error = None
    else:
        config, config_error = _sync_config(service)
    data: dict[str, Any] = {"config": config, "supervisor": service.supervisor.status()}
    if config_error:
        data["config_error"] = config_error
    return read_result(None, data, meta={"action": "sync_config_updated", "setting": setting})


def sync_set_excluded_folders(folders: list[str]) -> dict:
    """PERSISTENTLY change which vault folders Obsidian Sync excludes.

    Replaces the vault's excluded-folders list in the `ob` sync configuration
    (`ob sync-config --excluded-folders`). Files in excluded folders stop
    being uploaded/downloaded. Pass an empty list to clear all exclusions.
    Each folder must be relative to the vault root (no leading '/', no '..',
    no commas). The sync process is stopped, the config rewritten, and the
    sync process restarted. Returns the new sync configuration.
    """
    try:
        value, _ = excluded_folders_value(folders)
    except ObValidationError as exc:
        raise ToolError(str(exc)) from exc
    return _apply_sync_config("--excluded-folders", value, "excluded_folders")


def sync_set_file_types(types: list[FileType]) -> dict:
    """PERSISTENTLY change which non-note file types Obsidian Sync syncs.

    Replaces the vault's synced file-type list in the `ob` sync configuration
    (`ob sync-config --file-types`). Allowed: image, audio, video, pdf,
    unsupported. Types not listed stop syncing; an empty list clears it. The
    sync process is stopped, the config rewritten, and the sync process
    restarted. Returns the new sync configuration.
    """
    try:
        value = enum_list_value(types, FILE_TYPES, "file type")
    except ObValidationError as exc:
        raise ToolError(str(exc)) from exc
    return _apply_sync_config("--file-types", value, "file_types")


def sync_set_configs(categories: list[ConfigCategory]) -> dict:
    """PERSISTENTLY change which Obsidian settings categories Sync syncs.

    Replaces the vault's synced `.obsidian` config categories in the `ob`
    sync configuration (`ob sync-config --configs`). Allowed: app, appearance,
    appearance-data, hotkey, core-plugin, core-plugin-data, community-plugin,
    community-plugin-data. An empty list clears it (no settings synced). The
    sync process is stopped, the config rewritten, and the sync process
    restarted. Returns the new sync configuration.
    """
    try:
        value = enum_list_value(categories, CONFIG_CATEGORIES, "config category")
    except ObValidationError as exc:
        raise ToolError(str(exc)) from exc
    return _apply_sync_config("--configs", value, "configs")


def sync_set_conflict_strategy(strategy: ConflictStrategy) -> dict:
    """PERSISTENTLY change how Obsidian Sync resolves edit conflicts.

    Sets the vault's conflict strategy in the `ob` sync configuration
    (`ob sync-config --conflict-strategy`): "merge" merges concurrent edits,
    "conflict" keeps both versions as separate conflict files. The sync
    process is stopped, the config rewritten, and the sync process restarted.
    Returns the new sync configuration.
    """
    try:
        value = conflict_strategy_value(strategy)
    except ObValidationError as exc:
        raise ToolError(str(exc)) from exc
    return _apply_sync_config("--conflict-strategy", value, "conflict_strategy")


# ── registration ──────────────────────────────────────────────────────────

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_REMOTE_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
_OPS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
_ADMIN = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)


def register_ob_tools(mcp: Any, flags: ObFlags | None = None) -> list[str]:
    """Register the ob tools allowed by ``flags`` (default: from env).

    Returns the registered tool names. Registers nothing when ``ENABLE_OB``
    is false.
    """
    flags = flags or ObFlags.from_env()
    if not flags.enable_ob:
        return []
    registered: list[str] = []

    def add(fn: Any, annotations: ToolAnnotations) -> None:
        mcp.tool(annotations=annotations, output_schema=envelope_output_schema())(fn)
        registered.append(fn.__name__)

    add(sync_status, _READ_ONLY)
    add(sync_logs, _READ_ONLY)
    add(ob_info, _READ_ONLY)
    add(list_remote_vaults, _REMOTE_READ)
    if flags.enable_ops:
        add(sync_restart, _OPS)
    if flags.enable_admin:
        add(sync_set_excluded_folders, _ADMIN)
        add(sync_set_file_types, _ADMIN)
        add(sync_set_configs, _ADMIN)
        add(sync_set_conflict_strategy, _ADMIN)
    return registered


__all__ = [
    "ADMIN_TOOLS",
    "OPS_TOOLS",
    "READ_ONLY_TOOLS",
    "register_ob_tools",
]

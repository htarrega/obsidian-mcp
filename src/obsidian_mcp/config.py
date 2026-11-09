"""Load and validate single- or multi-vault environment configuration.

When ``VAULTS_CONFIG`` is set, the active vault is selected per request by
``VaultResolutionMiddleware``. Filesystem policy remains per-vault: every
property used by storage resolves through the same context variable as the
vault root.
"""

from __future__ import annotations

import contextvars
import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from .layout import LayoutError, VaultLayout, layout_from_env, layout_from_json
from .storage.policy import (
    EffectiveAccessPolicy,
    PolicyMergeError,
    VaultPathError,
    normalise_path_rules,
    path_rules_from_env,
)


class ConfigError(Exception):
    pass


def _path_rules(value: object, *, name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Normalize JSON path rules without comma-delimited env parsing."""
    if value is None:
        values = list(default)
    elif isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        values = list(value)
    else:
        raise ConfigError(f"{name} must be a list of strings")
    try:
        return normalise_path_rules(values, name=name)
    except VaultPathError as exc:
        raise ConfigError(str(exc)) from exc


@dataclass(frozen=True)
class VaultEntry:
    name: str
    path: Path
    write_paths: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    deny_read_paths: tuple[str, ...] = (".obsidian/", ".trash/")
    deny_write_paths: tuple[str, ...] = (
        ".obsidian/",
        ".trash/",
        "_AI_INSTRUCTIONS.md",
    )
    read_only: bool | None = None
    description: str = ""
    layout: VaultLayout = field(default_factory=VaultLayout)


@dataclass(frozen=True)
class PolicyOverride:
    """A per-identity narrowing of one vault's five path-policy fields.

    ``None`` means "inherit the vault's value unchanged".  An override can
    only ever restrict: allowlists intersect, denylists union, ``read_only``
    ORs toward ``True`` (see ``storage.policy`` merge helpers).
    """

    write_paths: tuple[str, ...] | None = None
    read_paths: tuple[str, ...] | None = None
    deny_read_paths: tuple[str, ...] | None = None
    deny_write_paths: tuple[str, ...] | None = None
    read_only: bool | None = None

    def is_empty(self) -> bool:
        return all(
            getattr(self, name) is None
            for name in (
                "write_paths",
                "read_paths",
                "deny_read_paths",
                "deny_write_paths",
                "read_only",
            )
        )


@dataclass(frozen=True)
class Identity:
    """One API key or GitHub login and the vaults it may access."""

    type: str
    value: str
    vaults: tuple[str, ...]
    default: str | None = None
    # Stored as pairs rather than a dict so Identity stays hashable/frozen.
    overrides: tuple[tuple[str, PolicyOverride], ...] = ()

    def override_for(self, vault_name: str) -> PolicyOverride | None:
        """The policy override this identity has for one vault, if any."""
        for name, override in self.overrides:
            if name == vault_name:
                return override
        return None


_current_vault_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_vault", default=None
)
_current_identity_var: contextvars.ContextVar[Identity | None] = contextvars.ContextVar(
    "current_identity", default=None
)


def set_current_vault(name: str | None) -> contextvars.Token:
    return _current_vault_var.set(name)


def reset_current_vault(token: contextvars.Token) -> None:
    _current_vault_var.reset(token)


def set_current_identity(identity: Identity | None) -> contextvars.Token:
    """Publish the request's resolved identity for policy resolution."""
    return _current_identity_var.set(identity)


def reset_current_identity(token: contextvars.Token) -> None:
    _current_identity_var.reset(token)


def current_identity() -> Identity | None:
    """The identity this request resolved to, or ``None`` outside a request."""
    return _current_identity_var.get()


def _override_path_rules(value: object, *, name: str) -> tuple[str, ...] | None:
    """Parse one optional override path list (absent/``None`` = inherit)."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ConfigError(f"{name} must be a list of strings")
    if not value:
        raise ConfigError(
            f"{name} must list at least one path — omit the field to inherit "
            "the vault's own value"
        )
    try:
        return normalise_path_rules(value, name=name)
    except VaultPathError as exc:
        raise ConfigError(str(exc)) from exc


_OVERRIDE_FIELDS = (
    "write_paths",
    "read_paths",
    "deny_read_paths",
    "deny_write_paths",
    "read_only",
)


def _parse_policy_override(raw: object, *, name: str) -> PolicyOverride:
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be an object of policy overrides")
    unknown = sorted(set(raw) - set(_OVERRIDE_FIELDS))
    if unknown:
        raise ConfigError(
            f"{name} has unsupported override field(s): {', '.join(unknown)} "
            f"(allowed: {', '.join(_OVERRIDE_FIELDS)})"
        )
    read_only = raw.get("read_only")
    if read_only is not None and not isinstance(read_only, bool):
        raise ConfigError(f"{name} read_only must be a boolean")
    return PolicyOverride(
        write_paths=_override_path_rules(
            raw.get("write_paths"), name=f"{name} write_paths"
        ),
        read_paths=_override_path_rules(
            raw.get("read_paths"), name=f"{name} read_paths"
        ),
        deny_read_paths=_override_path_rules(
            raw.get("deny_read_paths"), name=f"{name} deny_read_paths"
        ),
        deny_write_paths=_override_path_rules(
            raw.get("deny_write_paths"), name=f"{name} deny_write_paths"
        ),
        read_only=read_only,
    )


def _validate_override(
    vault: VaultEntry,
    override: PolicyOverride,
    *,
    identity_value: str,
    global_read_only: bool,
) -> None:
    """Run one override through the merge helpers at load time.

    A widening allowlist fails the server on boot rather than silently
    under-enforcing on the first call.
    """
    base = EffectiveAccessPolicy(
        read_only=global_read_only if vault.read_only is None else vault.read_only,
        read_paths=vault.read_paths,
        write_paths=vault.write_paths,
        deny_read_paths=vault.deny_read_paths,
        deny_write_paths=vault.deny_write_paths,
    )
    try:
        base.merged_with(override)
    except PolicyMergeError as exc:
        raise ConfigError(
            f"Identity {identity_value!r} override for vault {vault.name!r}: {exc}"
        ) from exc


def load_vaults_file(
    path_str: str, *, global_read_only: bool = False
) -> tuple[dict[str, VaultEntry], tuple[Identity, ...]]:
    """Parse and validate ``VAULTS_CONFIG`` JSON."""
    path = Path(path_str)
    if not path.is_file():
        raise ConfigError(f"VAULTS_CONFIG file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"VAULTS_CONFIG is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("VAULTS_CONFIG must contain a JSON object")

    vaults_raw = data.get("vaults") or {}
    if not isinstance(vaults_raw, dict) or not vaults_raw:
        raise ConfigError("VAULTS_CONFIG must define at least one vault under 'vaults'")

    vaults: dict[str, VaultEntry] = {}
    for name, raw_entry in vaults_raw.items():
        if not isinstance(name, str) or not name:
            raise ConfigError("Vault names in VAULTS_CONFIG must be non-empty strings")
        if not isinstance(raw_entry, dict):
            raise ConfigError(f"Vault {name!r} in VAULTS_CONFIG must be an object")
        raw_path = raw_entry.get("path", "")
        if not isinstance(raw_path, str) or not raw_path:
            raise ConfigError(f"Vault {name!r} in VAULTS_CONFIG is missing 'path'")
        vault_path = Path(raw_path).resolve()
        if not vault_path.is_dir():
            raise ConfigError(
                f"Vault {name!r} path does not exist or is not a directory: {vault_path}"
            )
        read_only = raw_entry.get("read_only")
        if read_only is not None and not isinstance(read_only, bool):
            raise ConfigError(f"Vault {name!r} read_only must be a boolean")
        try:
            layout = layout_from_json(raw_entry, vault_name=name)
        except LayoutError as exc:
            raise ConfigError(str(exc)) from exc
        vaults[name] = VaultEntry(
            name=name,
            path=vault_path,
            write_paths=_path_rules(
                raw_entry.get("write_paths"), name=f"vault {name!r} write_paths"
            ),
            read_paths=_path_rules(
                raw_entry.get("read_paths"), name=f"vault {name!r} read_paths"
            ),
            exclude_paths=_path_rules(
                raw_entry.get("exclude_paths"),
                name=f"vault {name!r} exclude_paths",
                default=("private/", ".obsidian/", ".trash/"),
            ),
            deny_read_paths=_path_rules(
                raw_entry.get("deny_read_paths"),
                name=f"vault {name!r} deny_read_paths",
                default=(".obsidian/", ".trash/"),
            ),
            deny_write_paths=_path_rules(
                raw_entry.get("deny_write_paths"),
                name=f"vault {name!r} deny_write_paths",
                default=(".obsidian/", ".trash/", layout.conventions_path),
            ),
            read_only=read_only,
            description=str(raw_entry.get("description", "")),
            layout=layout,
        )

    vault_items = list(vaults.items())
    for index, (name, vault) in enumerate(vault_items):
        for other_name, other_vault in vault_items[index + 1:]:
            if (
                vault.path == other_vault.path
                or vault.path in other_vault.path.parents
                or other_vault.path in vault.path.parents
            ):
                raise ConfigError(
                    f"Vault paths must not overlap: {name!r} ({vault.path}) and "
                    f"{other_name!r} ({other_vault.path})"
                )

    identities_raw = data.get("identities") or []
    if not isinstance(identities_raw, list) or not identities_raw:
        raise ConfigError("VAULTS_CONFIG must define at least one identity under 'identities'")

    identities: list[Identity] = []
    for raw_identity in identities_raw:
        if not isinstance(raw_identity, dict):
            raise ConfigError("Identity entries in VAULTS_CONFIG must be objects")
        identity_type = raw_identity.get("type")
        if identity_type not in ("api_key", "github_login"):
            raise ConfigError(
                f"Unknown identity type in VAULTS_CONFIG: {identity_type!r} "
                "(must be 'api_key' or 'github_login')"
            )
        value = str(raw_identity.get("value", ""))
        if not value:
            raise ConfigError("An identity entry in VAULTS_CONFIG is missing 'value'")
        if identity_type == "github_login":
            value = value.lower()
        raw_identity_vaults = raw_identity.get("vaults", [])
        overrides: list[tuple[str, PolicyOverride]] = []
        if isinstance(raw_identity_vaults, dict):
            # Object form: {vault_name: {policy override}}. Each override may
            # only narrow that vault's own policy — validated below.
            if not all(isinstance(key, str) for key in raw_identity_vaults):
                raise ConfigError(
                    f"Identity {value!r} 'vaults' object keys must be vault names"
                )
            identity_vaults = tuple(raw_identity_vaults)
            for vault_name, raw_override in raw_identity_vaults.items():
                override = _parse_policy_override(
                    raw_override,
                    name=f"Identity {value!r} vault {vault_name!r}",
                )
                if not override.is_empty():
                    overrides.append((vault_name, override))
        elif isinstance(raw_identity_vaults, list) and all(
            isinstance(item, str) for item in raw_identity_vaults
        ):
            # Array form: inherit each vault's policy exactly as before.
            identity_vaults = tuple(raw_identity_vaults)
        else:
            raise ConfigError(
                f"Identity {value!r} 'vaults' must be a list of strings or an "
                "object mapping vault names to policy overrides"
            )
        if not identity_vaults:
            raise ConfigError(f"Identity {value!r} in VAULTS_CONFIG has no 'vaults'")
        for vault_name in identity_vaults:
            if vault_name not in vaults:
                raise ConfigError(
                    f"Identity {value!r} references unknown vault {vault_name!r}"
                )
        default = raw_identity.get("default")
        if default is None and len(identity_vaults) == 1:
            default = identity_vaults[0]
        if default is not None and default not in identity_vaults:
            raise ConfigError(
                f"Identity {value!r} default vault {default!r} is not in its own 'vaults' list"
            )
        for vault_name, override in overrides:
            _validate_override(
                vaults[vault_name],
                override,
                identity_value=value,
                global_read_only=global_read_only,
            )
        identities.append(
            Identity(
                type=identity_type,
                value=value,
                vaults=identity_vaults,
                default=default,
                overrides=tuple(overrides),
            )
        )
    return vaults, tuple(identities)


@dataclass(frozen=True, init=False)
class Config:
    """Validated immutable configuration with context-selected vault fields."""

    multi_vault: bool = field(init=False)
    vaults: Mapping[str, VaultEntry] = field(init=False)
    identities: tuple[Identity, ...] = field(init=False)
    default_vault_name: str = field(init=False)
    _global_read_only: bool = field(init=False, repr=False)
    lock_path: Path = field(init=False)
    audit_log_path: Path = field(init=False)
    allow_permanent_delete: bool = field(init=False)
    max_attachment_bytes: int = field(init=False)
    require_write_preconditions: bool = field(init=False)
    index_reconcile_interval: float = field(init=False)
    watcher_debounce_ms: int = field(init=False)
    transport: str = field(init=False)
    host: str = field(init=False)
    port: int = field(init=False)
    api_key: str = field(init=False, repr=False)
    public_base_url: str = field(init=False)
    oauth_github_client_id: str = field(init=False)
    oauth_github_client_secret: str = field(init=False, repr=False)
    oauth_github_allowed_logins: tuple[str, ...] = field(init=False)
    oauth_hardening: OAuthHardening = field(init=False)
    mask_error_details: bool = field(init=False)
    regex_max_length: int = field(init=False)
    regex_timeout_seconds: float = field(init=False)
    regex_max_repeat: int = field(init=False)
    enable_canvas: bool = field(init=False)
    enable_excalidraw: bool = field(init=False)
    enable_kanban: bool = field(init=False)
    enable_bases: bool = field(init=False)
    enable_move: bool = field(init=False)
    enable_folder_rename: bool = field(init=False)
    enable_bulk_replace: bool = field(init=False)
    enable_delete: bool = field(init=False)

    def __init__(self) -> None:
        set_value = object.__setattr__
        vaults_config_path = os.environ.get("VAULTS_CONFIG", "")
        set_value(self, "multi_vault", bool(vaults_config_path))
        set_value(
            self,
            "_global_read_only",
            os.environ.get("READ_ONLY", "false").lower() in ("1", "true", "yes"),
        )

        if self.multi_vault:
            vaults, identities = load_vaults_file(
                vaults_config_path, global_read_only=self._global_read_only
            )
            set_value(self, "vaults", MappingProxyType(vaults))
            set_value(self, "identities", identities)
            set_value(self, "default_vault_name", next(iter(vaults)))
            set_value(self, "api_key", "")
            set_value(
                self,
                "oauth_github_allowed_logins",
                tuple(identity.value for identity in identities if identity.type == "github_login"),
            )
        else:
            raw_vault = os.environ.get("VAULT_PATH", "")
            if not raw_vault:
                raise ConfigError("VAULT_PATH is required")
            vault_path = Path(raw_vault).resolve()
            if not vault_path.is_dir():
                raise ConfigError(
                    f"VAULT_PATH does not exist or is not a directory: {vault_path}"
                )
            try:
                layout = layout_from_env()
            except LayoutError as exc:
                raise ConfigError(str(exc)) from exc
            try:
                entry = VaultEntry(
                    name="default",
                    path=vault_path,
                    write_paths=tuple(
                        path_rules_from_env(
                            os.environ.get("WRITE_PATHS", ""), name="WRITE_PATHS"
                        )
                    ),
                    read_paths=tuple(
                        path_rules_from_env(
                            os.environ.get("READ_PATHS", ""), name="READ_PATHS"
                        )
                    ),
                    exclude_paths=tuple(
                        path_rules_from_env(
                            os.environ.get("EXCLUDE_PATHS", "private/,.obsidian/,.trash/"),
                            name="EXCLUDE_PATHS",
                        )
                    ),
                    deny_read_paths=tuple(
                        path_rules_from_env(
                            os.environ.get("DENY_READ_PATHS", ".obsidian/,.trash/"),
                            name="DENY_READ_PATHS",
                        )
                    ),
                    deny_write_paths=tuple(
                        path_rules_from_env(
                            os.environ.get(
                                "DENY_WRITE_PATHS",
                                f".obsidian/,.trash/,{layout.conventions_path}",
                            ),
                            name="DENY_WRITE_PATHS",
                        )
                    ),
                    layout=layout,
                )
            except VaultPathError as exc:
                raise ConfigError(str(exc)) from exc
            set_value(self, "vaults", MappingProxyType({"default": entry}))
            set_value(self, "identities", ())
            set_value(self, "default_vault_name", "default")
            set_value(
                self,
                "api_key",
                os.environ.get("API_KEY") or os.environ.get("OBSIDIAN_MCP_API_KEY") or "",
            )
            raw_logins = os.environ.get("OAUTH_GITHUB_ALLOWED_LOGINS", "")
            set_value(
                self,
                "oauth_github_allowed_logins",
                tuple(login.strip().lower() for login in raw_logins.split(",") if login.strip()),
            )

        raw_lock_path = os.environ.get("LOCK_PATH", "")
        if raw_lock_path:
            lock_path = Path(raw_lock_path).expanduser().resolve()
        elif os.environ.get("FASTMCP_HOME"):
            lock_path = (Path(os.environ["FASTMCP_HOME"]).expanduser() / "locks").resolve()
        else:
            lock_path = (Path(tempfile.gettempdir()) / "obsidian-mcp-locks").resolve()
        for vault in self.vaults.values():
            if lock_path == vault.path or vault.path in lock_path.parents:
                raise ConfigError("LOCK_PATH must be outside every configured vault")
        set_value(self, "lock_path", lock_path)

        raw_audit_path = os.environ.get("AUDIT_LOG_PATH", "")
        audit_log_path = (
            Path(os.path.abspath(Path(raw_audit_path).expanduser()))
            if raw_audit_path
            else lock_path / "audit.jsonl"
        )
        resolved_audit_parent = audit_log_path.parent.resolve()
        for vault in self.vaults.values():
            if (
                audit_log_path == vault.path
                or vault.path in audit_log_path.parents
                or resolved_audit_parent == vault.path
                or vault.path in resolved_audit_parent.parents
            ):
                raise ConfigError("AUDIT_LOG_PATH must be outside every configured vault")
        set_value(self, "audit_log_path", audit_log_path)

        set_value(
            self,
            "allow_permanent_delete",
            os.environ.get("ALLOW_PERMANENT_DELETE", "false").lower()
            in ("1", "true", "yes"),
        )
        raw_max_attachment_bytes = os.environ.get(
            "MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024)
        )
        try:
            max_attachment_bytes = int(raw_max_attachment_bytes)
        except ValueError as exc:
            raise ConfigError("MAX_ATTACHMENT_BYTES must be a positive integer") from exc
        if max_attachment_bytes <= 0:
            raise ConfigError("MAX_ATTACHMENT_BYTES must be a positive integer")
        set_value(self, "max_attachment_bytes", max_attachment_bytes)

        set_value(
            self,
            "require_write_preconditions",
            os.environ.get("REQUIRE_WRITE_PRECONDITIONS", "false").lower()
            in ("1", "true", "yes"),
        )
        try:
            reconcile_interval = float(os.environ.get("INDEX_RECONCILE_INTERVAL", "900"))
        except ValueError as exc:
            raise ConfigError("INDEX_RECONCILE_INTERVAL must be a finite positive number") from exc
        if not math.isfinite(reconcile_interval) or reconcile_interval <= 0:
            raise ConfigError("INDEX_RECONCILE_INTERVAL must be a finite positive number")
        set_value(self, "index_reconcile_interval", reconcile_interval)

        try:
            debounce_ms = int(os.environ.get("WATCHER_DEBOUNCE_MS", "100"))
        except ValueError as exc:
            raise ConfigError("WATCHER_DEBOUNCE_MS must be a non-negative integer") from exc
        if debounce_ms < 0:
            raise ConfigError("WATCHER_DEBOUNCE_MS must be a non-negative integer")
        set_value(self, "watcher_debounce_ms", debounce_ms)

        # Optional plugin-format tool groups — opt-in, disabled by default.
        for attribute, environment in (
            ("enable_canvas", "ENABLE_CANVAS"),
            ("enable_excalidraw", "ENABLE_EXCALIDRAW"),
            ("enable_kanban", "ENABLE_KANBAN"),
            ("enable_bases", "ENABLE_BASES"),
            ("enable_move", "ENABLE_MOVE"),
            ("enable_folder_rename", "ENABLE_FOLDER_RENAME"),
            ("enable_bulk_replace", "ENABLE_BULK_REPLACE"),
            ("enable_delete", "ENABLE_DELETE"),
        ):
            set_value(
                self,
                attribute,
                os.environ.get(environment, "false").lower() in ("1", "true", "yes"),
            )

        set_value(self, "transport", os.environ.get("TRANSPORT", "stdio"))
        if self.multi_vault and self.transport == "stdio":
            raise ConfigError(
                "VAULTS_CONFIG requires an authenticated network transport; "
                "TRANSPORT=stdio cannot identify the calling identity"
            )
        set_value(self, "host", os.environ.get("HOST", "0.0.0.0"))
        set_value(self, "port", int(os.environ.get("PORT", "8000")))
        set_value(self, "public_base_url", os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"))
        set_value(
            self, "oauth_github_client_id", os.environ.get("OAUTH_GITHUB_CLIENT_ID", "")
        )
        set_value(
            self,
            "oauth_github_client_secret",
            os.environ.get("OAUTH_GITHUB_CLIENT_SECRET", ""),
        )
        set_value(self, "oauth_hardening", oauth_hardening_from_env())
        set_value(self, "mask_error_details", mask_error_details_from_env())
        regex_max_length, regex_timeout_seconds = regex_limits_from_env()
        set_value(self, "regex_max_length", regex_max_length)
        set_value(self, "regex_timeout_seconds", regex_timeout_seconds)
        set_value(self, "regex_max_repeat", regex_max_repeat_from_env())

        oauth_configured = bool(
            self.oauth_github_client_id or self.oauth_github_client_secret
        )
        if oauth_configured:
            if not (self.oauth_github_client_id and self.oauth_github_client_secret):
                raise ConfigError(
                    "OAUTH_GITHUB_CLIENT_ID and OAUTH_GITHUB_CLIENT_SECRET must both be set "
                    "to enable GitHub OAuth"
                )
            if not self.oauth_github_allowed_logins:
                raise ConfigError(
                    "No allowed GitHub logins configured (OAUTH_GITHUB_ALLOWED_LOGINS, or "
                    "at least one 'github_login' identity in VAULTS_CONFIG) — without one, "
                    "any GitHub account could authenticate and get full access to a vault"
                )
            if not self.public_base_url:
                raise ConfigError(
                    "PUBLIC_BASE_URL is required when GitHub OAuth is configured "
                    "(used as the OAuth callback base URL, e.g. https://your-server.com)"
                )

        has_api_keys = bool(self.api_key) or any(
            identity.type == "api_key" for identity in self.identities
        )
        if self.transport != "stdio" and not has_api_keys and not oauth_configured:
            raise ConfigError(
                f"API_KEY or GitHub OAuth (OAUTH_GITHUB_CLIENT_ID/SECRET) is required when "
                f"TRANSPORT={self.transport} (the server would otherwise be reachable without "
                "authentication) — or 'api_key'/'github_login' identities via VAULTS_CONFIG"
            )

    def resolve_vault_name(self) -> str:
        name = _current_vault_var.get()
        if name is None:
            return self.default_vault_name
        if name not in self.vaults:
            raise ConfigError(f"Unknown vault: {name!r}")
        return name

    def _current_vault_entry(self) -> VaultEntry:
        return self.vaults[self.resolve_vault_name()]

    @property
    def vault_path(self) -> Path:
        return self._current_vault_entry().path

    @property
    def write_paths(self) -> tuple[str, ...]:
        return self._current_vault_entry().write_paths

    @property
    def read_paths(self) -> tuple[str, ...]:
        return self._current_vault_entry().read_paths

    @property
    def exclude_paths(self) -> tuple[str, ...]:
        return self._current_vault_entry().exclude_paths

    @property
    def deny_read_paths(self) -> tuple[str, ...]:
        return self._current_vault_entry().deny_read_paths

    @property
    def deny_write_paths(self) -> tuple[str, ...]:
        return self._current_vault_entry().deny_write_paths

    @property
    def layout(self) -> VaultLayout:
        return self._current_vault_entry().layout

    @property
    def read_only(self) -> bool:
        vault_read_only = self._current_vault_entry().read_only
        return self._global_read_only if vault_read_only is None else vault_read_only


# ── Hardening settings ───────────────────────────────────────────────────────
# Plain env readers (not tied to Config) because server.py needs some of them
# at import time, before VAULT_PATH/VAULTS_CONFIG are guaranteed to be set.
# Config.__init__ calls them too, so invalid values fail loudly at startup.

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false)")


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Comma-separated list; unset or blank falls back to the default."""
    items = tuple(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())
    return items or default


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


DEFAULT_OAUTH_GITHUB_SCOPES = ("read:user",)
# claude.ai remote-connector callback (Anthropic help center, "Building custom
# connectors via remote MCP servers"), plus the claude.com variant the same
# article says to allowlist too, plus loopback on any port for local clients
# (Claude Code, MCP Inspector, mcp-remote).
DEFAULT_OAUTH_ALLOWED_CLIENT_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
)
DEFAULT_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS = 30 * 24 * 3600
DEFAULT_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS = 90 * 24 * 3600


@dataclass(frozen=True)
class OAuthHardening:
    github_scopes: tuple[str, ...]
    enable_cimd: bool
    allowed_client_redirect_uris: tuple[str, ...]
    access_token_expiry_seconds: int
    refresh_token_expiry_seconds: int


def oauth_hardening_from_env() -> OAuthHardening:
    return OAuthHardening(
        github_scopes=_env_list("OAUTH_GITHUB_SCOPES", DEFAULT_OAUTH_GITHUB_SCOPES),
        enable_cimd=_env_bool("OAUTH_ENABLE_CIMD", False),
        allowed_client_redirect_uris=_env_list(
            "OAUTH_ALLOWED_CLIENT_REDIRECT_URIS", DEFAULT_OAUTH_ALLOWED_CLIENT_REDIRECT_URIS
        ),
        access_token_expiry_seconds=_env_positive_int(
            "OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS", DEFAULT_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS
        ),
        refresh_token_expiry_seconds=_env_positive_int(
            "OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS", DEFAULT_OAUTH_REFRESH_TOKEN_EXPIRY_SECONDS
        ),
    )


def mask_error_details_from_env() -> bool:
    """MASK_ERROR_DETAILS (default true): hide unexpected exception details
    from MCP clients; expected user-facing errors still get through."""
    return _env_bool("MASK_ERROR_DETAILS", True)


def regex_limits_from_env() -> tuple[int, float]:
    """(REGEX_MAX_LENGTH, REGEX_TIMEOUT_SECONDS) for caller-supplied regexes."""
    max_length = _env_positive_int("REGEX_MAX_LENGTH", 500)
    raw = os.environ.get("REGEX_TIMEOUT_SECONDS", "").strip() or "2"
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ConfigError("REGEX_TIMEOUT_SECONDS must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("REGEX_TIMEOUT_SECONDS must be a finite positive number")
    return max_length, timeout


def regex_max_repeat_from_env() -> int:
    """REGEX_MAX_REPEAT (default 100): largest ``{n}``/``{n,m}`` repetition
    count accepted in a caller-supplied regex (compile-time memory guard)."""
    return _env_positive_int("REGEX_MAX_REPEAT", 100)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config

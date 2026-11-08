"""Environment configuration for the ``ob`` wrapper.

Kept separate from :mod:`obsidian_mcp.config` so the wrapper stays an
isolated, opt-in add-on: when ``ENABLE_OB`` is false none of this is read.

Tier flags (:class:`ObFlags`) are read at import time of ``server.py`` to
decide which tools get registered. The runtime settings (:class:`ObSettings`)
need a vault path and are only resolved by ``start_ob()`` in ``main()``.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import ConfigError


def _truthy(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "false").strip().lower() in ("1", "true", "yes")


def ob_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``ENABLE_OB`` is set (the master switch for this package)."""
    return _truthy(os.environ if environ is None else environ, "ENABLE_OB")


@dataclass(frozen=True)
class ObFlags:
    """Which tool tiers are registered.

    * read-only tier: always, when ``ENABLE_OB``;
    * ops tier: ``ENABLE_OB_OPS`` (independent of ``READ_ONLY``);
    * admin tier: ``ENABLE_OB_ADMIN`` and not ``READ_ONLY``.
    """

    enable_ob: bool
    enable_ops: bool
    enable_admin: bool

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ObFlags:
        env = os.environ if environ is None else environ
        enable_ob = _truthy(env, "ENABLE_OB")
        return cls(
            enable_ob=enable_ob,
            enable_ops=enable_ob and _truthy(env, "ENABLE_OB_OPS"),
            enable_admin=enable_ob
            and _truthy(env, "ENABLE_OB_ADMIN")
            and not _truthy(env, "READ_ONLY"),
        )


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip() or str(default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip() or str(default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a finite positive number")
    return value


def auth_token_file(environ: Mapping[str, str] | None = None) -> Path:
    """Where ob keeps its login token: ``$XDG_CONFIG_HOME/obsidian-headless/auth_token``."""
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "obsidian-headless" / "auth_token"


def _non_negative_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip() or str(default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a finite number >= 0") from exc
    if not math.isfinite(value) or value < 0:
        raise ConfigError(f"{name} must be a finite number >= 0")
    return value


def _resolve_vault_path(env: Mapping[str, str]) -> Path:
    raw = env.get("OB_VAULT_PATH", "").strip()
    if not raw:
        if env.get("VAULTS_CONFIG", "").strip():
            raise ConfigError(
                "ENABLE_OB with VAULTS_CONFIG (multi-vault) requires OB_VAULT_PATH: "
                "the ob sync supervisor manages exactly one vault and will not guess which"
            )
        raw = env.get("VAULT_PATH", "").strip()
        if not raw:
            raise ConfigError("ENABLE_OB requires OB_VAULT_PATH or VAULT_PATH")
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ConfigError(f"OB vault path does not exist or is not a directory: {path}")
    return path


@dataclass(frozen=True)
class ObSettings:
    """Runtime settings for the supervisor and the one-shot command runner."""

    ob_bin: str
    vault_path: Path
    log_lines: int = 1000
    command_timeout: float = 60.0
    restart_backoff_max: float = 300.0
    restart_cooldown: float = 60.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ObSettings:
        env = os.environ if environ is None else environ
        ob_bin = env.get("OB_BIN", "").strip() or "ob"
        return cls(
            ob_bin=ob_bin,
            vault_path=_resolve_vault_path(env),
            log_lines=_positive_int(env, "OB_LOG_LINES", 1000),
            command_timeout=_positive_float(env, "OB_COMMAND_TIMEOUT", 60.0),
            restart_backoff_max=_positive_float(env, "OB_RESTART_BACKOFF_MAX", 300.0),
            restart_cooldown=_non_negative_float(env, "OB_RESTART_COOLDOWN", 60.0),
        )

    def sync_argv(self) -> list[str]:
        """The exact command line of the supervised continuous-sync child."""
        return [self.ob_bin, "sync", "--path", str(self.vault_path), "--continuous"]

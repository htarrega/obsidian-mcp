"""Validation of model-supplied sync-config values and OB_* settings."""

from __future__ import annotations

import pytest

from obsidian_mcp.config import ConfigError
from obsidian_mcp.ob.settings import ObFlags, ObSettings
from obsidian_mcp.ob.validate import (
    CONFIG_CATEGORIES,
    FILE_TYPES,
    ObValidationError,
    conflict_strategy_value,
    enum_list_value,
    excluded_folders_value,
    normalize_folder,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Private", "Private"),
        ("  a/b/  ", "a/b"),
        ("./a//b/./c", "a/b/c"),
        ("Archive 2023", "Archive 2023"),
    ],
)
def test_normalize_folder_accepts_relative_folders(raw, expected):
    assert normalize_folder(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "/etc",
        "../outside",
        "a/../../b",
        "a,b",
        "a\x00b",
        "a\nb",
        "a\rb",
        "",
        "   ",
        ".",
        "./",
        "-rf",
        "--mode",
        "a\\b",
        "C:/x",
        123,
        None,
    ],
)
def test_normalize_folder_rejects_bad_values(bad):
    with pytest.raises(ObValidationError):
        normalize_folder(bad)


def test_excluded_folders_value_dedupes_and_clears():
    assert excluded_folders_value(["a/", "a", "b"]) == ("a,b", ["a", "b"])
    assert excluded_folders_value([]) == ("", [])


def test_enum_lists_validate_members():
    assert enum_list_value(["pdf", "image", "pdf"], FILE_TYPES, "file type") == "pdf,image"
    assert enum_list_value([], CONFIG_CATEGORIES, "category") == ""
    with pytest.raises(ObValidationError):
        enum_list_value(["exe"], FILE_TYPES, "file type")
    with pytest.raises(ObValidationError):
        enum_list_value(["app,hotkey"], CONFIG_CATEGORIES, "category")
    assert conflict_strategy_value("conflict") == "conflict"
    with pytest.raises(ObValidationError):
        conflict_strategy_value("--mode")


def test_flags_default_off():
    assert ObFlags.from_env({}) == ObFlags(False, False, False)
    # Tier flags without the master switch do nothing.
    assert ObFlags.from_env({"ENABLE_OB_OPS": "true", "ENABLE_OB_ADMIN": "true"}) == ObFlags(
        False, False, False
    )


def test_read_only_disables_admin_only():
    env = {"ENABLE_OB": "1", "ENABLE_OB_OPS": "yes", "ENABLE_OB_ADMIN": "true", "READ_ONLY": "true"}
    assert ObFlags.from_env(env) == ObFlags(True, True, False)
    env["READ_ONLY"] = "false"
    assert ObFlags.from_env(env) == ObFlags(True, True, True)


def test_settings_default_to_vault_path(tmp_path):
    settings = ObSettings.from_env({"VAULT_PATH": str(tmp_path)})
    assert settings.vault_path == tmp_path.resolve()
    assert settings.ob_bin == "ob"
    assert (settings.log_lines, settings.command_timeout, settings.restart_backoff_max) == (
        1000,
        60.0,
        300.0,
    )
    assert settings.restart_cooldown == 60.0
    assert settings.sync_argv() == ["ob", "sync", "--path", str(tmp_path.resolve()), "--continuous"]


def test_settings_ob_vault_path_overrides(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    settings = ObSettings.from_env(
        {
            "VAULT_PATH": str(tmp_path),
            "OB_VAULT_PATH": str(other),
            "OB_BIN": "/opt/ob/bin/ob",
            "OB_LOG_LINES": "10",
            "OB_COMMAND_TIMEOUT": "2.5",
            "OB_RESTART_BACKOFF_MAX": "30",
            "OB_RESTART_COOLDOWN": "0",
        }
    )
    assert settings.restart_cooldown == 0.0
    assert settings.vault_path == other.resolve()
    assert settings.ob_bin == "/opt/ob/bin/ob"
    assert (settings.log_lines, settings.command_timeout, settings.restart_backoff_max) == (
        10,
        2.5,
        30.0,
    )


def test_settings_multi_vault_requires_ob_vault_path(tmp_path):
    with pytest.raises(ConfigError, match="OB_VAULT_PATH"):
        ObSettings.from_env({"VAULTS_CONFIG": str(tmp_path / "vaults.json")})
    settings = ObSettings.from_env(
        {"VAULTS_CONFIG": str(tmp_path / "vaults.json"), "OB_VAULT_PATH": str(tmp_path)}
    )
    assert settings.vault_path == tmp_path.resolve()


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"VAULT_PATH": "/does/not/exist"},
        {"OB_LOG_LINES": "0"},
        {"OB_LOG_LINES": "many"},
        {"OB_COMMAND_TIMEOUT": "-1"},
        {"OB_RESTART_BACKOFF_MAX": "inf"},
        {"OB_RESTART_COOLDOWN": "-1"},
        {"OB_RESTART_COOLDOWN": "nan"},
        {"OB_RESTART_COOLDOWN": "soon"},
    ],
)
def test_settings_reject_invalid_values(tmp_path, env):
    base = {} if env == {} or "VAULT_PATH" in env else {"VAULT_PATH": str(tmp_path)}
    with pytest.raises(ConfigError):
        ObSettings.from_env({**base, **env})

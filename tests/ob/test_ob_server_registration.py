"""server.py hook: ENABLE_OB gating, tier flags, READ_ONLY, startup/shutdown."""

from __future__ import annotations

import importlib
import sys

import pytest

import obsidian_mcp.config as cfg_mod
import obsidian_mcp.server as server_mod
from obsidian_mcp.ob import service as service_mod
from obsidian_mcp.ob.tools import ADMIN_TOOLS, OPS_TOOLS, READ_ONLY_TOOLS

ALL_OB_TOOLS = set(READ_ONLY_TOOLS) | set(OPS_TOOLS) | set(ADMIN_TOOLS)
_ENV = ("ENABLE_OB", "ENABLE_OB_OPS", "ENABLE_OB_ADMIN", "READ_ONLY", "TOOLS_ALLOWLIST")


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    yield
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    cfg_mod._config = None
    importlib.reload(server_mod)


async def _tool_names(monkeypatch, **env: str) -> set[str]:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cfg_mod._config = None
    server = importlib.reload(server_mod)
    return {tool.name for tool in await server.mcp.list_tools()}


async def test_disabled_registers_nothing_and_imports_nothing(monkeypatch):
    for name in [m for m in sys.modules if m.startswith("obsidian_mcp.ob")]:
        monkeypatch.delitem(sys.modules, name)
    names = await _tool_names(
        monkeypatch, ENABLE_OB_OPS="true", ENABLE_OB_ADMIN="true"
    )
    assert names.isdisjoint(ALL_OB_TOOLS)
    assert not any(m.startswith("obsidian_mcp.ob") for m in sys.modules)
    assert server_mod._ob_enabled is False


async def test_read_only_tier_only_by_default(monkeypatch):
    names = await _tool_names(monkeypatch, ENABLE_OB="true")
    assert set(READ_ONLY_TOOLS) <= names
    assert names.isdisjoint(set(OPS_TOOLS) | set(ADMIN_TOOLS))


async def test_ops_tier(monkeypatch):
    names = await _tool_names(monkeypatch, ENABLE_OB="true", ENABLE_OB_OPS="true")
    assert set(READ_ONLY_TOOLS) | set(OPS_TOOLS) <= names
    assert names.isdisjoint(ADMIN_TOOLS)


async def test_admin_tier(monkeypatch):
    names = await _tool_names(monkeypatch, ENABLE_OB="true", ENABLE_OB_ADMIN="true")
    assert set(READ_ONLY_TOOLS) | set(ADMIN_TOOLS) <= names
    assert names.isdisjoint(OPS_TOOLS)


async def test_read_only_disables_admin_but_not_ops(monkeypatch):
    names = await _tool_names(
        monkeypatch,
        ENABLE_OB="true",
        ENABLE_OB_OPS="true",
        ENABLE_OB_ADMIN="true",
        READ_ONLY="true",
    )
    assert set(READ_ONLY_TOOLS) | set(OPS_TOOLS) <= names
    assert names.isdisjoint(ADMIN_TOOLS)


async def test_never_exposes_dangerous_commands(monkeypatch):
    names = await _tool_names(
        monkeypatch, ENABLE_OB="true", ENABLE_OB_OPS="true", ENABLE_OB_ADMIN="true"
    )
    ob_names = {n for n in names if n in ALL_OB_TOOLS or "sync" in n or n.startswith("ob")}
    assert ob_names == ALL_OB_TOOLS
    for forbidden in ("login", "logout", "setup", "unlink", "publish", "create_remote", "mode"):
        assert not any(forbidden in n for n in ob_names)


async def test_ob_tools_advertise_object_schema(monkeypatch):
    await _tool_names(
        monkeypatch, ENABLE_OB="true", ENABLE_OB_OPS="true", ENABLE_OB_ADMIN="true"
    )
    for tool in await server_mod.mcp.list_tools():
        if tool.name in ALL_OB_TOOLS:
            assert tool.output_schema and tool.output_schema.get("type") == "object"


def _run_main(monkeypatch, vault, **env):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("LOCK_PATH", str(vault.parent / "locks"))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cfg_mod._config = None
    server = importlib.reload(server_mod)
    seen = {}

    def fake_run(**kwargs):
        seen["service"] = service_mod.get_service()

    monkeypatch.setattr(server.mcp, "run", fake_run)
    monkeypatch.setattr(service_mod, "_install_exit_hooks", lambda: None)
    server.main()
    return seen


def test_main_without_enable_ob_starts_nothing(fake_ob, monkeypatch):
    seen = _run_main(monkeypatch, fake_ob.vault, OB_BIN=str(fake_ob.bin))
    assert seen["service"] is None
    assert fake_ob.calls() == []


def test_main_starts_supervisor_and_stop_ob_terminates(fake_ob, monkeypatch, wait):
    try:
        seen = _run_main(monkeypatch, fake_ob.vault, ENABLE_OB="true", OB_BIN=str(fake_ob.bin))
        svc = seen["service"]
        assert svc is not None
        assert svc.settings.sync_argv() == [
            str(fake_ob.bin),
            "sync",
            "--path",
            str(fake_ob.vault),
            "--continuous",
        ]
        wait(lambda: fake_ob.live_sync_pids())
        service_mod.stop_ob()
        assert fake_ob.live_sync_pids() == []
        assert svc.supervisor.status()["state"] == "stopped"
    finally:
        service_mod.stop_ob()
        service_mod.set_service(None)


async def test_main_starts_supervisor_even_when_every_ob_tool_is_hidden(
    fake_ob, monkeypatch, wait
):
    try:
        seen = _run_main(
            monkeypatch,
            fake_ob.vault,
            ENABLE_OB="true",
            ENABLE_OB_OPS="true",
            OB_BIN=str(fake_ob.bin),
            TOOLS_ALLOWLIST="read_note_tool",
        )
        names = {tool.name for tool in await server_mod.mcp.list_tools()}
        assert names == {"read_note_tool"}
        assert seen["service"] is not None
        wait(lambda: fake_ob.live_sync_pids())
    finally:
        service_mod.stop_ob()
        service_mod.set_service(None)


def test_main_fails_fast_on_multi_vault_without_ob_vault_path(monkeypatch, tmp_path):
    from obsidian_mcp.config import ConfigError

    monkeypatch.setenv("ENABLE_OB", "true")
    monkeypatch.delenv("OB_VAULT_PATH", raising=False)
    monkeypatch.setenv("VAULTS_CONFIG", str(tmp_path / "vaults.json"))
    with pytest.raises(ConfigError, match="OB_VAULT_PATH"):
        service_mod.start_ob()
    assert service_mod.get_service() is None


@pytest.mark.parametrize("value", [" true ", "TRUE", "1\n", " yes", "0", "no", " ", "on"])
async def test_enable_ob_parsing_matches_ob_flags(monkeypatch, value):
    from obsidian_mcp.ob.settings import ObFlags

    names = await _tool_names(monkeypatch, ENABLE_OB=value)
    expected = ObFlags.from_env({"ENABLE_OB": value}).enable_ob
    assert server_mod._ob_enabled is expected
    assert (set(READ_ONLY_TOOLS) <= names) is expected

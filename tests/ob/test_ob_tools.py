"""The ob MCP tools against a fake ob: output shape, redaction, admin flow."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from obsidian_mcp.ob import tools
from obsidian_mcp.ob.redact import EMAIL_MASK


def test_tools_require_started_service(fake_ob):
    with pytest.raises(ToolError, match="not running"):
        tools.sync_status()


def test_sync_status_combines_supervisor_and_config(fake_ob, ob_service, wait):
    svc = ob_service()
    wait(lambda: svc.supervisor.pid)
    result = tools.sync_status()
    assert result["success"] is True
    data = result["data"]
    assert data["supervisor"]["state"] == "running"
    assert data["config"]["vaultName"] == "Test Vault"
    assert data["config"]["conflictStrategy"] == "merge"
    assert "SuperSecret" not in str(result)
    assert ["sync-status", "--path", str(fake_ob.vault), "--json"] in fake_ob.argvs()


def test_sync_status_reports_missing_config(fake_ob, ob_service, monkeypatch):
    ob_service(start=False)
    monkeypatch.setenv("FAKE_OB_EXIT_SYNC_STATUS", "3")
    monkeypatch.setenv("FAKE_OB_STDOUT_SYNC_STATUS", "No sync configuration found")
    data = tools.sync_status()["data"]
    assert data["config"] is None
    assert "ob sync-setup" in data["config_error"]


def test_sync_logs_clamps_and_redacts(fake_ob, ob_service, wait, monkeypatch):
    monkeypatch.setenv(
        "FAKE_OB_SYNC_LINES", "\n".join([f"Downloaded n{i}.md" for i in range(20)] + ["me@x.org"])
    )
    svc = ob_service(log_lines=5)
    wait(lambda: any(EMAIL_MASK in line for line in svc.supervisor.logs(5)))
    assert len(tools.sync_logs(lines=1000)["data"]["lines"]) == 5
    assert len(tools.sync_logs(lines=0)["data"]["lines"]) == 1
    assert len(tools.sync_logs(lines=-5)["data"]["lines"]) == 1
    assert "me@x.org" not in str(tools.sync_logs())


def test_ob_info(fake_ob, ob_service, tmp_path, monkeypatch):
    ob_service(start=False)
    info = tools.ob_info()["data"]
    assert info["version"] == "0.0.14"
    assert info["local_vault"]["path"] == str(fake_ob.vault)
    assert info["local_vault"]["id"] == "0123456789abcdef0123456789abcdef"
    assert info["device_name"] == "test-box"
    assert info["auth_token_file_present"] is False
    assert info["auth_token_env_set"] is False
    assert "errors" not in info

    token_file = tmp_path / "xdg" / "obsidian-headless" / "auth_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("very-secret-token-value")
    monkeypatch.setenv("OBSIDIAN_AUTH_TOKEN", "env-token-value")
    info = tools.ob_info()
    assert info["data"]["auth_token_file_present"] is True
    assert info["data"]["auth_token_env_set"] is True
    assert "very-secret" not in str(info) and "env-token-value" not in str(info)
    # Makes no network call.
    assert not any(argv[0] == "sync-list-remote" for argv in fake_ob.argvs())


def test_list_remote_vaults(fake_ob, ob_service, monkeypatch):
    ob_service(start=False)
    data = tools.list_remote_vaults()["data"]
    assert data["vaults"][0]["region"] == "eu"
    monkeypatch.setenv("FAKE_OB_EXIT_SYNC_LIST_REMOTE", "2")
    monkeypatch.setenv("FAKE_OB_STDERR_SYNC_LIST_REMOTE", "token=abc123 for a@b.io")
    with pytest.raises(ToolError) as excinfo:
        tools.list_remote_vaults()
    assert "abc123" not in str(excinfo.value) and "a@b.io" not in str(excinfo.value)


def test_sync_restart_returns_new_status(fake_ob, ob_service, wait):
    svc = ob_service()
    wait(lambda: svc.supervisor.pid)
    old = svc.supervisor.pid
    result = tools.sync_restart()
    assert result["meta"]["action"] == "restarted"
    wait(lambda: svc.supervisor.pid not in (None, old))
    assert len(fake_ob.live_sync_pids()) <= 1


def _admin_sequence(fake_ob):
    return [argv[0] for argv in fake_ob.argvs() if argv[0] in ("sync", "sync-config")]


def test_admin_flow_stops_configures_restarts(fake_ob, ob_service, wait):
    ob_service()
    wait(lambda: fake_ob.live_sync_pids())
    result =tools.sync_set_excluded_folders(["Private/", "./Archive//2023", "Private"])
    assert result["meta"] == {"action": "sync_config_updated", "setting": "excluded_folders"}
    assert result["data"]["config"]["excludedFolders"] == ["Private", "Archive/2023"]
    assert "SuperSecret" not in str(result)

    config_calls = [c for c in fake_ob.calls() if c["argv"][0] == "sync-config"]
    assert [c["argv"] for c in config_calls] == [
        [
            "sync-config",
            "--path",
            str(fake_ob.vault),
            "--excluded-folders",
            "Private,Archive/2023",
            "--json",
        ]
    ]
    assert config_calls[0]["sync_running"] is False  # child was stopped first
    wait(lambda: len(fake_ob.live_sync_pids()) == 1)
    assert _admin_sequence(fake_ob) == ["sync", "sync-config", "sync"]


@pytest.mark.parametrize(
    "call, flag, value",
    [
        (lambda: tools.sync_set_excluded_folders([]), "--excluded-folders", ""),
        (lambda: tools.sync_set_file_types(["pdf", "image"]), "--file-types", "pdf,image"),
        (lambda: tools.sync_set_file_types([]), "--file-types", ""),
        (
            lambda: tools.sync_set_configs(["app", "hotkey", "core-plugin"]),
            "--configs",
            "app,hotkey,core-plugin",
        ),
        (lambda: tools.sync_set_conflict_strategy("conflict"), "--conflict-strategy", "conflict"),
    ],
)
def test_admin_tools_build_exact_argv(fake_ob, ob_service, call, flag, value):
    ob_service(start=False)
    call()
    config_argvs = [argv for argv in fake_ob.argvs() if argv[0] == "sync-config"]
    assert config_argvs == [["sync-config", "--path", str(fake_ob.vault), flag, value, "--json"]]


@pytest.mark.parametrize(
    "call",
    [
        lambda: tools.sync_set_excluded_folders(["../etc"]),
        lambda: tools.sync_set_excluded_folders(["/abs"]),
        lambda: tools.sync_set_excluded_folders(["a,b"]),
        lambda: tools.sync_set_excluded_folders(["a\nb"]),
        lambda: tools.sync_set_excluded_folders(["--mode"]),
        lambda: tools.sync_set_file_types(["exe"]),
        lambda: tools.sync_set_configs(["everything"]),
        lambda: tools.sync_set_conflict_strategy("mirror-remote"),
    ],
)
def test_admin_tools_reject_invalid_values_without_touching_child(
    fake_ob, ob_service, wait, call
):
    svc = ob_service()
    wait(lambda: svc.supervisor.pid)
    pid = svc.supervisor.pid
    with pytest.raises(ToolError):
        call()
    assert not any(argv[0] == "sync-config" for argv in fake_ob.argvs())
    assert svc.supervisor.pid == pid


def test_admin_restarts_child_even_if_config_fails(fake_ob, ob_service, wait, monkeypatch):
    ob_service()
    wait(lambda: fake_ob.live_sync_pids())
    monkeypatch.setenv("FAKE_OB_EXIT_SYNC_CONFIG", "1")
    with pytest.raises(ToolError, match="not changed"):
        tools.sync_set_conflict_strategy("conflict")
    wait(lambda: fake_ob.live_sync_pids())
    assert _admin_sequence(fake_ob) == ["sync", "sync-config", "sync"]


def test_start_ob_registers_auth_token_file_for_redaction(fake_ob, tmp_path, monkeypatch, wait):
    from obsidian_mcp.ob import redact as redact_mod
    from obsidian_mcp.ob import service as service_mod

    token_file = tmp_path / "xdg" / "obsidian-headless" / "auth_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("file-token-zz9\n")
    monkeypatch.setenv("FAKE_OB_SYNC_LINES", "auth with file-token-zz9")
    monkeypatch.setattr(service_mod, "_install_exit_hooks", lambda: None)
    redact_mod.clear_registered_secrets()
    try:
        svc = service_mod.start_ob(fake_ob.settings())
        try:
            wait(lambda: any("auth with" in line for line in svc.supervisor.logs(50)))
            joined = "\n".join(svc.supervisor.logs(50))
            assert "file-token-zz9" not in joined
            assert "file-token-zz9" not in str(tools.ob_info())
        finally:
            service_mod.stop_ob()
            service_mod.set_service(None)
    finally:
        redact_mod.clear_registered_secrets()


def test_sync_restart_cooldown(fake_ob, ob_service, wait):
    svc = ob_service(restart_cooldown=60.0)
    wait(lambda: fake_ob.live_sync_pids())
    pid = svc.supervisor.pid
    with pytest.raises(ToolError, match="OB_RESTART_COOLDOWN"):
        tools.sync_restart()
    assert svc.supervisor.pid == pid
    assert len([a for a in fake_ob.argvs() if a[0] == "sync"]) == 1

    svc.supervisor.restart_cooldown = 0.2
    wait(lambda: _try_restart())
    wait(lambda: svc.supervisor.pid not in (None, pid))


def _try_restart() -> bool:
    try:
        tools.sync_restart()
    except ToolError:
        return False
    return True


def test_sync_restart_gives_up_when_control_lock_is_held(fake_ob, ob_service):
    svc = ob_service(start=False)
    svc.supervisor.control_timeout = 0.2
    assert svc.supervisor._control.acquire()
    try:
        with pytest.raises(ToolError, match="in progress"):
            tools.sync_restart()
        with pytest.raises(ToolError, match="not changed"):
            tools.sync_set_conflict_strategy("conflict")
    finally:
        svc.supervisor._control.release()
    assert not any(argv[0] == "sync-config" for argv in fake_ob.argvs())

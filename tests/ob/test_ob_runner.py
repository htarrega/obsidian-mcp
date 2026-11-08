"""One-shot runner: allowlist, exact argv (no shell), timeout, cap, redaction."""

from __future__ import annotations

import os
import time

import pytest

from obsidian_mcp.ob import runner as runner_mod
from obsidian_mcp.ob.runner import (
    MAX_OUTPUT_BYTES,
    ObArgumentError,
    ObCommandError,
    ObRunner,
    ObTimeoutError,
    validate_args,
)


@pytest.mark.parametrize(
    "args",
    [
        ["login"],
        ["logout"],
        ["sync-setup"],
        ["sync-create-remote"],
        ["sync-unlink"],
        ["sync"],
        ["sync", "--continuous"],
        ["publish-list"],
        ["sync-config", "--mode", "mirror-remote"],
        ["sync-config", "--device-name", "x"],
        ["sync-config", "--config-dir", "x"],
        ["sync-config", "--excluded-folders", "--mode"],
        ["sync-config", "--excluded-folders"],
        ["sync-config", "--path", "a", "--path", "b"],
        ["sync-status", "--path", "a\nb"],
        ["--version", "--json"],
        ["sync-list-local; rm -rf /"],
        [],
    ],
)
def test_allowlist_rejects(args):
    with pytest.raises(ObArgumentError):
        validate_args(args)


def test_allowlist_accepts_clear_value():
    validate_args(["sync-config", "--path", "/v", "--excluded-folders", "", "--json"])


def test_runner_does_not_spawn_rejected_commands(fake_ob):
    runner = ObRunner(fake_ob.settings())
    with pytest.raises(ObArgumentError):
        runner.run(["login"])
    assert fake_ob.calls() == []


def test_runner_passes_exact_argv_without_shell(fake_ob, monkeypatch):
    seen = {}
    real_popen = runner_mod.subprocess.Popen

    def spy(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(runner_mod.subprocess, "Popen", spy)
    runner = ObRunner(fake_ob.settings())
    result = runner.run(["sync-status", "--path", str(fake_ob.vault), "--json"])
    assert result.returncode == 0
    assert seen["argv"] == [str(fake_ob.bin), "sync-status", "--path", str(fake_ob.vault), "--json"]
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["cwd"] == str(fake_ob.vault)
    assert fake_ob.argvs() == [["sync-status", "--path", str(fake_ob.vault), "--json"]]


def test_runner_parses_and_redacts_json(fake_ob):
    data = ObRunner(fake_ob.settings()).run(["sync-status", "--path", str(fake_ob.vault), "--json"]).json()
    assert data["vaultName"] == "Test Vault"
    assert data["vaultId"] == "0123456789abcdef0123456789abcdef"
    assert "SuperSecret" not in str(data)


def test_runner_timeout_kills_process(fake_ob, monkeypatch):
    monkeypatch.setenv("FAKE_OB_SLEEP_VERSION", "30")
    runner = ObRunner(fake_ob.settings(command_timeout=0.5))
    started = time.monotonic()
    with pytest.raises(ObTimeoutError):
        runner.run(["--version"])
    assert time.monotonic() - started < 5
    pid = fake_ob.calls()[0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_runner_caps_output(fake_ob, monkeypatch):
    monkeypatch.setenv("FAKE_OB_BIG_VERSION", str(MAX_OUTPUT_BYTES * 3))
    result = ObRunner(fake_ob.settings()).run(["--version"])
    assert result.truncated is True
    assert len(result.stdout) <= MAX_OUTPUT_BYTES


def test_runner_redacts_stdout_and_errors(fake_ob, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_AUTH_TOKEN", "env-secret-value")
    monkeypatch.setenv(
        "FAKE_OB_STDOUT_VERSION",
        "user alice@example.com password=hunter2 Bearer abcdef env-secret-value\n",
    )
    out = ObRunner(fake_ob.settings()).run(["--version"]).stdout
    for secret in ("alice@example.com", "hunter2", "abcdef", "env-secret-value"):
        assert secret not in out

    monkeypatch.setenv("FAKE_OB_EXIT_SYNC_LIST_REMOTE", "2")
    monkeypatch.setenv("FAKE_OB_STDERR_SYNC_LIST_REMOTE", "No account logged in for bob@example.com\n")
    with pytest.raises(ObCommandError) as excinfo:
        ObRunner(fake_ob.settings()).run(["sync-list-remote", "--json"])
    assert excinfo.value.returncode == 2
    assert "bob@example.com" not in str(excinfo.value)
    assert "ob login" in str(excinfo.value)


def test_runner_missing_binary(fake_ob, tmp_path):
    runner = ObRunner(fake_ob.settings(ob_bin=str(tmp_path / "nope")))
    with pytest.raises(ObCommandError, match="cannot execute"):
        runner.run(["--version"])

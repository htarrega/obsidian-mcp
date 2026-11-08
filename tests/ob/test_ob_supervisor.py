"""Supervisor of `ob sync --continuous`: launch, logs, backoff, stop, restart."""

from __future__ import annotations

import os

import pytest

from obsidian_mcp.ob import supervisor as supervisor_mod
from obsidian_mcp.ob.redact import EMAIL_MASK


def _sync_calls(fake_ob):
    return [argv for argv in fake_ob.argvs() if argv and argv[0] == "sync"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_launches_exact_command_and_captures_logs(fake_ob, ob_service, wait, monkeypatch):
    monkeypatch.setenv(
        "FAKE_OB_SYNC_LINES",
        "Connecting...\nUploading file notes/a.md\nFully synced\nSync error: boom for x@y.com",
    )
    svc = ob_service()
    wait(lambda: svc.supervisor.status()["last_fully_synced_at"] is not None)
    wait(lambda: any("Sync error" in line for line in svc.supervisor.logs(50)))
    assert _sync_calls(fake_ob) == [["sync", "--path", str(fake_ob.vault), "--continuous"]]
    assert fake_ob.calls()[0]["cwd"] == str(fake_ob.vault)
    status = svc.supervisor.status()
    assert status["state"] == "running"
    assert status["pid"] is not None
    assert status["restart_count"] == 0
    assert any("Sync error" in err for err in status["recent_errors"])
    joined = "\n".join(svc.supervisor.logs(50))
    assert "Uploading file notes/a.md" in joined
    assert "x@y.com" not in joined and EMAIL_MASK in joined
    assert "x@y.com" not in str(status)


def test_log_ring_buffer_is_bounded(fake_ob, ob_service, wait, monkeypatch):
    monkeypatch.setenv("FAKE_OB_SYNC_LINES", "\n".join(f"line {i}" for i in range(30)))
    svc = ob_service(log_lines=10)
    wait(lambda: any("line 29" in line for line in svc.supervisor.logs(10)))
    assert len(svc.supervisor.logs(1000)) == 10
    assert len(svc.supervisor.logs(3)) == 3


def test_restarts_with_backoff_after_crash(fake_ob, ob_service, wait, monkeypatch):
    monkeypatch.setenv("FAKE_OB_SYNC_EXIT", "1")
    monkeypatch.setenv("FAKE_OB_SYNC_EXIT_ONCE", str(fake_ob.root / "crashed-once"))
    svc = ob_service()
    wait(lambda: len(_sync_calls(fake_ob)) == 2 and svc.supervisor.status()["state"] == "running")
    status = svc.supervisor.status()
    assert status["restart_count"] == 1
    assert status["last_exit_code"] == 1
    assert any("exited with code 1" in err for err in status["recent_errors"])


def test_backoff_grows_exponentially(fake_ob, monkeypatch):
    """Delays requested from the stop event double up to backoff_max."""
    monkeypatch.setenv("FAKE_OB_SYNC_EXIT", "1")
    sup = supervisor_mod.SyncSupervisor(
        fake_ob.settings().sync_argv(), fake_ob.vault, backoff_initial=0.05, backoff_max=0.2
    )
    delays: list[float] = []
    real_wait = sup._wait_for_restart

    def record(stop_event, delay, state, reason):
        delays.append(delay)
        if len(delays) >= 5:
            stop_event.set()
        real_wait(stop_event, delay, state, reason)

    monkeypatch.setattr(sup, "_wait_for_restart", record)
    sup.start()
    sup._thread.join(timeout=10)
    sup.stop()
    assert delays == [0.05, 0.1, 0.2, 0.2, 0.2]


@pytest.mark.parametrize("code, hint", [(2, "ob login"), (3, "ob sync-setup")])
def test_fatal_exit_codes_report_failed_with_max_backoff(
    fake_ob, ob_service, wait, monkeypatch, code, hint
):
    monkeypatch.setenv("FAKE_OB_SYNC_EXIT", str(code))
    svc = ob_service(restart_backoff_max=30.0)
    wait(lambda: svc.supervisor.status()["state"] == "failed")
    status = svc.supervisor.status()
    assert status["last_exit_code"] == code
    assert hint in status["failure_reason"]
    assert status["next_restart_in_seconds"] > 20
    assert len(_sync_calls(fake_ob)) == 1


def test_missing_binary_reports_failed(fake_ob, ob_service, wait, tmp_path):
    svc = ob_service(ob_bin=str(tmp_path / "missing-ob"))
    wait(lambda: svc.supervisor.status()["state"] == "failed")
    assert "cannot execute" in svc.supervisor.status()["failure_reason"]


def test_stop_terminates_child(fake_ob, ob_service, wait):
    svc = ob_service()
    wait(lambda: fake_ob.live_sync_pids())
    pid = svc.supervisor.pid
    svc.supervisor.stop()
    assert not _alive(pid)
    assert fake_ob.live_sync_pids() == []  # exited through its SIGTERM handler
    assert svc.supervisor.status()["state"] == "stopped"
    assert any("SIGTERM" in line for line in svc.supervisor.logs(50))


def test_stop_kills_child_that_ignores_sigterm(fake_ob, ob_service, wait, monkeypatch):
    monkeypatch.setenv("FAKE_OB_SYNC_IGNORE_TERM", "1")
    svc = ob_service()
    wait(lambda: fake_ob.live_sync_pids())
    pid = svc.supervisor.pid
    svc.supervisor.stop_timeout = 0.3
    svc.supervisor.stop()
    assert not _alive(pid)
    assert svc.supervisor.status()["state"] == "stopped"


def test_restart_replaces_child_and_never_runs_two(fake_ob, ob_service, wait):
    svc = ob_service()
    wait(lambda: fake_ob.live_sync_pids())
    old_pid = svc.supervisor.pid
    svc.supervisor.start()  # second start is a no-op
    svc.supervisor.restart()
    wait(lambda: svc.supervisor.pid not in (None, old_pid))
    wait(lambda: len(fake_ob.live_sync_pids()) == 1)
    assert fake_ob.live_sync_pids() == [str(svc.supervisor.pid)]
    assert not _alive(old_pid)
    assert len(_sync_calls(fake_ob)) == 2

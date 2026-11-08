"""Fixtures for the ob CLI wrapper tests: a fake ``ob`` executable and a
service wired to it with fast supervisor timings."""

from __future__ import annotations

import json
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from obsidian_mcp.ob import service as service_mod
from obsidian_mcp.ob.service import ObService
from obsidian_mcp.ob.settings import ObSettings

_FAKE_SOURCE = Path(__file__).with_name("fake_ob.py")

_OB_ENV = (
    "ENABLE_OB",
    "ENABLE_OB_OPS",
    "ENABLE_OB_ADMIN",
    "OB_BIN",
    "OB_VAULT_PATH",
    "OB_LOG_LINES",
    "OB_COMMAND_TIMEOUT",
    "OB_RESTART_BACKOFF_MAX",
    "OBSIDIAN_AUTH_TOKEN",
)


def wait_until(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


@dataclass
class FakeOb:
    bin: Path
    vault: Path
    log: Path
    state: Path
    pids: Path
    root: Path

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def argvs(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls()]

    def live_sync_pids(self) -> list[str]:
        return sorted(p.name for p in self.pids.iterdir()) if self.pids.exists() else []

    def settings(self, **overrides) -> ObSettings:
        values = {
            "ob_bin": str(self.bin),
            "vault_path": self.vault,
            "log_lines": 50,
            "command_timeout": 5.0,
            "restart_backoff_max": 0.6,
        }
        values.update(overrides)
        return ObSettings(**values)


@pytest.fixture
def wait() -> Callable[..., None]:
    return wait_until


@pytest.fixture
def fake_ob(tmp_path: Path, monkeypatch) -> FakeOb:
    for name in _OB_ENV:
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "ob-fake"
    root.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    binary = root / "ob"
    binary.write_text(f"#!{sys.executable}\n" + _FAKE_SOURCE.read_text())
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    fake = FakeOb(
        bin=binary,
        vault=vault.resolve(),
        log=root / "calls.jsonl",
        state=root / "state.json",
        pids=root / "pids",
        root=root,
    )
    monkeypatch.setenv("FAKE_OB_LOG", str(fake.log))
    monkeypatch.setenv("FAKE_OB_STATE", str(fake.state))
    monkeypatch.setenv("FAKE_OB_SYNC_PIDS", str(fake.pids))
    # Never let a test touch a real ob credential directory.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return fake


@pytest.fixture
def ob_service(fake_ob: FakeOb):
    """A started-on-demand service bound to the fake ob; always torn down."""
    created: list[ObService] = []

    def _make(start: bool = True, **settings_overrides) -> ObService:
        svc = ObService.from_settings(
            fake_ob.settings(**settings_overrides),
            backoff_initial=0.1,
            stable_after=5.0,
            stop_timeout=2.0,
        )
        created.append(svc)
        service_mod.set_service(svc)
        if start:
            svc.supervisor.start()
        return svc

    yield _make
    for svc in created:
        svc.supervisor.stop()
    service_mod.set_service(None)

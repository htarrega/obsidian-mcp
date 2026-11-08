"""Supervisor for the long-running ``ob sync --continuous`` child process.

Thread-based on purpose: the server's watcher/indexer already run in plain
threads and ``mcp.run()`` owns its own event loop for either transport, so a
``subprocess.Popen`` plus one monitor thread works identically for stdio and
HTTP without hooking into the FastMCP lifespan.

Guarantees:

* at most one child at a time — spawning happens under a lock that also
  checks the stop flag, and :meth:`start` refuses while a monitor thread is
  alive;
* every captured line is redacted before it is stored or logged;
* crashes are restarted with exponential backoff (``backoff_initial`` doubling
  up to ``backoff_max``, reset after ``stable_after`` seconds of uptime);
  exit codes 2 (not logged in) and 3 (no sync config) keep retrying but always
  at ``backoff_max`` and are reported as ``failed`` with a reason;
* :meth:`stop` sends SIGTERM to the child's process group, waits up to
  ``stop_timeout`` seconds (``ob`` releases ``.obsidian/.sync.lock`` on
  SIGTERM), then SIGKILLs.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .redact import redact
from .runner import child_cwd, child_env, signal_process_group

logger = logging.getLogger(__name__)

MAX_LINE_CHARS = 4096
# Bytes read per readline() call; longer lines are truncated and the rest of
# the line is discarded, so one huge line cannot exhaust memory.
MAX_LINE_BYTES = MAX_LINE_CHARS * 4
MAX_ERRORS = 20
_TRUNCATED = " [line truncated]"

_ERROR_MARKERS = (
    "Sync error",
    "Sync failed",
    "Another sync instance is already running",
    "No account logged in",
    "No sync configuration found",
)
_FATAL_EXIT_REASONS = {
    2: "not logged in — the operator must run `ob login` on the host",
    3: "no sync configuration for this vault — the operator must run `ob sync-setup`",
}


class SupervisorBusyError(RuntimeError):
    """A control operation could not run now (cooldown or another in flight)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SyncSupervisor:
    def __init__(
        self,
        argv: list[str],
        cwd: Path,
        *,
        log_lines: int = 1000,
        backoff_initial: float = 2.0,
        backoff_max: float = 300.0,
        stable_after: float = 60.0,
        stop_timeout: float = 10.0,
        restart_cooldown: float = 0.0,
        control_timeout: float = 30.0,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.backoff_initial = min(backoff_initial, backoff_max)
        self.backoff_max = backoff_max
        self.stable_after = stable_after
        self.stop_timeout = stop_timeout
        self.restart_cooldown = restart_cooldown
        self.control_timeout = control_timeout
        # Extra seconds to wait for the monitor thread after the child is gone.
        self.join_grace = 5.0

        self._lock = threading.RLock()
        self._control = threading.Lock()  # serialises start/stop/restart callers
        self._logs: deque[str] = deque(maxlen=log_lines)
        self._errors: deque[str] = deque(maxlen=MAX_ERRORS)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None

        self._state = "stopped"
        self._started_at: str | None = None
        self._started_monotonic: float | None = None
        self._spawn_count = 0
        self._last_exit_code: int | None = None
        self._last_exit_at: str | None = None
        self._last_fully_synced_at: str | None = None
        self._next_restart_monotonic: float | None = None
        self._failure_reason: str | None = None
        self._last_start_monotonic: float | None = None

    # ── public API ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start supervising (no-op if already running)."""
        with self._control:
            self._start_locked()

    def stop(self) -> None:
        """Stop the child and the monitor thread; idempotent."""
        with self._control:
            self._stop_locked()

    def restart(self) -> None:
        """Stop the child (SIGTERM, wait) and launch a fresh one.

        Raises :class:`SupervisorBusyError` if the last (re)start was less
        than ``restart_cooldown`` seconds ago, or if another control
        operation does not finish within ``control_timeout`` seconds.
        """
        with self._controlled():
            if self._last_start_monotonic is not None and self.restart_cooldown > 0:
                elapsed = time.monotonic() - self._last_start_monotonic
                if elapsed < self.restart_cooldown:
                    raise SupervisorBusyError(
                        f"sync was (re)started {elapsed:.0f}s ago; wait "
                        f"{self.restart_cooldown - elapsed:.0f}s more before restarting "
                        f"(OB_RESTART_COOLDOWN={self.restart_cooldown:g})"
                    )
            self._stop_locked()
            self._start_locked()

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Stop the child for the duration of the block, then relaunch it.

        Used for admin changes: ``ob sync-config`` takes no lock and the
        continuous process only reads its config at start, so the child must
        be down while the config is rewritten and restarted afterwards. The
        child is relaunched even if the block raises.
        """
        with self._controlled():
            self._stop_locked()
            try:
                yield
            finally:
                self._start_locked()

    @contextmanager
    def _controlled(self) -> Iterator[None]:
        """Hold the control lock, giving up after ``control_timeout``."""
        if not self._control.acquire(timeout=self.control_timeout):
            raise SupervisorBusyError(
                "another sync start/stop/restart is still in progress; try again later"
            )
        try:
            yield
        finally:
            self._control.release()

    def status(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            running = proc is not None and proc.poll() is None
            uptime = (
                round(time.monotonic() - self._started_monotonic, 1)
                if running and self._started_monotonic is not None
                else None
            )
            next_restart = (
                max(0.0, round(self._next_restart_monotonic - time.monotonic(), 1))
                if self._next_restart_monotonic is not None
                and self._state in ("backoff", "failed")
                else None
            )
            return {
                "state": self._state,
                "pid": proc.pid if running and proc is not None else None,
                "started_at": self._started_at if running else None,
                "uptime_seconds": uptime,
                "restart_count": max(0, self._spawn_count - 1),
                "last_exit_code": self._last_exit_code,
                "last_exit_at": self._last_exit_at,
                "last_fully_synced_at": self._last_fully_synced_at,
                "next_restart_in_seconds": next_restart,
                "failure_reason": self._failure_reason,
                "recent_errors": list(self._errors),
                "command": [redact(part) for part in self.argv],
            }

    def logs(self, lines: int) -> list[str]:
        with self._lock:
            if lines <= 0:
                return []
            return list(self._logs)[-lines:]

    @property
    def pid(self) -> int | None:
        with self._lock:
            proc = self._proc
            return proc.pid if proc is not None and proc.poll() is None else None

    # ── internals ─────────────────────────────────────────────────────────

    def _start_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            if not self._stop_event.is_set():
                return  # already running
            # A previous stop could not reap the monitor thread; starting a
            # second one could run two children, so refuse loudly instead of
            # silently doing nothing.
            message = (
                "previous ob sync monitor thread has not exited yet; "
                "cannot start a new sync process"
            )
            logger.error(message)
            with self._lock:
                self._state = "failed"
                self._failure_reason = message
            raise SupervisorBusyError(message)
        stop_event = threading.Event()
        with self._lock:
            self._stop_event = stop_event
            self._state = "starting"
            self._failure_reason = None
            self._next_restart_monotonic = None
            self._last_start_monotonic = time.monotonic()
        thread = threading.Thread(
            target=self._monitor, args=(stop_event,), name="ob-sync-supervisor", daemon=True
        )
        self._thread = thread
        thread.start()

    def _stop_locked(self) -> None:
        with self._lock:
            self._stop_event.set()
            proc = self._proc
            thread = self._thread
        if proc is not None and proc.poll() is None:
            signal_process_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                logger.warning("ob sync child %s ignored SIGTERM; sending SIGKILL", proc.pid)
                signal_process_group(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.error("ob sync child %s did not die after SIGKILL", proc.pid)
        stuck = False
        if thread is not None:
            thread.join(timeout=self.stop_timeout + self.join_grace)
            if thread.is_alive():
                stuck = True
            else:
                self._thread = None
        with self._lock:
            self._next_restart_monotonic = None
            if stuck:
                self._state = "failed"
                self._failure_reason = (
                    "ob sync monitor thread did not exit after stop; "
                    "restart the server if this persists"
                )
            else:
                self._state = "stopped"
        if stuck:
            logger.error("ob sync monitor thread did not exit after stop")
            self._record("[supervisor] monitor thread did not exit after stop")

    def _spawn(self, stop_event: threading.Event) -> subprocess.Popen[bytes] | None:
        with self._lock:
            if stop_event.is_set():
                return None
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(self.cwd),
                env=child_env(),
                shell=False,
                start_new_session=True,
            )
            self._proc = proc
            self._spawn_count += 1
            self._state = "running"
            self._started_at = _now()
            self._started_monotonic = time.monotonic()
            self._next_restart_monotonic = None
            self._failure_reason = None
        self._record(f"[supervisor] started ob sync (pid {proc.pid})")
        logger.info("Started ob sync --continuous (pid %s)", proc.pid)
        return proc

    def _record(self, line: str, *, truncated: bool = False) -> None:
        line = redact(line.rstrip("\r\n"))
        if truncated or len(line) > MAX_LINE_CHARS:
            line = line[: MAX_LINE_CHARS - len(_TRUNCATED)] + _TRUNCATED
        stamped = f"{_now()} {line}"
        with self._lock:
            self._logs.append(stamped)
            if "Fully synced" in line:
                self._last_fully_synced_at = _now()
            if any(marker in line for marker in _ERROR_MARKERS):
                self._errors.append(stamped)
                logger.warning("ob sync: %s", line)

    def _pump(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        discarding = False  # inside the tail of an over-long line
        while True:
            raw = proc.stdout.readline(MAX_LINE_BYTES)
            if not raw:
                break
            complete = raw.endswith(b"\n")
            if not discarding:
                self._record(raw.decode("utf-8", errors="replace"), truncated=not complete)
            discarding = not complete
        proc.stdout.close()

    def _wait_for_restart(
        self, stop_event: threading.Event, delay: float, state: str, reason: str | None
    ) -> None:
        with self._lock:
            self._state = state
            self._failure_reason = reason
            self._next_restart_monotonic = time.monotonic() + delay
        stop_event.wait(delay)

    def _monitor(self, stop_event: threading.Event) -> None:
        backoff = self.backoff_initial
        while not stop_event.is_set():
            try:
                proc = self._spawn(stop_event)
            except OSError as exc:
                message = f"cannot execute {self.argv[0]!r}: {exc.strerror or exc}"
                self._record(f"[supervisor] {message}")
                with self._lock:
                    self._errors.append(f"{_now()} {redact(message)}")
                self._wait_for_restart(stop_event, self.backoff_max, "failed", redact(message))
                continue
            if proc is None:
                break
            self._pump(proc)
            returncode = proc.wait()
            uptime = time.monotonic() - (self._started_monotonic or time.monotonic())
            with self._lock:
                self._last_exit_code = returncode
                self._last_exit_at = _now()
            self._record(f"[supervisor] ob sync exited with code {returncode}")
            if stop_event.is_set():
                break
            with self._lock:
                self._errors.append(f"{_now()} ob sync exited with code {returncode}")
            logger.warning("ob sync exited with code %s after %.1fs", returncode, uptime)
            if uptime >= self.stable_after:
                backoff = self.backoff_initial
            reason = _FATAL_EXIT_REASONS.get(returncode)
            if reason is not None:
                self._wait_for_restart(stop_event, self.backoff_max, "failed", reason)
            else:
                self._wait_for_restart(
                    stop_event, backoff, "backoff", f"exited with code {returncode}"
                )
                backoff = min(backoff * 2, self.backoff_max)
        with self._lock:
            self._state = "stopped"
            self._next_restart_monotonic = None


def make_supervisor(settings: Any, **overrides: Any) -> SyncSupervisor:
    """Build the supervisor for :class:`~obsidian_mcp.ob.settings.ObSettings`."""
    kwargs: dict[str, Any] = {
        "log_lines": settings.log_lines,
        "backoff_max": settings.restart_backoff_max,
        "restart_cooldown": settings.restart_cooldown,
    }
    kwargs.update(overrides)
    return SyncSupervisor(settings.sync_argv(), child_cwd(settings.vault_path), **kwargs)


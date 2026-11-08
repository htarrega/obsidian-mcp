"""Process-wide ob service: settings + one-shot runner + sync supervisor.

``start_ob()`` is called once from ``server.main()`` when ``ENABLE_OB`` is
set. It validates the ``OB_*`` settings (a :class:`ConfigError` aborts
startup with a clear message), launches the supervisor and arranges for the
child to be terminated when the server exits.
"""

from __future__ import annotations

import atexit
import logging
import signal
import threading
from dataclasses import dataclass
from typing import Any

from .redact import register_secret_file
from .runner import ObRunner
from .settings import ObSettings, auth_token_file
from .supervisor import SyncSupervisor, make_supervisor

logger = logging.getLogger(__name__)


@dataclass
class ObService:
    settings: ObSettings
    runner: ObRunner
    supervisor: SyncSupervisor

    @classmethod
    def from_settings(cls, settings: ObSettings, **supervisor_overrides: Any) -> ObService:
        return cls(
            settings=settings,
            runner=ObRunner(settings),
            supervisor=make_supervisor(settings, **supervisor_overrides),
        )


_service: ObService | None = None
_service_lock = threading.Lock()
_exit_hooks_installed = False


def get_service() -> ObService | None:
    return _service


def set_service(service: ObService | None) -> None:
    """Install (or clear) the process-wide service; used by start_ob and tests."""
    global _service
    _service = service


def _install_exit_hooks() -> None:
    """Stop the child on interpreter exit and on SIGTERM.

    ``atexit`` covers normal shutdown (stdio EOF, uvicorn's graceful
    shutdown). A SIGTERM handler is only installed when the default handler
    is in place, so it never overrides one the transport installed; it stops
    the child and then re-delivers SIGTERM with the default action.
    """
    global _exit_hooks_installed
    if _exit_hooks_installed:
        return
    _exit_hooks_installed = True
    atexit.register(stop_ob)
    if threading.current_thread() is not threading.main_thread():
        return
    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
        return

    def _on_sigterm(signum: int, frame: object) -> None:
        stop_ob()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.raise_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, _on_sigterm)


def start_ob(settings: ObSettings | None = None) -> ObService:
    """Validate settings, start the sync supervisor and return the service."""
    with _service_lock:
        if _service is not None:
            return _service
        resolved = settings or ObSettings.from_env()
        service = ObService.from_settings(resolved)
        set_service(service)
    _install_exit_hooks()
    # Mask the ob login token verbatim in everything we return or log. Read
    # once here; the content is never returned or logged.
    if register_secret_file(auth_token_file()):
        logger.info("Registered the ob auth_token for redaction")
    logger.info(
        "Starting ob sync supervisor for %s (bin=%s)", resolved.vault_path, resolved.ob_bin
    )
    service.supervisor.start()
    return service


def stop_ob() -> None:
    """Terminate the supervised child (SIGTERM, then SIGKILL after ~10s)."""
    service = _service
    if service is not None:
        service.supervisor.stop()

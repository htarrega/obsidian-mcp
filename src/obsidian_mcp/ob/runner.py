"""One-shot ``ob`` command runner with an argument allowlist.

Every invocation is ``[OB_BIN, *args]`` executed without a shell, with a
timeout (the process group is killed when it expires), a per-stream output
cap and redaction of everything that comes back. ``args`` are checked
against :data:`_ALLOWED` before anything is spawned, so a bug elsewhere can
never turn into ``ob login``/``sync-unlink``/``publish``/arbitrary flags.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from typing import IO, Any

from .redact import redact, redact_obj
from .settings import ObSettings

MAX_OUTPUT_BYTES = 256 * 1024

# subcommand -> {flag: takes_value}. ``--version`` is a bare top-level flag.
_ALLOWED: dict[str, dict[str, bool]] = {
    "--version": {},
    "sync-status": {"--path": True, "--json": False},
    "sync-list-local": {"--json": False},
    "sync-list-remote": {"--json": False},
    "sync-config": {
        "--path": True,
        "--json": False,
        "--excluded-folders": True,
        "--file-types": True,
        "--configs": True,
        "--conflict-strategy": True,
    },
}


class ObError(Exception):
    """Base error of the ob wrapper; messages are already redacted."""


class ObArgumentError(ObError):
    """An argv that is not on the allowlist (a programming error)."""


class ObCommandError(ObError):
    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class ObTimeoutError(ObError):
    pass


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    truncated: bool
    # Unredacted stdout, kept only so JSON can be parsed before structural
    # redaction. Never return or log it.
    _raw_stdout: str = field(default="", repr=False)

    def unredacted_json(self) -> Any:
        """Parsed stdout *before* redaction — for internal comparisons only."""
        try:
            return json.loads(self._raw_stdout)
        except ValueError as exc:
            hint = " (output was truncated)" if self.truncated else ""
            raise ObCommandError(f"ob returned invalid JSON{hint}") from exc

    def json(self) -> Any:
        """Parse stdout as JSON and redact it structurally."""
        return redact_obj(self.unredacted_json())


def validate_args(args: list[str]) -> None:
    """Raise :class:`ObArgumentError` unless ``args`` is on the allowlist."""
    if not args or not all(isinstance(arg, str) for arg in args):
        raise ObArgumentError("ob arguments must be a non-empty list of strings")
    subcommand, rest = args[0], args[1:]
    flags = _ALLOWED.get(subcommand)
    if flags is None:
        raise ObArgumentError(f"ob subcommand not allowed: {subcommand!r}")
    seen: set[str] = set()
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag not in flags:
            raise ObArgumentError(f"ob flag not allowed for {subcommand}: {flag!r}")
        if flag in seen:
            raise ObArgumentError(f"duplicate ob flag: {flag}")
        seen.add(flag)
        if flags[flag]:
            if index + 1 >= len(rest):
                raise ObArgumentError(f"ob flag {flag} requires a value")
            value = rest[index + 1]
            if value.startswith("-") or "\x00" in value or "\n" in value or "\r" in value:
                raise ObArgumentError(f"invalid value for ob flag {flag}")
            index += 2
        else:
            index += 1


def _drain(stream: IO[bytes], sink: bytearray, state: dict[str, bool]) -> None:
    """Read ``stream`` to EOF, keeping at most MAX_OUTPUT_BYTES."""
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        room = MAX_OUTPUT_BYTES - len(sink)
        if room > 0:
            sink.extend(chunk[:room])
        if len(chunk) > max(room, 0):
            state["truncated"] = True
    stream.close()


def signal_process_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child's whole process group (it runs in its own session)."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)


class ObRunner:
    """Runs allowlisted one-shot ``ob`` commands for one configured vault."""

    def __init__(self, settings: ObSettings) -> None:
        self.settings = settings

    @property
    def vault_arg(self) -> str:
        return str(self.settings.vault_path)

    def run(self, args: list[str], *, check: bool = True) -> RunResult:
        validate_args(args)
        argv = [self.settings.ob_bin, *args]
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv list, no shell, allowlisted
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.settings.vault_path),
                shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            raise ObCommandError(
                redact(f"cannot execute {self.settings.ob_bin!r}: {exc.strerror or exc}")
            ) from exc

        out, err = bytearray(), bytearray()
        state = {"truncated": False}
        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, out, state), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, err, state), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            returncode = proc.wait(timeout=self.settings.command_timeout)
        except subprocess.TimeoutExpired as exc:
            signal_process_group(proc, signal.SIGKILL)
            proc.wait()
            for reader in readers:
                reader.join(timeout=2)
            raise ObTimeoutError(
                f"ob {args[0]} timed out after {self.settings.command_timeout:g}s and was killed"
            ) from exc
        for reader in readers:
            reader.join(timeout=5)

        raw_stdout = out.decode("utf-8", errors="replace")
        result = RunResult(
            returncode=returncode,
            stdout=redact(raw_stdout),
            stderr=redact(err.decode("utf-8", errors="replace")),
            truncated=state["truncated"],
            _raw_stdout=raw_stdout,
        )
        if check and returncode != 0:
            raise ObCommandError(describe_failure(args[0], result), returncode)
        return result


_EXIT_HINTS = {
    2: "not logged in or missing key — the operator must run `ob login` on the host",
    3: "no sync configuration for this vault — the operator must run `ob sync-setup`",
}


def describe_failure(subcommand: str, result: RunResult) -> str:
    """A short, redacted, human-readable failure message."""
    detail = (result.stderr.strip() or result.stdout.strip())[-1000:]
    message = f"ob {subcommand} exited with code {result.returncode}"
    hint = _EXIT_HINTS.get(result.returncode)
    if hint:
        message += f" ({hint})"
    if detail:
        message += f": {detail}"
    return message

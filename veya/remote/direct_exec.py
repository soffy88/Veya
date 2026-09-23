"""Direct, streaming command execution for the remote MCP fast path (P0-B/F/H/P).

This is the *direct execution* path: an MCP ``shell.exec`` / ``test.run`` /
``build.run`` call runs a real local subprocess without Hicode, MasterAgent, any
agent loop or LLM provider. It reuses the canonical command policy and sandbox
wrapping from :mod:`runtime.coding.command_runner` (one command authority),
but streams stdout/stderr incrementally instead of ``subprocess.run``-and-wait.

Hard properties:

* output is streamed line/chunk-wise to a bounded tail store so
  ``process.status`` can show progress while the command runs;
* the child is started in its own process session, so cancellation terminates
  the whole process group (pytest / npm / ffmpeg workers), with a SIGTERM grace
  period before SIGKILL and a reap;
* a timeout kills the group and is reported as ``timeout``;
* client disconnect never reaches here — the durable job owns the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from runtime.coding.command_runner import (
    _SANDBOX_DEPTH_ENV,
    CommandPolicyError,
    CommandRunner,
    _nested_write_path_violation,
    _safe_environment,
    command_may_use_network,
    command_requires_approval,
    parse_command,
    redact_text,
)

TAIL_LINES = 200
TAIL_BYTES = 32_000
DEFAULT_DIRECT_TIMEOUT_S = 900.0
SYNC_WINDOW_MS_DEFAULT = 800
SYNC_WINDOW_MS_MIN = 200
SYNC_WINDOW_MS_MAX = 1500
_READ_CHUNK = 4096


def direct_sync_window_s() -> float:
    """DIRECT_SYNC_WINDOW_MS (clamped 200..1500), returned in seconds (P0-B)."""

    raw = os.environ.get("VEYA_REMOTE_DIRECT_SYNC_WINDOW_MS", "")
    try:
        value = int(raw) if raw else SYNC_WINDOW_MS_DEFAULT
    except ValueError:
        value = SYNC_WINDOW_MS_DEFAULT
    value = max(SYNC_WINDOW_MS_MIN, min(SYNC_WINDOW_MS_MAX, value))
    return value / 1000.0


@dataclass
class DirectCommandResult:
    """Bounded, redacted result of one direct command."""

    command: str
    argv: list[str]
    cwd: str
    profile: str
    status: str  # passed | failed | timeout | denied | approval_required
    exit_code: int | None
    stdout_tail: str = ""
    stderr_tail: str = ""
    bytes_stdout: int = 0
    bytes_stderr: int = 0
    duration_ms: float = 0.0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    requires_approval: bool = False

    def to_public(self) -> dict[str, object]:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "profile": self.profile,
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "bytes_stdout": self.bytes_stdout,
            "bytes_stderr": self.bytes_stderr,
            "duration_ms": round(self.duration_ms, 3),
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "timed_out": self.timed_out,
            "requires_approval": self.requires_approval,
        }


@dataclass
class _Tail:
    lines: list[str] = field(default_factory=list)
    partial: str = ""
    text: str = ""
    bytes_total: int = 0

    def append(self, chunk: str) -> list[str]:
        """Accumulate ``chunk``; return the complete lines produced by it."""

        self.bytes_total += len(chunk.encode("utf-8", "replace"))
        self.text = (self.text + chunk)[-TAIL_BYTES:]
        self.partial += chunk
        produced: list[str] = []
        while "\n" in self.partial:
            line, self.partial = self.partial.split("\n", 1)
            produced.append(line + "\n")
        if produced:
            self.lines.extend(produced)
            del self.lines[: max(0, len(self.lines) - TAIL_LINES)]
        return produced

    def flush_partial(self) -> None:
        if self.partial:
            self.lines.append(self.partial)
            self.partial = ""
            del self.lines[: max(0, len(self.lines) - TAIL_LINES)]

    def tail_text(self) -> str:
        return "".join(self.lines)[-TAIL_BYTES:]

    @property
    def truncated(self) -> bool:
        return self.bytes_total > TAIL_BYTES or len(self.text) >= TAIL_BYTES


def _secret_values() -> list[str]:
    from runtime.coding.command_runner import _SECRET_NAME

    return [value for key, value in os.environ.items() if _SECRET_NAME.search(key) and value]


async def _terminate_group(proc: asyncio.subprocess.Process, *, grace_s: float = 0.5) -> None:
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
    else:  # pragma: no cover - defensive
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_s)
        return
    except TimeoutError:
        pass
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
    else:  # pragma: no cover - defensive
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=2.0)


async def _pump(
    stream: asyncio.StreamReader | None,
    tail: _Tail,
    on_line: Callable[[str], None] | None,
) -> None:
    if stream is None:  # pragma: no cover - defensive
        return
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        for line in tail.append(chunk.decode("utf-8", "replace")):
            if on_line is not None:
                on_line(line)
    tail.flush_partial()
    if tail.partial and on_line is not None:  # pragma: no cover - flushed above
        on_line(tail.partial)


def _plan_execution(
    workspace_root: Path,
    command: str | Sequence[str],
    *,
    cwd: Path,
    profile: str,
    approved: bool,
    network: str | None,
) -> tuple[CommandRunner, list[str], list[str], str | None, str]:
    """Return ``(runner, argv, execution_argv, execution_cwd, command_text)``.

    Policy checks mirror :meth:`CommandRunner.run`; the sandbox argv wrapping is
    delegated to the canonical runner so there is exactly one sandbox authority.
    """

    runner = CommandRunner(workspace_root, profile=profile)
    argv = parse_command(command)
    command_text = command if isinstance(command, str) else " ".join(argv)
    if runner.profile.network == "denied" and command_may_use_network(argv):
        raise DirectDenied("网络访问被沙箱 profile 禁止 (network denied by sandbox profile)")
    if command_requires_approval(argv) and not approved:
        raise DirectApprovalRequired(
            "该命令需要显式批准 (destructive/package/remote command requires approval)"
        )
    violation = _nested_write_path_violation(argv, runner.workspace_root, cwd)
    if violation:
        raise DirectDenied(violation)
    execution_argv = list(argv)
    execution_cwd: str | None = str(cwd)
    if Path(execution_argv[0]).name == "pytest":
        import importlib.util
        import sys

        if importlib.util.find_spec("pytest"):
            execution_argv = [sys.executable, "-m", "pytest", *execution_argv[1:]]
    if runner.profile.executor == "docker":
        execution_argv = runner._docker_argv(execution_argv, cwd, network)
        execution_cwd = None
    elif runner.profile.id == "local_restricted" and os.environ.get(_SANDBOX_DEPTH_ENV) != "1":
        execution_argv = runner._local_restricted_argv(execution_argv, cwd)
        execution_cwd = None
    return runner, argv, execution_argv, execution_cwd, command_text


class DirectDenied(Exception):
    pass


class DirectApprovalRequired(Exception):
    pass


class DirectOutcome:
    denied = "denied"
    approval_required = "approval_required"


async def run_direct_command(
    workspace_root: str | Path,
    command: str | Sequence[str],
    *,
    cwd: str | Path | None = None,
    profile: str = "local_restricted",
    timeout_s: float = DEFAULT_DIRECT_TIMEOUT_S,
    approved: bool = False,
    network: str | None = None,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    on_process: Callable[[int, int], None] | None = None,
) -> DirectCommandResult:
    """Run one command with incremental stdout/stderr streaming.

    Raises :class:`DirectDenied` / :class:`DirectApprovalRequired` for policy
    refusals; otherwise returns a bounded result (non-zero exit is ``failed``,
    timeout is ``timeout``, cancellation re-raises ``CancelledError`` after the
    process group is reaped).
    """

    started = time.monotonic()
    workspace = Path(workspace_root).expanduser().resolve()
    target = Path(cwd).expanduser().resolve() if cwd else workspace
    if not target.is_dir() or (target != workspace and workspace not in target.parents):
        raise DirectDenied("cwd must remain inside the task workspace")
    if timeout_s <= 0:
        raise CommandPolicyError("timeout_s must be positive")

    runner, argv, execution_argv, execution_cwd, command_text = _plan_execution(
        workspace, command, cwd=target, profile=profile, approved=approved, network=network
    )
    environment = _safe_environment(None)
    workspace_bin = workspace / "venv" / "bin"
    if workspace_bin.is_dir():
        environment["PATH"] = os.pathsep.join(
            [str(workspace_bin), environment.get("PATH", os.defpath)]
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            *execution_argv,
            cwd=execution_cwd,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return DirectCommandResult(
            command=command_text,
            argv=argv,
            cwd=str(target),
            profile=runner.profile.id,
            status="failed",
            exit_code=None,
            stderr_tail=f"unable to execute command: {exc}",
            duration_ms=(time.monotonic() - started) * 1000,
        )

    if on_process is not None:
        with contextlib.suppress(OSError):
            on_process(int(proc.pid), int(os.getpgid(proc.pid) or proc.pid))

    stdout_tail = _Tail()
    stderr_tail = _Tail()
    readers = [
        asyncio.ensure_future(_pump(proc.stdout, stdout_tail, on_stdout)),
        asyncio.ensure_future(_pump(proc.stderr, stderr_tail, on_stderr)),
    ]
    status = "failed"
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        status = "passed" if proc.returncode == 0 else "failed"
    except TimeoutError:
        timed_out = True
        status = "timeout"
        await _terminate_group(proc)
    except asyncio.CancelledError:
        await _terminate_group(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=2.0)
        raise
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=3.0)
        if proc.returncode is None:  # pragma: no cover - defensive
            await _terminate_group(proc)

    secrets = _secret_values()
    return DirectCommandResult(
        command=redact_text(command_text, secret_values=secrets),
        argv=[redact_text(item, secret_values=secrets) for item in argv],
        cwd=str(target),
        profile=runner.profile.id,
        status=status,
        exit_code=proc.returncode,
        stdout_tail=redact_text(stdout_tail.tail_text(), secret_values=secrets),
        stderr_tail=redact_text(stderr_tail.tail_text(), secret_values=secrets),
        bytes_stdout=stdout_tail.bytes_total,
        bytes_stderr=stderr_tail.bytes_total,
        duration_ms=(time.monotonic() - started) * 1000,
        stdout_truncated=stdout_tail.truncated,
        stderr_truncated=stderr_tail.truncated,
        timed_out=timed_out,
    )


async def terminate_process_group(
    proc: asyncio.subprocess.Process, *, grace_s: float = 0.5
) -> None:
    """Public alias for the SIGTERM->grace->SIGKILL->reap sequence (P0-P)."""

    await _terminate_group(proc, grace_s=grace_s)


async def terminate_process_group_id(pgid: int, *, grace_s: float = 0.5) -> None:
    """SIGTERM an execution-owned process group, grace, then SIGKILL (P1 cancel).

    Never signals our own process group. Used when the direct child may already
    have been reaped (e.g. Hicode's own finally) but its grandchildren survive.
    """

    if pgid <= 1 or pgid == os.getpgid(0):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        await asyncio.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


__all__ = [
    "DEFAULT_DIRECT_TIMEOUT_S",
    "SYNC_WINDOW_MS_DEFAULT",
    "DirectApprovalRequired",
    "DirectCommandResult",
    "DirectDenied",
    "DirectOutcome",
    "direct_sync_window_s",
    "run_direct_command",
    "terminate_process_group",
    "terminate_process_group_id",
]

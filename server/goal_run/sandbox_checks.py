"""Run check commands against a fresh scratch copy of a tree.

Isolation level, stated honestly: the commands run in a NEW COPY of the
tree (never the live working tree), argv-only (no shell), with a
scrubbed environment (no secrets, no proxy), a per-command timeout, and
the scratch copy removed afterwards.

What this is NOT: the srt sandbox. Neither sandbox honors a
caller-supplied worktree -- the remote daemon rejects ``working_dir``
explicitly, and the local sandbox runs every command in its own fresh
temp dir -- so there is no supported way to aim srt at a PR tree today.
Running checks through srt needs a daemon protocol change (send the
tree, unpack it into the daemon's workdir) and is a separate, security
boundary decision. Until then, this module does not claim sandbox
isolation: it claims fresh-copy + no-shell + scrubbed-env + timeout,
which is what the code actually provides.

Commands come from the delivery config (owner-authored, main branch
only). Nothing else may add commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CHECKS_COMPLETED_TOPIC = "checks.completed"

_COMMAND_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_OUTPUT_LIMIT = 4000


@dataclass
class CommandResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False


@dataclass
class ChecksResult:
    passed: bool
    commands: list[CommandResult] = field(default_factory=list)
    reason: str = ""


def validate_commands(commands: Any) -> list[list[str]]:
    """Fail-closed command validation. Raises ValueError on any bad shape."""

    if not isinstance(commands, list) or not commands:
        raise ValueError("checks must be a non-empty list of argv arrays")
    validated: list[list[str]] = []
    for command in commands:
        if not isinstance(command, list) or not command:
            raise ValueError("each check must be a non-empty argv array")
        if not all(isinstance(arg, str) for arg in command):
            raise ValueError("check argv entries must be strings")
        if not _COMMAND_RE.match(command[0]) or "/" in command[0]:
            raise ValueError(f"check command must be a bare name: {command[0]!r}")
        for arg in command:
            if "\x00" in arg or "\n" in arg or "\r" in arg:
                raise ValueError("check argv entries must not contain control characters")
            # Checks run with cwd inside a scratch copy: an absolute path
            # or a parent reference would act outside that copy, so both
            # are rejected. Legitimate checks are cwd-relative. The check
            # also covers "--flag=/abs" style values.
            for part in arg.split("="):
                if part.startswith("/") or part == ".." or part.startswith("../") or part.endswith("/..") or "/../" in part:
                    raise ValueError(f"check argv must stay inside the scratch copy: {arg!r}")
        validated.append(list(command))
    return validated


def _scrubbed_env(scratch: str) -> dict[str, str]:
    """Minimal environment: no secrets, no proxy, writable HOME/TMPDIR."""

    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": scratch,
        "TMPDIR": scratch,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


async def _run_one(
    argv: list[str], cwd: str, timeout_s: float
) -> CommandResult:
    import time

    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=_scrubbed_env(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as exc:
        return CommandResult(
            argv=argv,
            exit_code=-2,
            stdout="",
            stderr=f"could not start: {type(exc).__name__}: {exc}",
            duration_s=0.0,
        )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        timed_out = False
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = await proc.communicate()
    duration = time.monotonic() - started
    return CommandResult(
        argv=argv,
        exit_code=-1 if timed_out else int(proc.returncode or 0),
        stdout=stdout.decode("utf-8", "replace")[:_OUTPUT_LIMIT],
        stderr=stderr.decode("utf-8", "replace")[:_OUTPUT_LIMIT],
        duration_s=duration,
        timed_out=timed_out,
    )


async def run_checks(
    *,
    tree: str,
    commands: list[list[str]],
    timeout_s: float = 600,
    scratch_root: str | None = None,
    executor: Callable[..., Awaitable[CommandResult]] | None = None,
) -> ChecksResult:
    """Copy ``tree`` to scratch and run ``commands`` in the copy. Never raises."""

    try:
        validated = validate_commands(commands)
    except ValueError as exc:
        return ChecksResult(passed=False, reason=str(exc))
    source = Path(tree)
    if not source.is_dir():
        return ChecksResult(passed=False, reason=f"tree is not a directory: {tree}")
    root = scratch_root or os.environ.get("VEYA_WORK_ROOT", str(Path.home() / ".veya" / "work"))
    scratch = tempfile.mkdtemp(prefix="checks-", dir=root) if Path(root).is_dir() else tempfile.mkdtemp(prefix="checks-")
    try:
        shutil.copytree(source, scratch, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
        results: list[CommandResult] = []
        for argv in validated:
            if executor is not None:
                result = await executor(argv, scratch, timeout_s)
            else:
                result = await _run_one(argv, scratch, timeout_s)
            results.append(result)
            if result.exit_code != 0:
                reason = (
                    f"check failed: {' '.join(argv)} "
                    f"(exit {result.exit_code}{' timeout' if result.timed_out else ''})"
                )
                return ChecksResult(passed=False, commands=results, reason=reason)
        return ChecksResult(passed=True, commands=results, reason="all checks passed")
    except Exception as exc:
        return ChecksResult(passed=False, reason=f"checks harness failed: {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def record_checks(
    events: Any, subject_id: str, diff_hash: str, result: ChecksResult
) -> None:
    """Append the ``checks.completed`` record the merge gate reads."""

    events.append(
        {
            "topic": CHECKS_COMPLETED_TOPIC,
            "task_id": subject_id,
            "actor": "system",
            "payload": {
                "subject_id": subject_id,
                "diff_hash": diff_hash,
                "passed": result.passed,
                "reason": result.reason,
                "commands": [
                    {
                        "argv": item.argv,
                        "exit_code": item.exit_code,
                        "timed_out": item.timed_out,
                        "stdout": item.stdout,
                        "stderr": item.stderr,
                    }
                    for item in result.commands
                ],
            },
        }
    )

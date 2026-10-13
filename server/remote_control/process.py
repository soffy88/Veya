# 3O-IO-ALLOW — server/remote_control/process.py
# 理由：这是 W13 包里唯一 import subprocess 的文件，也是唯一允许进程 I/O 的
# 地方。`tailscale whois --json <ip>` 必须由真实子进程执行，远程 executor
# 启动（ssh / 本机）同样如此；两件事共用同一个 argv-list、shell=False、
# 固定超时/固定二进制的注入点，所以直接-IO 网关只需要豁免这一个文件。
# 仓库既有同形豁免见 server/exec_process.py:1 与 server/supervisor/binding.py:1-8。
# 测试从不调用本模块的真实路径：identity/executor 都持有可注入的 seam，
# 测试注入假实现，因此测试套件不触碰任何真实 peer。
"""The one process-execution seam W13 owns.

Two operations, both argv-list, both ``shell=False``, both time-bounded:

* :func:`run_tailscale` — ``tailscale whois --json <ip>`` (identity).
* :func:`spawn` — start a remote agent executor locally or over ssh.

Everything else in ``server/remote_control/`` is pure and takes these as
injectable callables, so the test suite never forks a process.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Sequence

#: Seconds before a whois lookup is considered dead. A hung tailscaled must
#: never hold an HTTP request open.
WHOIS_TIMEOUT_S = 5.0

#: Seconds before a started executor is reported as a launch failure.
SPAWN_TIMEOUT_S = 10.0


class ProcessUnavailable(RuntimeError):
    """The binary is missing, timed out, or exited non-zero."""


@dataclass
class ProcessHandle:
    """A started process. ``pid`` is None when the spawn was mocked out."""

    argv: list[str] = field(default_factory=list)
    pid: int | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "pid": self.pid,
            "returncode": self.returncode,
            "ok": self.ok,
        }


def _resolve(binary: str) -> str:
    found = shutil.which(binary)
    if not found:
        raise ProcessUnavailable(f"{binary} not found on PATH")
    return found


def run_tailscale(argv: Sequence[str], *, timeout_s: float = WHOIS_TIMEOUT_S) -> str:
    """Run one ``tailscale ...`` subcommand and return its stdout.

    ``argv[0]`` must be ``tailscale`` — this is deliberately not a general
    exec helper.
    """

    args = [str(item) for item in (argv or ())]
    if not args or args[0] != "tailscale":
        raise ProcessUnavailable("only `tailscale ...` may be run here")
    binary = _resolve("tailscale")
    try:
        completed = subprocess.run(  # noqa: S603 — argv list, shell=False, fixed binary
            [binary, *args[1:]],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcessUnavailable(f"tailscale timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise ProcessUnavailable(f"tailscale could not be executed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[:200]
        raise ProcessUnavailable(f"tailscale exited {completed.returncode}: {detail}")
    return completed.stdout


def spawn(
    argv: Sequence[str],
    *,
    cwd: str = "",
    timeout_s: float = SPAWN_TIMEOUT_S,
) -> ProcessHandle:
    """Start one executor command and wait briefly for its first outcome.

    A long-running agent is expected: ``timeout_s`` bounds only the *launch*
    (fork + exec), and a process still alive at the bound is reported with
    ``returncode=None`` rather than being killed — cancelling is the session
    layer's job, and it goes through the approval path.
    """

    args = [str(item) for item in (argv or ())]
    if not args:
        raise ProcessUnavailable("empty argv")
    binary = _resolve(args[0])
    try:
        process = subprocess.Popen(  # noqa: S603 — argv list, shell=False
            [binary, *args[1:]],
            cwd=cwd or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise ProcessUnavailable(f"{args[0]} could not be started: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
        return ProcessHandle(
            argv=args,
            pid=process.pid,
            returncode=process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
        )
    except subprocess.TimeoutExpired:
        return ProcessHandle(argv=args, pid=process.pid, returncode=None)


__all__ = [
    "SPAWN_TIMEOUT_S",
    "WHOIS_TIMEOUT_S",
    "ProcessHandle",
    "ProcessUnavailable",
    "run_tailscale",
    "spawn",
]

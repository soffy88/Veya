"""Process guards shared by the executors (hicode / dsh).

A supervision runtime must not leave an executor running after its supervisor
dies: an orphaned executor keeps producing side effects for an execution that
reconciliation has already marked interrupted, and a later retry would then
duplicate them.
"""

from __future__ import annotations

import os

PR_SET_PDEATHSIG = 1


def die_with_parent() -> None:
    """preexec hook: ask the kernel to SIGKILL this child when the parent dies.

    Linux-only; silently does nothing elsewhere or when libc/prctl is missing, so
    it can never break a dispatch.
    """
    try:  # pragma: no cover - depends on host libc
        import ctypes
        import signal

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    except Exception:  # best effort, never fatal
        return


def executor_spawn_kwargs() -> dict:
    """Canonical spawn shape for every Veya executor.

    ``start_new_session`` gives the executor its own process group, which is what
    makes durable group ownership possible (see server/exec_process.py).
    ``die_with_parent`` is the cheap first line of defence; the durable group
    record is the authoritative one, because executors that fork (reasonix) can
    outlive the parent-death signal.
    """
    return {
        "start_new_session": True,
        "preexec_fn": die_with_parent if os.name == "posix" else None,
    }


__all__ = ["die_with_parent", "executor_spawn_kwargs"]

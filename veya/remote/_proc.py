"""Cancellation-safe child process spawning for the remote execution plane.

Why this exists (SEC F10): ``asyncio.create_subprocess_exec`` can hang forever
when the loop shuts down (or every task is cancelled) while a spawn is still in
flight.  CPython 3.12's ``_make_subprocess_transport`` reacts to the cancel with
``transport.close(); await transport._wait()``, but ``_wait`` only resolves once
every pipe protocol is connected *and* disconnected; the internal
``_connect_pipes`` task was cancelled too, so a pipe slot stays ``None`` and
``_try_finish`` never fires.  The cancelled task then pins
``asyncio.runners._cancel_all_tasks`` (observed in
``tests/remote/test_local2_unlimited_concurrency.py`` teardown under bwrap).

``spawn_process`` starts the child with a plain ``subprocess.Popen`` (in its own
session / process group, exactly like before), attaches asyncio
``StreamReader``s to its pipes and detects exit through a pidfd reader.  No
child watcher and no ``transport._wait`` are involved, so cancellation at any
point either propagates immediately or kills the half-started child.  The
returned object exposes the subset of ``asyncio.subprocess.Process`` used by
the callers: ``pid``, ``returncode``, ``stdout``, ``stderr``, ``wait()``,
``send_signal()``, ``terminate()``, ``kill()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping
from typing import Any

_PIPE = asyncio.subprocess.PIPE
_DEVNULL = asyncio.subprocess.DEVNULL


class SpawnedProcess:
    """Minimal ``asyncio.subprocess.Process`` look-alike over a ``Popen``."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen
        self.pid = popen.pid
        self.stdin = None
        self.stdout: asyncio.StreamReader | None = None
        self.stderr: asyncio.StreamReader | None = None
        self._exit: asyncio.Future[int] | None = None
        self._pidfd: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def returncode(self) -> int | None:
        return self._popen.poll()

    def _watch_exit(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._exit = loop.create_future()
        try:
            self._pidfd = os.pidfd_open(self.pid)
        except (AttributeError, OSError):
            self._pidfd = None
        if self._pidfd is not None:
            loop.add_reader(self._pidfd, self._on_pidfd)
        self._on_pidfd()  # already exited?

    def _on_pidfd(self) -> None:
        rc = self._popen.poll()
        if rc is None:
            return
        self._release_pidfd()
        if self._exit is not None and not self._exit.done():
            self._exit.set_result(rc)

    def _release_pidfd(self) -> None:
        if self._pidfd is None:
            return
        fd, self._pidfd = self._pidfd, None
        if self._loop is not None and not self._loop.is_closed():
            with contextlib.suppress(Exception):
                self._loop.remove_reader(fd)
        with contextlib.suppress(OSError):
            os.close(fd)

    async def wait(self) -> int:
        rc = self._popen.poll()
        if rc is not None:
            self._on_pidfd()
            return rc
        if self._exit is None or self._pidfd is None:
            # No pidfd support: bounded polling, still cancel-safe.
            while (rc := self._popen.poll()) is None:
                await asyncio.sleep(0.05)
            return rc
        # shield: one waiter's cancellation must not cancel the shared future.
        return await asyncio.shield(self._exit)

    def send_signal(self, sig: int) -> None:
        if self._popen.poll() is None:
            self._popen.send_signal(sig)

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)


def _kill_group(popen: subprocess.Popen[bytes]) -> None:
    if popen.poll() is not None:
        return
    try:
        os.killpg(popen.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            popen.kill()


async def _attach_reader(
    loop: asyncio.AbstractEventLoop, pipe: Any
) -> asyncio.StreamReader:
    reader = asyncio.StreamReader(limit=2**16, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    await loop.connect_read_pipe(lambda: protocol, pipe)
    return reader


async def spawn_process(
    *argv: str,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdin: int | None = None,
    stdout: int | None = _PIPE,
    stderr: int | None = _PIPE,
    start_new_session: bool = True,
) -> Any:
    """Spawn ``argv`` cancel-safely; see module docstring."""

    def _map(value: int | None) -> int | None:
        if value == _PIPE:
            return subprocess.PIPE
        if value == _DEVNULL:
            return subprocess.DEVNULL
        return value

    patched = asyncio.create_subprocess_exec
    if patched is not asyncio.subprocess.create_subprocess_exec:
        # Respect an installed asyncio.create_subprocess_exec seam (test
        # hermeticity guards such as "never run a real systemctl").
        return await patched(
            *argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=start_new_session,
        )
    loop = asyncio.get_running_loop()
    popen = subprocess.Popen(  # argv is policy-checked by callers
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=_map(stdin),
        stdout=_map(stdout),
        stderr=_map(stderr),
        start_new_session=start_new_session,
        close_fds=True,
    )
    proc = SpawnedProcess(popen)
    try:
        if popen.stdout is not None:
            proc.stdout = await _attach_reader(loop, popen.stdout)
        if popen.stderr is not None:
            proc.stderr = await _attach_reader(loop, popen.stderr)
        proc._watch_exit(loop)
    except BaseException:
        _kill_group(popen)
        for pipe in (popen.stdout, popen.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        raise
    return proc


__all__ = ["SpawnedProcess", "spawn_process"]

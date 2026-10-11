"""veya/obase/acp_stdio — canonical stdio JSON-RPC transport (external adapter).

obase's job is "how we talk to the outside world" (§3 / §7.2).  Spawning a child
process and speaking newline-delimited JSON-RPC 2.0 over its stdin/stdout is
exactly that, and exactly one implementation of it is allowed (§1.4).

This module was **promoted** from :mod:`server.acp_client`, which now delegates
here — the protocol semantics (``session/new``, ``task/start``, …) stay in that
module, the byte-level transport moves down to the layer that owns external
adapters.  ``tests/test_nano_w9_buzz.py`` carries the single-source guard test.

Scope boundaries, deliberately:

* this is a **transport**, not an agent framework — it knows nothing about ACP
  method names, sessions, approvals, or project state;
* it is **not** an authority: it cannot schedule, approve, or decide anything; a
  caller can only use it to execute a task it was already given;
* it never re-implements hashing/JSON canonicalisation.

Only stdlib is used, per the obase dependency rule (no 3O layers, no project
business roots).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

__all__ = ["StdioJsonRpcError", "StdioJsonRpcTransport"]


class StdioJsonRpcError(RuntimeError):
    """Transport failure: spawn error, protocol error, timeout, or early exit."""


class StdioJsonRpcTransport:
    """Newline-delimited JSON-RPC 2.0 over a child process's stdin/stdout.

    The caller supplies a message and gets the matching ``result`` back; server
    notifications (messages carrying a ``method`` but no matching request id)
    are handed to the injected ``on_notification`` callback instead of being
    silently dropped.

    Args:
        command: argv of the child process.
        cwd: working directory for the child.
        env: complete environment for the child (``None`` inherits).
        timeout_s: default per-request timeout.
        on_notification: called with every server notification payload.
        on_stderr: called with each stderr line (default: ignored).
        name: label used in error messages.

    Example:
        >>> # transport = StdioJsonRpcTransport(["my-acp-agent", "--stdio"])
        >>> # result = await transport.request("initialize", {"protocol": "acp/0.1"})
    """

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float = 30.0,
        on_notification: Callable[[dict[str, Any]], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        name: str = "stdio-jsonrpc",
    ) -> None:
        self.command = list(command or [])
        self.cwd = cwd
        self.env = env
        self.timeout_s = float(timeout_s)
        self.on_notification = on_notification
        self.on_stderr = on_stderr
        self.name = name
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 0

    # ------------------------------------------------------------------ state

    @property
    def running(self) -> bool:
        """True once the child process has been spawned."""
        return self._proc is not None and self._proc.returncode is None

    def allocate_id(self) -> int:
        """Return the next monotonically increasing request id."""
        self._next_id += 1
        return self._next_id

    # ----------------------------------------------------------------- process

    async def open(self) -> None:
        """Spawn the child process and start the reader loops (idempotent)."""
        if self._proc is not None:
            return
        if not self.command:
            raise StdioJsonRpcError(f"{self.name}: empty command")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.cwd,
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise StdioJsonRpcError(f"{self.name}: process spawn failed: {exc}") from exc
        self._reader_task = asyncio.create_task(self._read_loop())
        if self.on_stderr is not None and self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def close(self, *, timeout_s: float = 5.0) -> None:
        """Terminate the reader tasks and reap the child process."""
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._stderr_task = None
        proc = self._proc
        self._proc = None
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        self._fail_pending(StdioJsonRpcError(f"{self.name}: transport closed"))

    # ------------------------------------------------------------------- loops

    async def _read_loop(self) -> None:
        """Read NDJSON lines; route replies to futures, notices to the callback."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            try:
                line = await proc.stdout.readline()
            except (asyncio.CancelledError, ValueError):
                return
            if not line:
                break
            try:
                message = json.loads(line.decode(errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            method = message.get("method")
            if request_id is not None and method is None:
                future = self._pending.pop(request_id, None)
                if future is not None and not future.done():
                    error = message.get("error")
                    if error:
                        future.set_exception(StdioJsonRpcError(f"{self.name}: {error}"))
                    else:
                        result = message.get("result")
                        future.set_result(result if isinstance(result, dict) else {})
                continue
            if self.on_notification is not None:
                with contextlib.suppress(Exception):
                    self.on_notification(message.get("params") or message)
        self._fail_pending(StdioJsonRpcError(f"{self.name}: process exited"))

    async def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None or self.on_stderr is None:
            return
        while True:
            try:
                line = await proc.stderr.readline()
            except (asyncio.CancelledError, ValueError):
                return
            if not line:
                return
            with contextlib.suppress(Exception):
                self.on_stderr(line.decode(errors="replace").rstrip())

    def _fail_pending(self, exc: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # ---------------------------------------------------------------- requests

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a fire-and-forget notification (no id, no reply expected)."""
        await self.open()
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Send one JSON-RPC request and await its ``result``.

        Args:
            method: the JSON-RPC method name.
            params: the request params object.
            timeout_s: overrides the transport default.

        Returns:
            The server's ``result`` object (``{}`` when it omitted one).

        Raises:
            StdioJsonRpcError: on protocol error, timeout, or early process exit.
        """
        await self.open()
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise StdioJsonRpcError(f"{self.name}: transport not open")
        request_id = self.allocate_id()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(
                future, timeout=self.timeout_s if timeout_s is None else float(timeout_s)
            )
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise StdioJsonRpcError(f"{self.name}: request timeout: {method}") from None

    async def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise StdioJsonRpcError(f"{self.name}: transport not open")
        try:
            proc.stdin.write((json.dumps(payload) + "\n").encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise StdioJsonRpcError(f"{self.name}: pipe closed: {exc}") from exc
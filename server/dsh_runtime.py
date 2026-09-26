"""Persistent DSH runtime surface for the canonical DSH executor.

This is a thin client for DSH's shipped Web/API session contract.  It does not
route models, own credentials, or create another executor: it keeps one DSH
Web host alive, addresses one DSH Session, and exposes the real follow stream
to Veya callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from server import dsh_plane
from server.process_guard import executor_spawn_kwargs

_URL_RE = re.compile(r"dsh web: (https?://\S+)")


@dataclass
class DSHStream:
    """One real ``session/follow`` stream and its bounded observations."""

    events: list[dict[str, Any]] = field(default_factory=list)
    assistant_events: int = 0
    terminal: bool = False
    _queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)

    async def push(self, value: dict[str, Any]) -> None:
        self.events.append(value)
        if len(self.events) > 200:
            del self.events[:-200]
        if value.get("type") == "assistant-stream":
            self.assistant_events += 1
        if value.get("type") == "event":
            event_type = (value.get("event") or {}).get("type")
            if event_type in {"turn/end", "session/settled"}:
                self.terminal = True
        await self._queue.put(value)

    async def close(self) -> None:
        await self._queue.put(None)

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            value = await self._queue.get()
            if value is None:
                return
            yield value


class DSHRuntimeError(RuntimeError):
    pass


class DSHTransientError(DSHRuntimeError):
    """A bounded, local runtime-boundary failure (never an upstream failure)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"transient runtime boundary HTTP {status}")
        self.status = status


class DSHRuntime:
    """Long-lived DSH Web host + persistent DSH session."""

    def __init__(self, *, cfg: dict[str, str] | None = None, port: int = 0) -> None:
        self.cfg = dsh_plane.load_config() if cfg is None else cfg
        self.port = port
        self.process: asyncio.subprocess.Process | None = None
        self.base_url = ""
        self.cookie = ""
        self.session_id: str | None = None
        self.provider: str | None = None
        self.model: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self.stream = DSHStream()
        self.retry_occurred = False
        self.retry_bound_respected = True

    async def start(self, *, cwd: str | None = None) -> DSHRuntime:
        if self.process is not None:
            return self
        bin_path = dsh_plane._resolve_dsh_bin() if hasattr(dsh_plane, "_resolve_dsh_bin") else None
        if not bin_path:
            import shutil

            bin_path = shutil.which("dsh")
        if not bin_path:
            raise DSHRuntimeError("dsh binary not found")
        env = dsh_plane.subprocess_env(self.cfg)
        argv = [
            bin_path,
            "--profile",
            "web",
            "--no-open",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **executor_spawn_kwargs(),
        )
        assert self.process.stdout is not None and self.process.stderr is not None

        async def read_announcement() -> str:
            async def read(stream: asyncio.StreamReader) -> str:
                while True:
                    line = await stream.readline()
                    if not line:
                        return ""
                    text = line.decode("utf-8", "replace")
                    if _URL_RE.search(text):
                        return text

            tasks = [
                asyncio.create_task(read(self.process.stdout)),
                asyncio.create_task(read(self.process.stderr)),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            return next(iter(done)).result()

        line = await asyncio.wait_for(read_announcement(), timeout=30)
        match = _URL_RE.search(line)
        if not match:
            stderr = ""
            if self.process.stderr is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    stderr = (await asyncio.wait_for(self.process.stderr.read(4000), 1)).decode()
            raise DSHRuntimeError(f"dsh web did not announce a URL: {stderr[:500]}")
        from urllib.parse import parse_qs, urlsplit

        parsed = urlsplit(match.group(1))
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        token = parse_qs(parsed.query).get("token", [""])[0]
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=60.0)
        response = await self._client.get("/", params={"token": token}, follow_redirects=True)
        response.raise_for_status()
        self.cookie = "; ".join(f"{k}={v}" for k, v in self._client.cookies.items())
        created = await self.call("session/create", {"request": {"cwd": cwd} if cwd else {}})
        self.session_id = created["sessionId"]
        return self

    async def call(self, endpoint: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise DSHRuntimeError("runtime is not started")
        rpc_id = str(uuid.uuid4())
        response = await self._client.post(
            f"/api/{endpoint}",
            json={
                "type": "client-request",
                "rpcId": rpc_id,
                "method": endpoint,
                "payload": {"args": args},
            },
        )
        response.raise_for_status()
        envelope = response.json()
        result = envelope.get("result") or {}
        if not result.get("ok"):
            error = result.get("error") or {}
            raise DSHRuntimeError(
                f"{error.get('code', 'gateway/error')}: {error.get('message', 'request failed')}"
            )
        return result.get("value") or {}

    async def select_model(
        self, provider: str, model: str, reasoning_effort: str | None = None
    ) -> dict[str, Any]:
        if self.session_id is None:
            raise DSHRuntimeError("session is not created")
        request: dict[str, Any] = {
            "sessionId": self.session_id,
            "provider": provider,
            "model": model,
        }
        if reasoning_effort is not None:
            request["reasoningEffort"] = reasoning_effort
        selected = await self.call("session/selectModel", {"request": request})
        self.provider, self.model = provider, model
        return selected

    async def _follow(self) -> None:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.base_url)
        uri = f"{'wss' if parsed.scheme == 'https' else 'ws'}://{parsed.netloc}/api/remote.mux"
        try:
            async with websockets.connect(
                uri, additional_headers={"Cookie": self.cookie, "Origin": self.base_url}
            ) as ws:
                stream_id = str(uuid.uuid4())
                request = {
                    "address": {"kind": "session", "sessionId": self.session_id},
                    "assistantStream": True,
                }
                await ws.send(
                    json.dumps(
                        {
                            "type": "open",
                            "streamId": stream_id,
                            "endpoint": "session/follow",
                            "payload": {"args": {"request": request}},
                        }
                    )
                )
                async for raw in ws:
                    frame = json.loads(raw)
                    if frame.get("streamId") != stream_id:
                        continue
                    if frame.get("type") == "item":
                        await self.stream.push(frame.get("value") or {})
                    elif frame.get("type") in {"end", "error"}:
                        if frame.get("type") == "error":
                            await self.stream.push(
                                {"type": "runtime-error", "error": frame.get("error")}
                            )
                        return
        except Exception as exc:
            await self.stream.push({"type": "runtime-error", "error": {"message": str(exc)[:500]}})
        finally:
            await self.stream.close()

    async def ensure_follow(self) -> DSHStream:
        if self._stream_task is None or self._stream_task.done():
            self._stream_task = asyncio.create_task(self._follow())
            await asyncio.sleep(0.05)
        return self.stream

    async def prompt(
        self,
        content: list[dict[str, Any]],
        *,
        mode: str = "queue",
        transient_injector: Any | None = None,
        retry_bound: int = 1,
    ) -> dict[str, Any]:
        if self.session_id is None:
            raise DSHRuntimeError("session is not created")
        # A Session is persistent; each prompt gets a fresh follow cursor.
        self.stream = DSHStream()
        await self.ensure_follow()
        request = {
            "requestId": str(uuid.uuid4()),
            "sessionId": self.session_id,
            "mode": mode,
            "content": content,
        }
        attempts = 0
        while True:
            try:
                if transient_injector is not None:
                    status = transient_injector(attempts)
                    if status:
                        raise DSHTransientError(int(status))
                return await self.call("session/prompt", {"request": request})
            except DSHTransientError as exc:
                if exc.status not in {502, 503, 504} or attempts >= retry_bound:
                    self.retry_bound_respected = attempts <= retry_bound
                    raise
                attempts += 1
                self.retry_occurred = True

    async def close(self) -> None:
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
        if self._client is not None:
            await self._client.aclose()
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.process.wait(), timeout=3)
        self.process = None


__all__ = ["DSHRuntime", "DSHRuntimeError", "DSHStream"]

"""Minimal ACP-over-stdio JSON-RPC client.

Design adapted from block/buzz (Apache-2.0, Copyright 2026 Block, Inc.):
session lifecycle, permission requests, steer/interrupt and usage accounting,
expressed on Veya's existing ApprovalStore / action_risk / process supervisor.
No Buzz Rust code is copied.

Wire format is newline-delimited JSON-RPC 2.0 on stdin/stdout. Diagnostics on
stderr never contaminate stdout. The client is opt-in: ``VEYA_ACP=1``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from veya.obase.approvals import DECISION_DENY, ApprovalStore

logger = logging.getLogger("veya.acp")

_ALIASES = {
    "goose": "goose",
    "codex": "codex",
    "claude": "claude-code",
    "claude_code": "claude-code",
    "claude-code": "claude-code",
}


def normalize_adapter(name: str) -> str:
    """Canonical adapter id; unknown names pass through unchanged."""

    return _ALIASES.get(str(name or "").strip().lower(), str(name or "").strip().lower())


def acp_enabled() -> bool:
    """``VEYA_ACP`` gates the adapter; default OFF."""

    return os.environ.get("VEYA_ACP", "0").strip().lower() in {"1", "true", "yes", "on"}


class ACPError(RuntimeError):
    """ACP protocol or transport failure."""


@dataclass
class ACPResult:
    ok: bool
    session_id: str = ""
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    pending_permission: str | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "text": self.text,
            "usage": dict(self.usage),
            "pending_permission": self.pending_permission,
            "error": self.error,
            "engine": "acp",
        }


class ACPClient:
    """One stdio child, one ACP session, explicit permissions."""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        approvals: ApprovalStore | None = None,
        request_timeout_s: float = 60.0,
    ) -> None:
        if not argv:
            raise ACPError("ACP argv is empty")
        self.argv = list(argv)
        self.cwd = cwd
        self.env = env
        self.approvals = approvals or ApprovalStore()
        self.request_timeout_s = float(request_timeout_s)
        self.proc: asyncio.subprocess.Process | None = None
        self.session_id = ""
        self._next_id = 1
        self._lock = asyncio.Lock()
        self.usage: dict[str, Any] = {}

    async def start(self) -> None:
        if self.proc is not None:
            return
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=self.cwd,
                env=dict(self.env) if self.env is not None else None,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise ACPError(f"ACP agent failed to start: {exc}") from exc
        payload = await self._request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "veya", "version": "0.6.0"},
                "capabilities": {},
            },
        )
        if not isinstance(payload, dict):
            raise ACPError("ACP initialize returned no object")

    async def new_session(self, *, cwd: str | None = None) -> str:
        await self.start()
        result = await self._request("session/new", {"cwd": cwd or self.cwd or ""})
        sid = str((result or {}).get("sessionId") or "") if isinstance(result, dict) else ""
        self.session_id = sid or uuid.uuid4().hex
        return self.session_id

    async def prompt(self, text: str, *, session_id: str | None = None) -> ACPResult:
        """Send one prompt; normalize adapter text/usage into one result."""

        await self.start()
        sid = session_id or self.session_id or await self.new_session()
        result = await self._request(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": str(text)}]},
        )
        if not isinstance(result, dict):
            return ACPResult(False, session_id=sid, error="invalid prompt result")
        self._note_usage(result)
        text_out = _extract_text(result)
        return ACPResult(True, session_id=sid, text=text_out, usage=dict(self.usage))

    async def cancel(self, *, session_id: str | None = None) -> bool:
        """Steer/interrupt the active prompt without killing the session."""

        if self.proc is None:
            return False
        try:
            await self._request(
                "session/cancel", {"sessionId": session_id or self.session_id}, notify=True
            )
            return True
        except ACPError:
            return False

    async def request_permission(
        self,
        tool: str,
        args: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        auto_decision: str | None = None,
    ) -> ACPResult:
        """Route `session/request_permission` through Veya approval + risk."""

        from server.action_risk import RISK_DESTRUCTIVE, RISK_OUTBOUND, classify_action

        await self.start()
        sid = session_id or self.session_id or await self.new_session()
        args = dict(args or {})
        tier = classify_action(tool, args)
        payload = {
            "sessionId": sid,
            "tool": tool,
            "args": args,
            "risk": tier,
            "options": ["allow_once", "deny"],
        }
        upstream = await self._request("session/request_permission", payload)
        request_id = uuid.uuid4().hex
        self.approvals.create_approval(
            {
                "request_id": request_id,
                "scope": f"acp:{sid}",
                "tool": tool,
                "tool_args": {"acp": payload, "upstream": upstream},
                "actor": "acp",
            }
        )
        if auto_decision == DECISION_DENY or (
            auto_decision is None and tier in {RISK_OUTBOUND, RISK_DESTRUCTIVE}
        ):
            return ACPResult(
                False,
                session_id=sid,
                pending_permission=request_id,
                error="approval required for outbound/destructive action",
            )
        return ACPResult(True, session_id=sid, pending_permission=request_id)

    async def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        self.proc = None
        if proc.returncode is not None:
            return
        from server.exec_process import terminate_process

        await terminate_process(proc)

    def _note_usage(self, result: Mapping[str, Any]) -> None:
        raw = result.get("usage")
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                try:
                    self.usage[str(key)] = self.usage.get(str(key), 0) + int(value)
                except (TypeError, ValueError):
                    continue

    async def _request(self, method: str, params: dict[str, Any], *, notify: bool = False) -> Any:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise ACPError("ACP agent is not running")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            message["id"] = self._next_id
            self._next_id += 1
        async with self._lock:
            proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            await proc.stdin.drain()
            if notify:
                return None
            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=self.request_timeout_s
                )
                if not line:
                    raise ACPError("ACP agent closed stdout")
                try:
                    frame = json.loads(line)
                except ValueError:
                    logger.warning("ACP agent emitted non-JSON stdout line; ignored")
                    continue
                if frame.get("id") != message["id"]:
                    continue  # session notifications / update events
                if "error" in frame:
                    raise ACPError(str((frame["error"] or {}).get("message") or frame["error"]))
                return (frame.get("result") or {}) if isinstance(frame.get("result"), dict) else frame.get("result")


def _extract_text(result: Mapping[str, Any]) -> str:
    """Normalize text fields across goose/codex/claude ACP adapters."""

    chunks: list[str] = []
    for candidate in (result.get("text"), result.get("content"), result.get("message")):
        if isinstance(candidate, str):
            chunks.append(candidate)
        elif isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, Mapping) and item.get("type") == "text":
                    chunks.append(str(item.get("text") or ""))
    return "\n".join(part for part in chunks if part)

"""
server/events.py — Canonical Event Model §4 + on_step ContextVar

Holds the per-request on_step callback via asyncio ContextVar.
Using ContextVar avoids threading the callback through every function signature
while remaining coroutine-safe (each async task has its own context copy).

Usage:
    # Set in coordinator.handle():
    token = _on_step_ctx.set(callback)
    try: ...
    finally: _on_step_ctx.reset(token)

    # Fire from anywhere in the call chain:
    from server.events import fire_step
    fire_step({"type": "tool_call", "tool_name": "write"})

Canonical Event Model §4 (P3-04 Telemetry v1):
    EventEnvelope: event_id, trace_id, session_id, task_id, turn_id,
                   topic, ts, actor, payload, schema_version
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Canonical Event Model §4 — Schema 版本 (关键结构变更时递增)
_SCHEMA_VERSION = 1

# §4 定义的最小事件类型白名单
_MINIMAL_EVENTS = frozenset({
    "session.created",
    "turn.started",
    "message.user_added",
    "message.assistant_added",
    "tool.requested",
    "tool.approval_required",
    "tool.approved",
    "tool.denied",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "tool.cancelled",
    "task.created",
    "task.started",
    "task.waiting_approval",
    "task.completed",
    "task.failed",
    "task.cancelled",
    "checkpoint.created",
    "resume.started",
    "resume.completed",
    "resume.failed",
    "memory.candidate_created",
    "memory.committed",
    "memory.corrected",
    "memory.superseded",
    "memory.forgotten",
    "memory.explicit_write",
    "memory.distilled_candidate_source",
    "memory.conflict_detected",
    "memory.source_missing",
    "skill.candidate_created",
    "skill.created",
    "skill.updated",
    "skill.version_created",
    "skill.executed",
    "skill.completed",
    "skill.partial",
    "skill.teaching_instruction",
    "skill.deprecated",
    "skill.rolled_back",
    "skill.failed",
    "continuity.snapshot_created",
    "continuity.resumed",
    "learning.candidate_created",
    "learning.validated",
    "learning.rejected",
    "learning.applied",
    "learning.rolled_back",
    "delegate.started",
    "delegate.completed",
    "delegate.queued",
    "delegate.partial",
    "delegate.failed",
    "delegate.cancelled",
    "scheduler.slot_acquired",
    "scheduler.slot_released",
    "scheduler.task_ready",
    "scheduler.task_started",
    "fanin.started",
    "fanin.completed",
    "finalization.started",
    "finalization.child_cancelled",
    "finalization.artifact_collected",
    "finalization.acceptance_run",
    "finalization.completed",
    "artifact.created",
    "artifact.verified",
    "artifact.partial",
    "goal.started",
    "goal.updated",
    "goal.completed",
    "trajectory.recorded",
    "eval.recorded",
    "goal_run.created",
    "goal_run.status_changed",
    "work_item.created",
    "work_item.ready",
    "work_item.claimed",
    "work_item.started",
    "work_item.heartbeat",
    "work_item.checkpointed",
    "work_item.succeeded",
    "work_item.failed",
    "work_item.cancelled",
    "work_item.completion_deduplicated",
    "work_item.unknown",
    "work_item.fenced_out",
    "lease.expired",
    "recovery.started",
    "recovery.decision",
    "recovery.completed",
    "reconciliation.completed",
    "side_effect.declared",
    "side_effect.started",
    "side_effect.committed",
    "side_effect.unknown",
    "side_effect.compensated",
    "fanin.snapshot_created",
    "finalization.resumed",
    "finalization.partial_completed",
    "outbox.published",
    "migration.comparison",
})


@dataclass(frozen=True)
class EventEnvelope:
    """Canonical Event Model §4, persisted as a JSON-compatible envelope."""

    event_id: str
    trace_id: str
    session_id: str
    task_id: str | None
    turn_id: str | None
    topic: str
    ts: float
    actor: str
    payload: dict[str, Any]
    schema_version: int = _SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_DEFAULT_EVENT_STORE_PATH = str(Path.home() / ".veya" / "events.jsonl")


class EventStore:
    """Append-only JSONL Event Store used by projections and replay queries."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(
            path or os.environ.get("VEYA_EVENT_STORE_PATH", _DEFAULT_EVENT_STORE_PATH)
        ).expanduser()
        self._lock = threading.RLock()
        self._known_event_ids: set[str] | None = None

    def append(self, event: EventEnvelope | dict[str, Any]) -> dict[str, Any]:
        envelope = event.to_dict() if isinstance(event, EventEnvelope) else _to_envelope(event)
        with self._lock:
            event_id = str(envelope.get("event_id") or "")
            if self._known_event_ids is None:
                self._known_event_ids = set()
                if self.path.exists():
                    for line in self.path.read_text(encoding="utf-8").splitlines():
                        with contextlib.suppress(json.JSONDecodeError):
                            stored = json.loads(line)
                            if stored.get("event_id"):
                                self._known_event_ids.add(str(stored["event_id"]))
            if event_id and event_id in self._known_event_ids:
                return envelope
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            if event_id:
                self._known_event_ids.add(event_id)
        return envelope

    def read_all(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        topics: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        result: list[dict[str, Any]] = []
        with self._lock:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if task_id is not None and event.get("task_id") != task_id:
                    continue
                if session_id is not None and event.get("session_id") != session_id:
                    continue
                if topics is not None and event.get("topic") not in topics:
                    continue
                result.append(event)
        return result


event_store = EventStore()

_on_step_ctx: contextvars.ContextVar[Callable | None] = contextvars.ContextVar(
    "on_step", default=None
)
_task_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "task_id", default=None
)
_event_session_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "event_session_id", default=None
)
_event_trace_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "event_trace_id", default=None
)
_event_turn_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "event_turn_id", default=None
)


def current_task_id() -> str | None:
    return _task_id_ctx.get()


def bind_event_context(
    *, session_id: str | None = None, trace_id: str | None = None, turn_id: str | None = None
) -> tuple[contextvars.Token, contextvars.Token, contextvars.Token]:
    """Bind canonical identifiers for the current request context."""
    return (
        _event_session_ctx.set(session_id),
        _event_trace_ctx.set(trace_id),
        _event_turn_ctx.set(turn_id),
    )


def reset_event_context(
    tokens: tuple[contextvars.Token, contextvars.Token, contextvars.Token]
) -> None:
    """Restore the previous request identifiers in reverse binding order."""
    session_token, trace_token, turn_token = tokens
    _event_turn_ctx.reset(turn_token)
    _event_trace_ctx.reset(trace_token)
    _event_session_ctx.reset(session_token)


def current_event_context() -> dict[str, str | None]:
    return {
        "session_id": _event_session_ctx.get(),
        "trace_id": _event_trace_ctx.get(),
        "turn_id": _event_turn_ctx.get(),
        "task_id": current_task_id(),
    }


def append_canonical_event(
    topic: str,
    payload: dict[str, Any] | None = None,
    *,
    actor: str = "system",
    session_id: str | None = None,
    trace_id: str | None = None,
    turn_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Persist a fact using the active request identifiers.

    This is intentionally a passive bridge: it records events and never
    chooses an executor or mutates a task projection directly.
    """
    context = current_event_context()
    resolved_task_id = task_id or context["task_id"]
    resolved_session_id = session_id or context["session_id"]
    resolved_trace_id = trace_id or context["trace_id"]
    if resolved_task_id and not resolved_session_id:
        with contextlib.suppress(Exception):
            from server.task_store import task_store

            task = task_store.get(resolved_task_id)
            if task is not None:
                resolved_session_id = task.session_id
                resolved_trace_id = resolved_trace_id or task.trace_id
    resolved_session_id = str(resolved_session_id or "unknown")
    resolved_trace_id = str(resolved_trace_id or resolved_session_id)
    return event_store.append(
        {
            "topic": str(topic),
            "session_id": resolved_session_id,
            "trace_id": resolved_trace_id,
            "task_id": resolved_task_id,
            "turn_id": turn_id or context["turn_id"],
            "actor": actor,
            "payload": dict(payload or {}),
        }
    )


def fire_step(event: dict[str, Any]) -> None:
    """Fire an on_step event to the current context's callback (if any)."""
    cb = _on_step_ctx.get()
    if cb is not None:
        with contextlib.suppress(Exception):
            cb(event)  # on_step errors must never abort the main flow


def _to_envelope(event: dict[str, Any]) -> dict[str, Any]:
    """裸 dict → Canonical Event Model §4 EventEnvelope。

    EventEnvelope 结构 (§4):
      event_id    : str         — UUID, 唯一标识单条事件记录
      trace_id    : str         — 贯穿一次故障处理链路
      session_id  : str         — 会话 ID
      task_id     : str | None  — 任务 ID (projection-only, A-04)
      turn_id     : str | None  — 当前 turn ID
      topic       : str         — 事件类型 (minimal events from §4)
      ts          : float       — 事件发生时间戳
      actor       : str         — 事件主体 (模型/用户/系统等)
      payload     : dict        — 事件负载
      schema_version : int      — Schema 版本

    只新增信封字段, 原字段一个不删不改。若原 dict 已占用某个信封键名,
    保留原字段, 跳过那一个信封键。任何异常退化为原样返回,
    绝不能因为信封逻辑本身拖垮 SSE 流。
    """
    try:
        if not isinstance(event, dict):
            return event
        session_id = str(event.get("session_id") or event.get("sid") or "")
        task_id = event.get("task_id")
        turn_id = event.get("turn_id")
        topic = str(event.get("topic") or event.get("type") or event.get("event") or "unknown")
        trace_id = str(
            event.get("trace_id")
            or session_id
            or task_id
            or event.get("plan_id")
            or event.get("request_id")
            or ""
        )
        payload = {
            k: v
            for k, v in event.items()
            if k
            not in {
                "event_id",
                "trace_id",
                "sid",
                "task_id",
                "turn_id",
                "topic",
                "ts",
                "actor",
                "schema_version",
                "payload",
                "type",
                "event",
            }
        }
        envelope = dict(event)
        # 信封核心字段, 均用 setdefault 保护原字段不被覆盖
        envelope.setdefault("event_id", str(uuid.uuid4()))
        envelope.setdefault(
            "trace_id",
            trace_id,
        )
        envelope.setdefault("session_id", session_id)
        envelope.setdefault("task_id", task_id)
        envelope.setdefault("turn_id", turn_id)
        envelope.setdefault("topic", topic)
        envelope.setdefault("ts", time.time())
        envelope.setdefault("actor", event.get("actor", "system"))
        envelope.setdefault("schema_version", _SCHEMA_VERSION)
        # payload: 除 type/event 外的所有原始字段
        if "payload" not in envelope:
            envelope["payload"] = payload
        return envelope
    except Exception:
        return event

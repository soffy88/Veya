"""server/telemetry.py — P3-04 Telemetry v1: Trace Correlation + Export.

Canonical Event Model §4 + AuditEmitter → 统一遥测写出口。

核心能力:
1. trace_id 贯穿全链路 (chat_stream → tool → SSE → 审计)
2. 事件自动写入 JSONL sink (复用 oprim._audit_emit.JsonlSink)
3. 导出接口: replay(trace_id), list_traces(), export_jsonl(), export_otlp()
4. 与现有 VeyaAudit 读侧 API 兼容 (server/audit.py)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 惰性导入 3O oprim (同 server/audit.py 模式)
_oprim = None


def _load_oprim():
    global _oprim
    if _oprim is None:
        from veya.platform import oprim as _load_oprim

        _oprim = _load_oprim()
    return _oprim


# ---------------------------------------------------------------------------
# 内部审计事件结构 (复用 oprim._audit_emit.AuditEvent 的 Schema)
# ---------------------------------------------------------------------------

_AUDIT_EVENT_TYPES = ("diagnose", "plan", "decide", "execute", "learn")


@dataclass
class TelemetryEvent:
    """遥测事件 (统一 Schema, 兼容 oprim._audit_emit.AuditEvent)。"""

    event_type: str
    trace_id: str
    audit_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    inputs: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    learning: dict[str, Any] | None = None
    context: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    task_id: str | None = None
    turn_id: str | None = None
    topic: str | None = None

    def __post_init__(self) -> None:
        if self.event_type not in _AUDIT_EVENT_TYPES:
            raise ValueError(f"event_type must be one of {_AUDIT_EVENT_TYPES}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "trace_id": self.trace_id,
            "ts": self.ts,
            "event_type": self.event_type,
            "inputs": self.inputs,
            "decision": self.decision,
            "execution": self.execution,
            "learning": self.learning,
            "context": self.context,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "topic": self.topic,
        }


# ---------------------------------------------------------------------------
# Sink (写入目的地)
# ---------------------------------------------------------------------------


class TelemetrySink:
    """遥测 sink: 统一 JSONL 文件落盘 (复用 oprim.JsonlSink 语义)。"""

    def __init__(self, path: str | Path, *, append: bool = True):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not append:
            self.path.write_text("")

    def write(self, event: TelemetryEvent) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def read_trace(self, trace_id: str) -> list[dict[str, Any]]:
        out = []
        try:
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("trace_id") == trace_id:
                        out.append(data)
        except FileNotFoundError:
            return []
        return out

    def read_all(self, limit: int = 0) -> list[dict[str, Any]]:
        out = []
        try:
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    out.append(json.loads(line))
                    if limit and len(out) >= limit:
                        break
        except FileNotFoundError:
            return []
        return out


# ---------------------------------------------------------------------------
# Telemetry Emitter (统一写出口)
# ---------------------------------------------------------------------------


class TelemetryEmitter:
    """P3-04 Telemetry v1: Trace Correlation + Export 写出口。

    - 不做决策, 只按 Canonical Event Model §4 记录
    - trace_id 贯穿 chat_stream → tool → SSE → 审计全链路
    - 默认 sink: ~/.veya/telemetry/telemetry.jsonl
    - 可注入自定义 sink (测试/多目标)
    """

    DEFAULT_DIR = "~/.veya/telemetry"

    def __init__(
        self,
        sink: TelemetrySink | None = None,
        trace_id: str | None = None,
    ):
        self.sink = sink or TelemetrySink(Path(self.DEFAULT_DIR).expanduser() / "telemetry.jsonl")
        self.trace_id = trace_id or uuid.uuid4().hex
        # 兼容旧 oprim AuditEmitter (如已有写入)
        try:
            oprim = _load_oprim()
            self._oprim_emitter = getattr(oprim, "_audit_emit", None)
        except Exception:
            self._oprim_emitter = None

    def emit(
        self,
        event_type: str,
        *,
        inputs: dict[str, Any] | None = None,
        decision: dict[str, Any] | None = None,
        execution: dict[str, Any] | None = None,
        learning: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        turn_id: str | None = None,
        topic: str | None = None,
    ) -> str:
        """写一条遥测记录, 返回 audit_id。"""
        event = TelemetryEvent(
            event_type=event_type,
            trace_id=self.trace_id,
            inputs=inputs or {},
            decision=decision,
            execution=execution,
            learning=learning,
            context=context or {},
            session_id=session_id,
            task_id=task_id,
            turn_id=turn_id,
            topic=topic,
        )
        self.sink.write(event)
        # 同时写入 oprim AuditEmitter (若可用, 保持兼容)
        if self._oprim_emitter and hasattr(self._oprim_emitter, "AuditEmitter"):
            try:
                oem = self._oprim_emitter.AuditEmitter(trace_id=self.trace_id)
                oem.emit(
                    event_type=event_type,
                    inputs=inputs,
                    decision=decision,
                    execution=execution,
                    learning=learning,
                    context=context,
                )
            except Exception:
                pass
        return event.audit_id

    # ── 链路节点快捷方法 ────────────────────────────────────────────
    def diagnose(self, **kw: Any) -> str:
        return self.emit("diagnose", **kw)

    def plan(self, **kw: Any) -> str:
        return self.emit("plan", **kw)

    def decide(self, **kw: Any) -> str:
        return self.emit("decide", **kw)

    def execute(self, **kw: Any) -> str:
        return self.emit("execute", **kw)

    def learn(self, **kw: Any) -> str:
        return self.emit("learn", **kw)

    # ── P3-04: Export / Replay ──────────────────────────────────────
    def replay(self) -> list[dict[str, Any]]:
        """回放本 trace 的完整决策链路 (按写入顺序)。"""
        return self.sink.read_trace(self.trace_id)

    def export_jsonl(self, output_path: str | Path) -> int:
        """导出本 trace 为 JSONL 文件, 返回事件数。"""
        events = self.replay()
        Path(output_path).write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
            encoding="utf-8",
        )
        return len(events)

    def export_otlp(self, output_path: str | Path) -> int:
        """导出为简化 OTLP 兼容格式 (用于 Grafana/Jaeger 等)。"""
        events = self.replay()
        otlp = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": "veya"}}]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "veya.telemetry"},
                            "spans": [
                                {
                                    "traceId": e["trace_id"],
                                    "spanId": e["audit_id"],
                                    "name": e["event_type"],
                                    "startTimeUnixNano": int(e["ts"] * 1e9),
                                    "endTimeUnixNano": int(e["ts"] * 1e9) + 1000000,
                                    "attributes": [
                                        {"key": k, "value": {"stringValue": str(v)}}
                                        for k, v in e.get("context", {}).items()
                                    ],
                                }
                                for e in events
                            ],
                        }
                    ],
                }
            ]
        }
        Path(output_path).write_text(json.dumps(otlp, ensure_ascii=False), encoding="utf-8")
        return len(events)


# ---------------------------------------------------------------------------
# 全局单例 + 便捷函数 (类似 server/audit.py 模式)
# ---------------------------------------------------------------------------

_default_emitter: TelemetryEmitter | None = None


def get_emitter(trace_id: str | None = None) -> TelemetryEmitter:
    """获取或创建全局遥测 emitter (进程内单例, 按 trace_id 区分)。"""
    global _default_emitter
    if trace_id and _default_emitter and _default_emitter.trace_id != trace_id:
        return TelemetryEmitter(trace_id=trace_id)
    if _default_emitter is None:
        _default_emitter = TelemetryEmitter(trace_id=trace_id)
    return _default_emitter


def reset_emitter() -> None:
    """重置全局 emitter (测试/进程内隔离用)。"""
    global _default_emitter
    _default_emitter = None


def new_trace() -> TelemetryEmitter:
    """显式开启新 trace (如新任务开始)。"""
    global _default_emitter
    _default_emitter = TelemetryEmitter()
    return _default_emitter


# ---------------------------------------------------------------------------
# 高层: Telemetry API (供 server/audit.py、路由、前端使用)
# ---------------------------------------------------------------------------


class TelemetryAPI:
    """统一遥测读取 API (复用 VeyaAudit 读侧语义, 增加导出)。"""

    def __init__(self, telemetry_dir: str | Path = "~/.veya/telemetry"):
        self.telemetry_dir = Path(telemetry_dir).expanduser()
        self.telemetry_dir.mkdir(parents=True, exist_ok=True)
        self._sink = TelemetrySink(self.telemetry_dir / "telemetry.jsonl")

    def replay(self, trace_id: str) -> dict[str, Any]:
        """回放一条 trace 的完整决策链路。"""
        events = self._sink.read_trace(trace_id)
        if not events:
            # The canonical event store is the runtime source of truth for
            # tool/task/session facts. Keep the legacy audit sink compatible,
            # but make a trace containing only canonical events observable via
            # this API as well.
            try:
                from server.events import event_store

                events = [
                    {
                        "audit_id": event.get("event_id"),
                        "trace_id": event.get("trace_id"),
                        "ts": event.get("ts"),
                        "event_type": event.get("topic"),
                        "inputs": {},
                        "decision": None,
                        "execution": event.get("payload") or {},
                        "learning": None,
                        "context": {},
                        "session_id": event.get("session_id"),
                        "task_id": event.get("task_id"),
                        "turn_id": event.get("turn_id"),
                        "topic": event.get("topic"),
                    }
                    for event in event_store.read_all()
                    if event.get("trace_id") == trace_id
                ]
            except Exception:
                events = []
        return {
            "trace_id": trace_id,
            "event_count": len(events),
            "events": events,
        }

    def traces(self, limit: int = 50) -> dict[str, Any]:
        """列出最近 trace 清单 (trace_id + 事件数 + 时间范围)。"""
        all_events = self._sink.read_all(limit=0)
        by_trace: dict[str, list] = {}
        for e in all_events:
            by_trace.setdefault(e.get("trace_id", "?"), []).append(e)
        out = []
        for tid, evs in sorted(by_trace.items(), key=lambda kv: kv[1][-1]["ts"], reverse=True)[
            :limit
        ]:
            out.append(
                {
                    "trace_id": tid,
                    "event_count": len(evs),
                    "event_types": [e["event_type"] for e in evs],
                    "first_ts": evs[0]["ts"],
                    "last_ts": evs[-1]["ts"],
                }
            )
        return {"traces": out, "total_events": len(all_events)}

    def export_trace(
        self, trace_id: str, fmt: str = "jsonl", output: str | None = None
    ) -> dict[str, Any]:
        """导出单条 trace (jsonl / otlp)。"""
        events = self._sink.read_trace(trace_id)
        if not events:
            return {"status": "not_found", "trace_id": trace_id}
        out_path = Path(output) if output else self.telemetry_dir / f"trace_{trace_id}.{fmt}"
        if fmt == "jsonl":
            out_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
                encoding="utf-8",
            )
        elif fmt == "otlp":
            otlp = {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                {"key": "service.name", "value": {"stringValue": "veya"}}
                            ]
                        },
                        "scopeSpans": [
                            {
                                "scope": {"name": "veya.telemetry"},
                                "spans": [
                                    {
                                        "traceId": e["trace_id"],
                                        "spanId": e["audit_id"],
                                        "name": e["event_type"],
                                        "startTimeUnixNano": int(e["ts"] * 1e9),
                                        "endTimeUnixNano": int(e["ts"] * 1e9) + 1000000,
                                        "attributes": [
                                            {"key": k, "value": {"stringValue": str(v)}}
                                            for k, v in e.get("context", {}).items()
                                        ],
                                    }
                                    for e in events
                                ],
                            }
                        ],
                    }
                ]
            }
            out_path.write_text(json.dumps(otlp, ensure_ascii=False), encoding="utf-8")
        else:
            return {"status": "error", "error": f"unsupported format: {fmt}"}
        return {
            "status": "exported",
            "trace_id": trace_id,
            "events": len(events),
            "path": str(out_path),
        }

    def metrics(self) -> dict[str, Any]:
        """Return honest runtime counters from canonical events and telemetry sinks."""
        try:
            from server.events import event_store

            events = event_store.read_all()
        except Exception:
            events = []

        def rate(numerator: int, denominator: int) -> float | None:
            return round(numerator / denominator, 6) if denominator else None

        task_completed = sum(e.get("topic") == "task.completed" for e in events)
        task_failed = sum(e.get("topic") == "task.failed" for e in events)
        task_terminal = task_completed + task_failed
        tool_requested = sum(e.get("topic") == "tool.requested" for e in events)
        tool_failed = sum(e.get("topic") == "tool.failed" for e in events)
        resume_completed = sum(e.get("topic") == "resume.completed" for e in events)
        resume_failed = sum(e.get("topic") == "resume.failed" for e in events)
        resume_terminal = resume_completed + resume_failed
        empty_responses = sum(
            e.get("topic") == "message.assistant_added"
            and not str((e.get("payload") or {}).get("content") or "").strip()
            for e in events
        )
        assistant_messages = sum(e.get("topic") == "message.assistant_added" for e in events)
        return {
            "task_success_rate": rate(task_completed, task_terminal),
            "resume_success_rate": rate(resume_completed, resume_terminal),
            "tool_failure_rate": rate(tool_failed, tool_requested),
            "empty_response_rate": rate(empty_responses, assistant_messages),
            "approval_wait_count": sum(e.get("topic") == "tool.approval_required" for e in events),
            "event_count": len(events),
        }


# 模块级单例 (供路由/外部直接使用)
telemetry = TelemetryAPI()


__all__ = [
    "TelemetryAPI",
    "TelemetryEmitter",
    "TelemetryEvent",
    "TelemetrySink",
    "get_emitter",
    "new_trace",
    "reset_emitter",
    "telemetry",
]

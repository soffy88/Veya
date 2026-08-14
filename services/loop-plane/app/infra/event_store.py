"""loop-plane infra — 统一存储（SPEC §3）。

- events.jsonl        # DomainEvent（append-only 单一真相源）
- audit_trail.jsonl   # AuditEvent（五节点：diagnose/plan/decide/execute/learn）
- graphs/ skills/ exports/  # 图/技能/兼容投影
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# DomainEvent（SPEC §3.1）
# ---------------------------------------------------------------------------

AGGREGATES = ("Goal", "Todo", "Experiment", "Skill", "Run")

EVENT_TYPES = (
    "GoalCreated", "GoalCompleted",
    "TodoUpdated", "TodoClaimed", "TodoReleased",
    "EvidenceAppended",
    "GateRequired", "GateResolved",
    "QuotaSpent",
    "ExperimentStarted", "ExperimentCompleted",
    "SkillReleased", "SkillRolledBack",
    "ActionSucceeded", "ActionFailed",
)


class EventStore:
    """append-only DomainEvent 存储：events.jsonl，进程内聚合索引。"""

    def __init__(self, data_dir: Path, tenant_id: str = "default") -> None:
        self._dir = Path(data_dir) / tenant_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "events.jsonl"
        self._lock = threading.Lock()
        # 聚合索引: (aggregate_type, aggregate_id) -> [event, ...]
        self._index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._replay()

    def _replay(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (event.get("aggregate_type", ""), event.get("aggregate_id", ""))
            self._index.setdefault(key, []).append(event)

    def append(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        trace_id: str = "",
    ) -> str:
        """追加 DomainEvent（原子写 + 索引）。返回 event_id。"""
        if aggregate_type not in AGGREGATES:
            raise ValueError(f"非法 aggregate_type {aggregate_type!r} (允许: {AGGREGATES})")
        if event_type not in EVENT_TYPES:
            raise ValueError(f"非法 event_type {event_type!r}")
        event: dict[str, Any] = {
            "event_id": new_id("evt_"),
            "timestamp": _now_iso(),
            "trace_id": trace_id,
            "tenant_id": self._dir.name,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "event_type": event_type,
            "payload": payload,
        }
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
            self._index.setdefault((aggregate_type, aggregate_id), []).append(event)
        return event["event_id"]

    def stream(
        self,
        *,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """事件流（可选按聚合过滤，返回顺序 = 追加顺序）。"""
        if aggregate_type is None and aggregate_id is None:
            out: list[dict[str, Any]] = []
            for events in self._index.values():
                out.extend(events)
            return sorted(out, key=lambda e: (e["timestamp"], e["event_id"]))
        if aggregate_type is None or aggregate_id is None:
            raise ValueError("aggregate_type 与 aggregate_id 必须同时给出或同时省略")
        return list(self._index.get((aggregate_type, aggregate_id), []))

    def aggregates(self, aggregate_type: str) -> list[str]:
        return sorted(
            agg_id for (atype, agg_id) in self._index if atype == aggregate_type
        )


# ---------------------------------------------------------------------------
# AuditEvent（SPEC §3.3 — 五节点）
# ---------------------------------------------------------------------------

AUDIT_PHASES = ("diagnose", "plan", "decide", "execute", "learn")


class AuditLog:
    """audit_trail.jsonl：因果五节点审计，trace_id 关联。"""

    def __init__(self, data_dir: Path, tenant_id: str = "default") -> None:
        self._dir = Path(data_dir) / tenant_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "audit_trail.jsonl"
        self._lock = threading.Lock()

    def append(
        self,
        *,
        phase: str,
        trace_id: str,
        decision_made: dict[str, Any],
        context_snapshot: dict[str, Any] | None = None,
        metrics_observed: dict[str, Any] | None = None,
        graph_version: str = "",
        cpd_version: str = "",
        capability_nonce: str = "",
    ) -> str:
        """追加 AuditEvent。返回 event_id。"""
        if phase not in AUDIT_PHASES:
            raise ValueError(f"非法 phase {phase!r} (允许: {AUDIT_PHASES})")
        entry: dict[str, Any] = {
            "event_id": new_id("aud_"),
            "timestamp": _now_iso(),
            "phase": phase,
            "trace_id": trace_id,
            "graph_version": graph_version,
            "cpd_version": cpd_version,
            "context_snapshot": context_snapshot or {},
            "decision_made": decision_made,
            "metrics_observed": metrics_observed,
            "capability_nonce": capability_nonce,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
        return entry["event_id"]

    def by_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """按 trace_id 关联全部节点。"""
        if not self._path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("trace_id") == trace_id:
                out.append(entry)
        return out


__all__ = ["AUDIT_PHASES", "AuditLog", "EVENT_TYPES", "EventStore", "new_id"]

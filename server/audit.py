"""Veya Audit — 决策审计统一读出口(薄适配层)。

3O 单一来源 (§1.4): 写出口本体已固化为主库 oprim._audit_emit.AuditEmitter
(统一 Schema + JSONL sink + trace 回放), 事务层(closed_loop_intervene /
multi_step_plan)已在关键节点自动写审计。
本层提供读侧 API: 按 trace_id 回放完整决策链路(取证/追责), 列 trace 清单。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from veya.platform import oprim as _load_oprim

_oprim = _load_oprim()

DEFAULT_AUDIT_DIR = "~/.veya/audit"


class VeyaAudit:
    """审计回放器: 把一次故障处理链路按写入顺序还原。"""

    def __init__(self, audit_dir: str | Path = DEFAULT_AUDIT_DIR):
        self.audit_dir = Path(audit_dir).expanduser()
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    def _sink(self) -> Any:
        # 生产默认: 单 JSONL 文件 (跨事务追加)
        return _oprim.JsonlSink(str(self.audit_dir / "audit.jsonl"))

    def replay(self, trace_id: str) -> dict[str, Any]:
        """回放一条 trace 的完整决策链路。"""
        events = self._sink().read_trace(trace_id)
        return {
            "trace_id": trace_id,
            "event_count": len(events),
            "events": events,
        }

    def traces(self, limit: int = 50) -> dict[str, Any]:
        """列出最近 trace 清单 (trace_id + 事件数 + 时间范围)。"""
        all_events = self._sink().read_all(limit=0)
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

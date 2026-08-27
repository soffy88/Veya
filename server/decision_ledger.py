"""decision_ledger — 决策账本 (Semantica Decision Intelligence 轻量内化, 2026-08-16)。

每个决策是一等公民记录: category / scenario / reasoning / outcome / confidence,
带因果 parent 链与元数据。可: 查先例 (find_similar_decisions)、追踪因果链
(trace_decision_chain)、分析影响 (analyze_decision_impact)、策略门
(check_decision_rules)、JSON 审计导出 (export_ledger)。

设计要点 (内化而非外包):
- SQLite 持久化 (~/.veya/decision_ledger.db), stdlib 零依赖, 与 auth.py 同模式
- 只记录, 不判断: 账本不参与任何路由决策 — 记录是审计/护栏, 判断仍由大模型做
- 接入点: project_ask 每次门禁判定/派工结果自动记录; 主脑经 decision_* 工具
  自主查询 (模型自主选择, 零程序路由, 不违反冻结架构)
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = None  # lazy

_DB_PATH = os.environ.get(
    "VEYA_DECISION_LEDGER_DB", str(Path.home() / ".veya" / "decision_ledger.db")
)
_DEFAULT_RULE_MIN_CONFIDENCE = float(os.environ.get("DECISION_RULE_MIN_CONFIDENCE", "0.5"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,
    parent_id   TEXT,
    category    TEXT NOT NULL,
    scenario    TEXT NOT NULL,
    reasoning   TEXT NOT NULL DEFAULT '',
    outcome     TEXT NOT NULL DEFAULT '',
    confidence  REAL NOT NULL DEFAULT 0.0,
    source      TEXT NOT NULL DEFAULT '',
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_category ON decisions(category);
CREATE INDEX IF NOT EXISTS idx_decisions_parent ON decisions(parent_id);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);
"""


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    # 同一毫秒多次记录会撞主键 — 加随机后缀 (uuid hex 4位, 概率可忽略)
    import uuid

    return f"dl_{int(time.time() * 1000) % 10_000_000:07d}_{uuid.uuid4().hex[:4]}"


class DecisionLedger:
    """决策账本 (SQLite 持久化, check_same_thread=False 与 auth.py 同模式)。

    默认全局实例 ``ledger`` (模块底部) 供 project_ask / 工具层使用;
    测试可注入临时路径。
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = str(db_path or _DB_PATH)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── 写 ─────────────────────────────────────────────────────────
    def record_decision(
        self,
        category: str,
        scenario: str,
        reasoning: str = "",
        outcome: str = "",
        confidence: float = 0.0,
        *,
        parent_id: str | None = None,
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """记录一条决策, 返回决策 id。parent_id 建立因果链 (trace/impact 用)。"""
        did = _new_id()
        self._conn.execute(
            "INSERT INTO decisions (id, parent_id, category, scenario, reasoning,"
            " outcome, confidence, source, metadata, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                did,
                parent_id,
                category[:200],
                scenario[:2000],
                reasoning[:4000],
                outcome[:2000],
                float(confidence or 0.0),
                source[:100],
                json.dumps(metadata or {}, ensure_ascii=False)[:8000],
                _now(),
            ),
        )
        self._conn.commit()
        return did

    # ── 读 ─────────────────────────────────────────────────────────
    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def trace_decision_chain(self, decision_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """因果链: 沿 parent_id 上溯到根 (最新在前)。"""
        chain: list[dict[str, Any]] = []
        cur = decision_id
        seen: set[str] = set()
        for _ in range(limit):
            if cur in seen:
                break  # 防环
            seen.add(cur)
            d = self.get_decision(cur)
            if not d:
                break
            chain.append(d)
            cur = d.get("parent_id") or ""
            if not cur:
                break
        return chain

    def analyze_decision_impact(self, decision_id: str) -> dict[str, Any]:
        """影响图: 谁直接以它为因果父 (下游影响统计)。"""
        rows = self._conn.execute(
            "SELECT id, category, outcome, created_at FROM decisions WHERE parent_id = ?",
            (decision_id,),
        ).fetchall()
        children = [dict(r) for r in rows]
        return {
            "decision_id": decision_id,
            "direct_children": len(children),
            "children": children,
            "by_outcome": _count_by(children, "outcome"),
            "by_category": _count_by(children, "category"),
        }

    def find_similar_decisions(
        self, query: str, *, category: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """先例检索: 场景/推理/结果文本关键词打分 + 类别过滤 (确定性, 无 embedding 依赖)。

        打分 = 查询词 (≥2 字) 在 scenario/reasoning/outcome 中的命中数, 加 confidence 微调。
        """
        words = [w for w in _tokenize(query) if len(w) >= 2]
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE (? IS NULL OR category = ?)"
            " ORDER BY created_at DESC LIMIT 200",
            (category, category),
        ).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for r in rows:
            d = dict(r)
            hay = f"{d['scenario']} {d['reasoning']} {d['outcome']}".lower()
            hits = sum(hay.count(w.lower()) for w in words)
            if hits:
                scored.append((hits + d["confidence"] * 0.1, d))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [d for _, d in scored[:limit]]

    def check_decision_rules(self, criteria: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """轻量策略门 (Semantica check_decision_rules 内化): 对账本存量决策跑规则。

        内置规则: 低置信度决策计数 (可配置阈值); 未来规则 (如 category 专属
        约束) 由调用方在 criteria 里传入, 本函数只做确定性检查, 不裁决执行。
        """
        criteria = criteria or {}
        min_conf = float(criteria.get("min_confidence", _DEFAULT_RULE_MIN_CONFIDENCE))
        rows = self._conn.execute(
            "SELECT COUNT(*) c FROM decisions WHERE confidence < ?",
            (min_conf,),
        ).fetchone()
        low_conf = rows["c"] if rows else 0
        return [
            {
                "rule": "low_confidence",
                "threshold": min_conf,
                "matched": low_conf,
                "status": "warn" if low_conf > 0 else "pass",
            }
        ]

    def export_ledger(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """审计导出 (JSON 行): 全部/最近 N 条决策, 含因果 parent 引用。"""
        sql = "SELECT * FROM decisions ORDER BY created_at DESC"
        params: tuple = ()
        if limit:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def summary(self, limit: int = 8) -> str:
        """人类可读摘要 (供 project_ask 上下文前缀 / 诊断)。"""
        rows = self._conn.execute(
            "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        if not rows:
            return "决策账本: 暂无记录。"
        lines = [f"决策账本: 最近 {len(rows)} 条"]
        for r in rows:
            d = dict(r)
            icon = "✅" if d["outcome"] in ("completed", "act") else "⛔"
            lines.append(
                f"  {icon} #{d['id']} [{d['category']}] conf={d['confidence']:.2f} — "
                f"{(d['scenario'] or '')[:60]} → {d['outcome']}"
            )
        return "\n".join(lines)

    def close(self) -> None:
        self._conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["metadata"] = json.loads(d.get("metadata") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["metadata"] = {}
    return d


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[str(it.get(key) or "unknown")] = out.get(str(it.get(key) or "unknown"), 0) + 1
    return out


def _tokenize(text: str) -> list[str]:
    """中英文分词 (轻量): 中文按连续非空白段 + 2-gram, 英文按单词。"""
    import re

    parts = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    out: list[str] = []
    for p in parts:
        if re.fullmatch(r"[a-z0-9_]+", p):
            out.append(p)
        else:
            # 中文: 整段 + 2-gram (无 jieba 依赖)
            out.append(p)
            if len(p) > 2:
                out.extend(p[i : i + 2] for i in range(len(p) - 1))
    return out


# ── 全局单例 (与 hicode_task_queue / master_tools 同模式) ──────────
ledger = DecisionLedger()


__all__ = [
    "DecisionLedger",
    "analyze_decision_impact",
    "check_decision_rules",
    "export_ledger",
    "find_similar_decisions",
    "ledger",
    "record_decision",
    "trace_decision_chain",
]


def record_decision(
    category: str,
    scenario: str,
    reasoning: str = "",
    outcome: str = "",
    confidence: float = 0.0,
    *,
    parent_id: str | None = None,
    source: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    return ledger.record_decision(
        category,
        scenario,
        reasoning,
        outcome,
        confidence,
        parent_id=parent_id,
        source=source,
        metadata=metadata,
    )


def trace_decision_chain(decision_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return ledger.trace_decision_chain(decision_id, limit=limit)


def find_similar_decisions(
    query: str, *, category: str | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    return ledger.find_similar_decisions(query, category=category, limit=limit)


def analyze_decision_impact(decision_id: str) -> dict[str, Any]:
    return ledger.analyze_decision_impact(decision_id)


def check_decision_rules(criteria: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return ledger.check_decision_rules(criteria)


def export_ledger(*, limit: int | None = None) -> list[dict[str, Any]]:
    return ledger.export_ledger(limit=limit)

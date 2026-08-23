"""server.vaom_query_tools — MasterAgent 只读查询工具: harness 历史表现 + 项目经验教训。

P5 落地（PR-25 的最小可行版本，见 docs/dev/rfc-01-vaom.md、
docs/VEYA_3.0_GAP_AUDIT.md）。2026-08-23 用户已按 `ARCHITECTURE_STABLE.md` §4
明确批准新增这两个工具到 MasterAgent 主链工具面。

两个都是纯只读、无副作用，数据来自 P2/P3 已经在跑的旁路记录
（`server/capability_model.py::performance_store`、
`server/memory_controller.py::memory_controller`）——不是给模型代做选择，是给
证据，模型自己判断要不要参考（VAOM P2 原则"Registry 提供证据不替模型思考"，
见 `docs/dev/rfc-01-vaom.md`）。

现实情况：这两个数据源刚建成不久，短期内大概率返回"样本太少"/"没查到"——
如实返回，不伪造数据掩盖这一点。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any


def harness_performance_query(harness_id: str = "", task_archetype: str = "") -> dict[str, Any]:
    """查某个执行者的历史表现，或不传 harness_id 时对比全部已知执行者。"""
    from server.capability_model import harness_registry, performance_store

    archetype = task_archetype or None
    if harness_id:
        profile = performance_store.aggregate(harness_id, archetype)
        if profile is None:
            return {
                "harness_id": harness_id,
                "status": "no_data",
                "message": "还没有该执行者的历史样本(数据随 goal_run 实际执行积累)。",
            }
        return {"harness_id": harness_id, "status": "ok", **asdict(profile)}

    known = [h.harness_id for h in harness_registry.list()]
    comparison = performance_store.compare(known, archetype)
    if not comparison:
        return {
            "status": "no_data",
            "known_harnesses": known,
            "message": "还没有任何执行者的历史样本。",
        }
    return {"status": "ok", "profiles": {hid: asdict(p) for hid, p in comparison.items()}}


def memory_recall_project_lessons(query: str = "", scope: str = "") -> dict[str, Any]:
    """召回过往任务积累的经验教训。关键词匹配，不是语义检索(见模块 docstring)。"""
    from server.memory_controller import memory_controller

    records = memory_controller.search(query, scope=scope or None)
    if not records:
        return {
            "status": "no_data",
            "message": "没查到匹配的记忆条目(关键词匹配, 换个说法可能查得到; 也可能是真的还没积累)。",
        }
    return {
        "status": "ok",
        "lessons": [
            {
                "content": r.content,
                "status": r.status,
                "trust_level": r.trust_level,
                "scope": r.scope,
                "provenance": r.provenance,
            }
            for r in records
        ],
    }

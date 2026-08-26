"""server.vaom_query_tools — MasterAgent 只读查询工具: harness 历史表现 + 项目经验教训。

P5 落地（PR-25 的最小可行版本，见 docs/dev/rfc-01-vaom.md、
docs/VEYA_3.0_GAP_AUDIT.md）。2026-08-23 用户已按 `ARCHITECTURE_STABLE.md` §4
明确批准新增这两个工具到 MasterAgent 主链工具面。

两个都是纯只读、无副作用。生产 Memory 读取来自 Personal Runtime 的
PostgreSQL authority；没有生产 DSN 的旧单元测试/本地开发才保留旧适配器，
不改变生产 authority。

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
    import os

    # Existing isolated tests exercise the pre-v2 adapter without a durable
    # DSN. Production always has the PostgreSQL DSN and therefore cannot read
    # the legacy JSON authority.
    if not os.environ.get("VEYA_EXECUTION_DATABASE_URL") and os.environ.get("VEYA_EXECUTION_PRODUCTION", "0") in {"", "0", "false", "off", "no"}:
        from server.memory_controller import memory_controller

        records = memory_controller.search(query, scope=scope or None)
        lessons = [
            {
                "content": r.content,
                "status": r.status,
                "trust_level": r.trust_level,
                "scope": r.scope,
                "provenance": r.provenance,
            }
            for r in records
        ]
    else:
        from runtime.personal import get_personal_runtime
        from server import auth as auth_mod

        user_id = str(auth_mod.current_user().get("user_id") or "anonymous")
        scope_type = {"global": "user", "user": "user", "project": "workspace", "workspace": "workspace", "session": "session"}.get(scope or "", scope or None)
        scope_id = user_id if scope in {"global", "user"} else os.environ.get("VEYA_WORKSPACE") if scope in {"project", "workspace"} else None
        personal = get_personal_runtime()
        records = personal.run_sync(
            personal.search_memory(query, scope_type=scope_type, scope_id=scope_id)
        )
        lessons = [
            {
                "content": r["content"],
                "status": r["status"],
                "confidence": r["confidence"],
                "scope_type": r["scope_type"],
                "scope_id": r["scope_id"],
                "provenance": r.get("provenance", {}),
                "source_event_ids": r["source_event_ids"],
                "last_verified_at": r.get("last_verified_at"),
            }
            for r in records
        ]
    if not lessons:
        return {
            "status": "no_data",
            "message": "没查到匹配的记忆条目(关键词匹配, 换个说法可能查得到; 也可能是真的还没积累)。",
        }
    return {
        "status": "ok",
        "lessons": lessons,
    }

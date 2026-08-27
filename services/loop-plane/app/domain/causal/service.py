"""loop-plane domain.causal — 因果规划与诊断（SPEC §4.3 / §6.2）。

底层算法来自 veya_loop（经其惰性 _ELEMENT_MAP 解析 3O 主库符号）:
    multi_step_plan / causal_fault_diagnose
无库可用（VEYA_LOOP_OPTIONAL）时明确降级错误（503 语义）。

plan_for_goal（SPEC §8）:
    goal → 兼容信号 prompt → multi_step_plan(execute=False)
    → 适配 GoalPlanReport {ranked_actions, trace_id}
二期: 显式 target_node + UtilitySpec（去字符串伪装）。
"""

from __future__ import annotations

from typing import Any

from app.domain.state.projectors import render_text
from app.infra.event_store import AuditLog, EventStore, new_id


def _require_causal():
    """惰性解析 veya_loop 因果符号；VEYA_LOOP_OPTIONAL=true 时返回 None。"""
    try:
        import veya_loop

        return veya_loop
    except Exception:  # noqa: BLE001
        return None


def plan_for_goal(
    goal: str,
    criteria: str = "",
    *,
    store: EventStore | None = None,
    execute: bool = False,
    trace_id: str = "",
) -> dict[str, Any]:
    """Goal → 兼容信号 → multi_step_plan(execute=False) → GoalPlanReport。"""
    trace_id = trace_id or new_id("trc_")
    vl = _require_causal()
    if vl is None:
        raise RuntimeError("veya_loop 不可用 (VEYA_LOOP_OPTIONAL=true 降级失败)")

    graph_store = _graph_store(store)
    prompt = f"GOAL_UNMET: {goal}"
    if criteria:
        prompt += f"\nACCEPT_CRITERIA: {criteria}"

    plan_fn = getattr(vl, "multi_step_plan", None)
    if plan_fn is None:
        raise RuntimeError("veya_loop.multi_step_plan 不可用")
    # multi_step_plan(failure_log, store=..., execute=...) → MultiStepPlanReport 对象
    raw = plan_fn(prompt, store=graph_store, execute=execute)

    report: dict[str, Any] = {
        "goal": goal,
        "criteria": criteria,
        "trace_id": trace_id,
        "execute": execute,
        "ranked_actions": _adapt_actions(raw),
        "raw": str(raw)[:400],
    }
    return report


def diagnose(
    symptom: str,
    context: dict[str, Any] | None = None,
    *,
    trace_id: str = "",
) -> dict[str, Any]:
    """故障诊断（主调 causal_fault_diagnose；缺干预时补 select_intervention）。"""
    trace_id = trace_id or new_id("trc_")
    vl = _require_causal()
    if vl is None:
        raise RuntimeError("veya_loop 不可用 (VEYA_LOOP_OPTIONAL=true 降级失败)")

    fn = getattr(vl, "causal_fault_diagnose", None)
    if fn is None:
        raise RuntimeError("veya_loop.causal_fault_diagnose 不可用")
    # 主库签名: causal_fault_diagnose(failure_log, *, store=None, ...) → CausalDiagnosisReport
    report = fn(symptom)

    # 缺干预 → select_intervention（存在时）
    intervention = getattr(report, "intervention", None) or (
        report.get("intervention") if isinstance(report, dict) else None
    )
    if not intervention:
        sel = getattr(vl, "select_intervention", None)
        if sel is not None:
            try:
                intervention = sel(report)
            except Exception:  # noqa: BLE001
                intervention = None

    return {
        "symptom": symptom,
        "trace_id": trace_id,
        "root_causes": _root_causes(report),
        "intervention": intervention,
        "raw": str(report)[:400],
    }


# ---------------------------------------------------------------------------
# 适配辅助（对 veya_loop 输出做结构适配，保持 API 稳定）
# ---------------------------------------------------------------------------


def _adapt_actions(raw: Any) -> list[dict[str, Any]]:
    """MultiStepPlanReport / 列表 / 文本 → [{action, reason, priority}]。"""
    # MultiStepPlanReport: .recommended_actions / .plan.planned_actions
    planned = getattr(raw, "recommended_actions", None)
    if planned is None:
        plan_obj = getattr(raw, "plan", None)
        planned = getattr(plan_obj, "planned_actions", None)
    if planned is not None:
        out: list[dict[str, Any]] = []
        for i, item in enumerate(planned):
            if isinstance(item, dict):
                out.append(
                    {
                        "action": item.get("action") or item.get("step") or str(item),
                        "reason": item.get("reason", ""),
                        "priority": item.get("priority", i + 1),
                    }
                )
            else:
                out.append({"action": str(item), "reason": "", "priority": i + 1})
        return out
    if isinstance(raw, list):
        out = []
        for i, item in enumerate(raw):
            if isinstance(item, dict):
                out.append(
                    {
                        "action": item.get("action") or item.get("step") or str(item),
                        "reason": item.get("reason", ""),
                        "priority": item.get("priority", i + 1),
                    }
                )
            else:
                out.append({"action": str(item), "reason": "", "priority": i + 1})
        return out
    # 文本/其他: 单步包装
    return [{"action": str(raw)[:300], "reason": "", "priority": 1}]


def _root_causes(report: Any) -> list[dict[str, Any]]:
    """CausalDiagnosisReport / dict → root_causes 列表。"""
    causes = getattr(report, "root_cause_candidates", None)
    if causes is None and isinstance(report, dict):
        causes = report.get("root_cause_candidates") or report.get("root_causes")
    if isinstance(causes, list):
        out: list[dict[str, Any]] = []
        for item in causes:
            if isinstance(item, dict):
                out.append(item)
            else:
                out.append({"cause": str(item)})
        return out
    text = getattr(report, "diagnosis", "") or (
        report.get("diagnosis", report.get("cause", "")) if isinstance(report, dict) else ""
    )
    return [{"cause": str(text)}] if text else []


def _graph_store(store: EventStore | None):
    """graph_repo：CausalGraphStore（veya_loop 惰性解析, SPEC §8 load_or_empty）。
    二期: 从事件流重建/持久化图（graphs/{graph_id}.json）。"""
    try:
        import veya_loop

        graph_store = getattr(veya_loop, "CausalGraphStore", None)
        if graph_store is not None:
            return graph_store()
    except Exception:  # noqa: BLE001
        pass
    return store  # 降级: 无库时沿用调用方 store（multi_step_plan 会明确报错）


class CausalService:
    """因果服务门面：plan_for_goal / diagnose + 统一审计写入（SPEC §6.2）。"""

    def __init__(self, store: EventStore | None = None, audit: AuditLog | None = None) -> None:
        self._store = store
        self._audit = audit

    def plan_goal(
        self, goal: str, criteria: str = "", *, execute: bool = False, trace_id: str = ""
    ) -> dict[str, Any]:
        report = plan_for_goal(
            goal, criteria, store=self._store, execute=execute, trace_id=trace_id
        )
        if self._audit is not None:
            audit_and_report(
                self._audit,
                phase="plan",
                trace_id=report["trace_id"],
                decision_made={"goal": goal, "actions": len(report["ranked_actions"])},
                context_snapshot={"criteria": criteria},
            )
        return report

    def diagnose(
        self, symptom: str, context: dict[str, Any] | None = None, *, trace_id: str = ""
    ) -> dict[str, Any]:
        report = diagnose(symptom, context, trace_id=trace_id)
        if self._audit is not None:
            audit_and_report(
                self._audit,
                phase="diagnose",
                trace_id=report["trace_id"],
                decision_made={"symptom": symptom, "root_causes": len(report["root_causes"])},
                context_snapshot=context or {},
            )
        return report


def audit_and_report(
    audit: AuditLog,
    *,
    phase: str,
    trace_id: str,
    decision_made: dict[str, Any],
    context_snapshot: dict[str, Any] | None = None,
) -> str:
    """统一审计写入（SPEC §6.2：API 层统一写 Audit，调用库避免双写）。"""
    return audit.append(
        phase=phase,
        trace_id=trace_id,
        decision_made=decision_made,
        context_snapshot=context_snapshot,
    )


__all__ = ["CausalService", "audit_and_report", "diagnose", "plan_for_goal"]

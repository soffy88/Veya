"""Veya NeuroSymbolic — 神经符号逻辑引擎(薄适配层)。

3O 单一来源 (§1.4): 管线本体已固化为主库 omodul.neuro_symbolic.run_neuro_symbolic
(组合 oprim._plan_ir 校验 + _backtranslate 回译 + _ir_compile 编译 +
_mus 收缩 + _ir_solve MaxSMT, 四道闸门)。
本层保留脚手架 API: VeyaNeuroSymbolic.plan(raw_ir)。
LLM 在这里不参与判定 —— 它只负责产出 IR JSON 和按 RepairPayload 反思重试。
"""

from __future__ import annotations

from typing import Any

from veya.platform import omodul as _load_omodul

_omodul = _load_omodul()


class VeyaNeuroSymbolic:
    """神经符号规划器: 意图 IR → 可验证 Plan(带矛盾核心 / 回译证据 / 唯一解)。"""

    @staticmethod
    def plan(raw_ir: Any, **kwargs: Any) -> dict[str, Any]:
        """跑 O1 四道闸门(纯计算, 同步)。

        kwargs: seed / feas_timeout_ms / opt_timeout_ms / deterministic_tiebreak /
                strict_diff(回译 diff 是否阻断)。

        Returns:
            {ok, plan_id, stage, solution, repair, ...} —
            ok=False 时 repair 是喂回 LLM 的确定性反馈。
        """
        res = _omodul.run_neuro_symbolic(raw_ir, **kwargs)
        out: dict[str, Any] = {
            "ok": res.ok,
            "plan_id": res.plan_id,
            "stage": res.stage,
        }
        if res.errors:
            out["errors"] = [e.as_dict() for e in res.errors]
        if res.diffs:
            out["diffs"] = [
                {"id": d.cid, "intent": d.intent, "rendered": d.rendered,
                 "similarity": d.similarity,
                 "blocked": d.blocked,
                 "findings": [f.__dict__ for f in d.findings]}
                for d in res.diffs
            ]
        if res.feasibility is not None:
            fe = res.feasibility
            out["feasibility"] = {"status": fe.status, "checks": fe.checks,
                                  "elapsed_ms": fe.elapsed_ms,
                                  "reason_unknown": fe.reason_unknown}
            if fe.mus is not None:
                out["feasibility"]["mus"] = {
                    "ids": fe.mus.mus, "verified": fe.mus.verified,
                    "dropped": fe.mus.dropped, "checks": fe.mus.checks,
                }
        if res.solution is not None:
            s = res.solution
            out["solution"] = {"status": s.status, "assignment": s.assignment,
                               "objective_value": s.objective_value,
                               "relaxed_soft": s.relaxed_soft,
                               "satisfied_soft": s.satisfied_soft,
                               "elapsed_ms": s.elapsed_ms}
        if res.repair is not None:
            out["repair"] = res.repair.as_dict()
        return out

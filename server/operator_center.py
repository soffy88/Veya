"""Veya OperatorCenter — 确定性分配 / 激励相容支付 / 死锁防线(薄适配层)。

3O 单一来源 (§1.4): 管线本体已固化为主库 omodul.operator_center.run_operator_center
(组合 oprim._allocate 匈牙利/MILP + _payments VCG + _deadlock 全序/租约 + _ledger 账本)。
本层保留脚手架 API: VeyaOperatorCenter.dispatch(problem_dict, ...)。
"""

from __future__ import annotations

from typing import Any

from veya.platform import omodul as _load_omodul
from veya.platform import oprim as _load_oprim

_omodul = _load_omodul()
_oprim = _load_oprim()


class VeyaOperatorCenter:
    """算子调度中心: 报价 → 组合最优化分配 → 支付 → 死锁安全执行计划 → 账本。"""

    @staticmethod
    def dispatch(problem_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """跑 O2 管线(纯计算, 同步)。

        problem_dict: {"tasks": [...], "workers": [...], "bids": [...],
                       "unassigned_penalty": float}
        kwargs: mode(auto|one_to_one|capacity) / payment_rule(None|first_price|
                second_price|vcg) / resource_ranking / balance_weight。

        Returns:
            {decision_id, ok, allocation, payments, acquisition_plan,
             escalations, replay_key, welfare}
        """
        p = _oprim.Problem(
            tasks=[_oprim.Task(**t) for t in problem_dict.get("tasks", [])],
            workers=[_oprim.Worker(**w) for w in problem_dict.get("workers", [])],
            bids=[_oprim.Bid(**b) for b in problem_dict.get("bids", [])],
            unassigned_penalty=float(problem_dict.get("unassigned_penalty", 1e6)),
        )
        d = _omodul.run_operator_center(p, **kwargs)

        alloc = d.allocation
        out: dict[str, Any] = {
            "decision_id": d.decision_id,
            "ok": d.ok,
            "welfare": d.welfare,
            "allocation": {
                "method": alloc.method,
                "pairs": [list(x) for x in alloc.pairs],
                "by_worker": alloc.by_worker(),
                "unassigned": alloc.unassigned,
                "total_cost": alloc.total_cost,
            },
            "acquisition_plan": d.acquisition_plan,
            "escalations": [e.__dict__ for e in d.escalations],
            "replay_key": d.ledger.replay_key(),
        }
        if d.payments is not None:
            out["payments"] = {
                "rule": d.payments.rule,
                "payments": d.payments.payments,
                "total": d.payments.total(),
                "solves": d.payments.solves,
                "detail": d.payments.detail,
            }
        return out

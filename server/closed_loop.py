"""Veya ClosedLoop — 反脆弱闭环事务(薄适配层)。

3O 单一来源 (§1.4): 事务本体已固化为主库 omodul.closed_loop_intervene
(组合 oprim._expected_utility_select 效用选择 + oskill._online_cpd_update
Dirichlet/EMA 在线更新)。
本层保留脚手架 API: VeyaClosedLoop.run(cpd, ...)。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from veya.platform import omodul as _load_omodul
from veya.platform import oskill as _load_oskill

_omodul = _load_omodul()
_oskill = _load_oskill()


class VeyaClosedLoop:
    """闭环干预: 诊断 → 效用选择 → 执行/模拟 → 观测 → 在线更新因果模型。"""

    def __init__(
        self,
        output_dir: str | Path = "~/.veya/closed_loop",
        audit_dir: str | Path = "~/.veya/audit",
    ):
        self.output_dir = Path(output_dir).expanduser()
        self.audit_dir = Path(audit_dir).expanduser()
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    async def run(
        self,
        cpd: dict[str, Any],
        *,
        interventions: list[dict[str, Any]] | None = None,
        diagnosis: dict[str, Any] | None = None,
        baseline_config: str = "degraded",
        fault_state: str = "fault",
        lambda_cost: float = 1.0,
        risk_aversion: float = 1.0,
        update_mode: str = "dirichlet",
        rounds: int = 1,
        execute_fn: Callable | None = None,
        audit_path: str | None = None,
    ) -> dict[str, Any]:
        """跑一轮闭环。

        cpd: {"child_states": [...], "counts": {...}, "parents": [...]} —
             在线更新的因果模型本体(跨调用持久化即在线积累)。
        interventions: [{"action_id", "target_value", "cost", "risk",
                         "delta_p"(可选, 缺省从 CPD 现算)}]。
        execute_fn: 注入执行器 (action_id, target_value) -> {"success": bool};
                    缺省 simulate=True 用当前 CPD 蒙特卡洛模拟。

        Returns:
            {status, fingerprint, executed, p_fault_before/after, cpd_after,
             cpd_path, ...} — cpd_after 应被调用方持久化, 作为下一次的 cpd 输入。
        """
        cpd_obj = _oskill.CategoricalCPD.from_dict(cpd)
        inp = _omodul.ClosedLoopInput(cpd_obj, diagnosis=diagnosis, interventions=interventions)
        cfg = _omodul.ClosedLoopConfig(
            lambda_cost=lambda_cost,
            risk_aversion=risk_aversion,
            update_mode=update_mode,
            rounds=rounds,
            baseline_config=baseline_config,
            fault_state=fault_state,
            execute_fn=execute_fn,
            audit_path=audit_path or str(self.audit_dir / "audit.jsonl"),
        )
        return await _omodul.closed_loop_intervene(cfg, inp, self.output_dir)

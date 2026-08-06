"""Neuro-symbolic route — O1 神经符号规划器 API + P1 机制面 (O2/O3)。

3O 铁律 §1.4 — 机制全部在主库 oprim (分配/VCG/死锁/博弈),
本路由只装配转发, 不重实现。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.neuro_symbolic import VeyaNeuroSymbolic

router = APIRouter(prefix="/neurosymbolic", tags=["neurosymbolic"])


def _oprim():
    from veya.platform import oprim

    return oprim()


class PlanRequest(BaseModel):
    ir: dict[str, Any]  # Plan IR JSON (LLM 产出, 机器判定)
    seed: int = 0
    strict_diff: bool = True
    deterministic_tiebreak: bool = True


@router.post("/plan")
async def neuro_symbolic_plan(req: PlanRequest) -> dict[str, Any]:
    """O1 四道闸门: 校验 → 回译 diff → Z3 可行性+MUS → MaxSMT 唯一最优解。

    ok=False 时返回 repair(喂回 LLM 的确定性反馈: 矛盾核心/回译证据/修复 hint)。
    """
    if not req.ir:
        raise HTTPException(status_code=422, detail="ir 不能为空")
    return VeyaNeuroSymbolic.plan(
        req.ir,
        seed=req.seed,
        strict_diff=req.strict_diff,
        deterministic_tiebreak=req.deterministic_tiebreak,
    )


# ═══════════════════════════════════════════════════════════════════════════
# P1 · O2 组合优化分配 + VCG 支付 + 策略证明
# ═══════════════════════════════════════════════════════════════════════════


class WorkerSpec(BaseModel):
    id: str
    capacity: dict[str, float] = Field(default_factory=dict)  # skill → 能力值
    max_tasks: int = 1


class TaskSpec(BaseModel):
    id: str
    demand: dict[str, float] = Field(default_factory=dict)  # skill → 需求量
    priority: float = 1.0


class AllocateRequest(BaseModel):
    workers: list[WorkerSpec]
    tasks: list[TaskSpec]
    risk_adjusted: bool = True
    verify_strategyproof: bool = True
    unassigned_penalty: float = 1000000.0  # 无人承接任务的罚金 (VCG 支付基准)


@router.post("/allocate")
async def neuro_symbolic_allocate(req: AllocateRequest) -> dict[str, Any]:
    """最优分配 (匈牙利/容量) → 总福利 → VCG 支付 → 策略证明(可选)。

    输入 workers(技能能力) + tasks(技能需求) → 输出 {"assignment", "welfare",
    "payments", "strategyproof"}。VCG 支付保证真实报价是弱占优策略。
    """
    if not req.workers or not req.tasks:
        raise HTTPException(status_code=422, detail="workers 与 tasks 不能为空")
    oprim = _oprim()

    # 报价: 每个技能维度 demand/capacity 的时间比, 取最大; 无对应技能 → inf
    def _bid_cost(worker: WorkerSpec, task: TaskSpec) -> float | None:
        ratios: list[float] = []
        for skill, need in task.demand.items():
            cap = worker.capacity.get(skill)
            if not cap or cap <= 0:
                return None
            ratios.append(need / cap)
        return max(ratios) if ratios else 0.0

    bids = []
    for w in req.workers:
        for t in req.tasks:
            c = _bid_cost(w, t)
            if c is not None:
                bids.append(oprim.Bid(w.id, t.id, round(c, 9)))
    if not bids:
        raise HTTPException(
            status_code=422, detail="无可行报价: 任何 worker 都不满足任何 task 的技能需求"
        )

    p = oprim.Problem(
        tasks=[oprim.Task(t.id, dict(t.demand), priority=t.priority) for t in req.tasks],
        workers=[oprim.Worker(w.id, dict(w.capacity), max_tasks=w.max_tasks) for w in req.workers],
        bids=bids,
        unassigned_penalty=req.unassigned_penalty,
    )
    alloc = oprim.assign_one_to_one(p, risk_adjusted=req.risk_adjusted)
    welfare = oprim.welfare(p, alloc)
    pay = oprim.vcg(p, alloc)

    out: dict[str, Any] = {
        "ok": alloc.solver_status == "ok",
        "method": alloc.method,
        "assignment": [{"worker": w, "task": t} for w, t in alloc.pairs],
        "unassigned": alloc.unassigned,
        "total_cost": alloc.total_cost,
        "welfare": round(welfare, 6),
        "payments": {wid: round(v, 6) for wid, v in pay.payments.items()},
        "payment_rule": pay.rule,
    }
    if req.verify_strategyproof:
        report = oprim.check_strategyproof(p, oprim.vcg, allocator=oprim.assign_one_to_one)
        best = report.best_deviation
        out["strategyproof"] = {
            "manipulable": report.manipulable,
            "probes": report.probes,
            "best_deviation": {
                "worker": best.worker_id,
                "task": best.task_id,
                "truthful_bid": best.truthful_bid,
                "misreport": best.misreport,
                "gain": best.gain,
            }
            if best
            else None,
        }
    return out


# ═══════════════════════════════════════════════════════════════════════════
# P1 · O2 死锁检测 (等待图环 + 预检 + 受害者建议)
# ═══════════════════════════════════════════════════════════════════════════


class WaitEdge(BaseModel):
    waiter: str
    holder: str
    resource: str = ""


class DeadlockRequest(BaseModel):
    edges: list[WaitEdge]  # 现有等待关系
    propose: WaitEdge | None = None  # 待加边 (预检是否成环)
    cost: dict[str, float] = Field(default_factory=dict)  # 回滚代价 (victim 选择)


@router.post("/deadlock")
async def neuro_symbolic_deadlock(req: DeadlockRequest) -> dict[str, Any]:
    """等待图环检测 + 新边死锁预检 + 受害者建议。

    cycles: 现存环 (需要打破); would_deadlock: 加 propose 边是否成环;
    victims: 每个环的代价最小回滚受害者。
    """
    oprim = _oprim()
    wfg = oprim.WaitForGraph()
    for e in req.edges:
        wfg.add_wait(e.waiter, e.holder, resource=e.resource)

    cycles = wfg.cycles()
    out: dict[str, Any] = {
        "ok": True,
        "cycles": cycles,
        "deadlocked": bool(cycles),
        "victims": [{"cycle": c, "victim": wfg.victim(c, cost=dict(req.cost))} for c in cycles],
    }
    if req.propose is not None:
        out["would_deadlock"] = wfg.would_deadlock(req.propose.waiter, req.propose.holder)
        if not out["would_deadlock"]:
            out["propose_safe"] = True
    return out


# ═══════════════════════════════════════════════════════════════════════════
# P1 · 博弈论 (纯纳什 / 帕累托前沿 / 主导策略)
# ═══════════════════════════════════════════════════════════════════════════


class GameRequest(BaseModel):
    A: list[list[float]]  # 行玩家收益矩阵
    B: list[list[float]]  # 列玩家收益矩阵
    row_labels: list[str] = Field(default_factory=list)
    col_labels: list[str] = Field(default_factory=list)


@router.post("/game")
async def neuro_symbolic_game(req: GameRequest) -> dict[str, Any]:
    """双人有限博弈分析: 纯纳什均衡 / 帕累托最优 / 主导策略。

    输出 label 化结果 (如 [{"row": "r0", "col": "c1", "label": "(r0, c1)"}])
    供策略谈判/竞争推演直接消费。
    """
    if not req.A or not req.B or len(req.A) != len(req.B):
        raise HTTPException(status_code=422, detail="A/B 必须同形状非空矩阵")
    oprim = _oprim()
    g = oprim.Game(
        req.A, req.B, row_labels=req.row_labels or None, col_labels=req.col_labels or None
    )
    out: dict[str, Any] = {
        "ok": True,
        "shape": [g.A.shape[0], g.A.shape[1]],
        "pure_nash": [{"row": i, "col": j, "label": g.label(i, j)} for i, j in oprim.pure_nash(g)],
        "pareto_optimal": [
            {"row": i, "col": j, "label": g.label(i, j)} for i, j in oprim.pareto_optimal(g)
        ],
        "report": oprim.nash_vs_pareto_report(g),
    }
    rows, cols = oprim.dominant_strategies(g)
    out["dominant"] = {"rows": rows, "cols": cols}
    return out

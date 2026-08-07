"""veya_loop.cli — veya-loop 命令行入口 (通用能力入口, 薄封装)。

子命令:
  version     打印版本
  selftest    冒烟: 装配面完整性 + 关键机制链路 (四闸门/VCG/闭环/审计)
  plan        多步规划 (multi_step_plan 薄封装, --json 传输入)
  diagnose    因果故障诊断 (causal_fault_diagnose 薄封装, --json 传输入)

设计约束: 无第三方依赖 (argparse + stdlib), 失败时退出码非零 + stderr 原因。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ._version import __version__


def _json_out(payload: dict[str, Any], *, ok: bool = True) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))
    if not ok:
        sys.exit(1)


def cmd_version(_: argparse.Namespace) -> None:
    _json_out({"ok": True, "veya_loop": __version__})


def cmd_selftest(_: argparse.Namespace) -> None:
    """冒烟测试: 逐面验证装配可达, 关键链路能跑通。"""
    import veya_loop as v

    checks: list[dict[str, Any]] = []

    def check(name: str, fn: Any) -> None:
        try:
            fn()
            checks.append({"name": name, "ok": True})
        except Exception as exc:
            checks.append({"name": name, "ok": False, "error": str(exc)})

    # ── 装配面: 代表性符号可解析 (惰性 __getattr__ 触发主库加载) ──
    for sym in (
        "PlanIR",
        "validate",
        "shrink_to_mus",
        "optimize",
        "vcg",
        "check_strategyproof",
        "LeaseManager",
        "MCTS",
        "puct",
        "SnapshotStore",
        "ActionPlan",
        "SandboxPool",
        "closed_loop_intervene",
        "multi_step_plan",
        "causal_fault_diagnose",
        "StructuralSCM",
        "run_code_reliability_loop",
        "AppendOnlyEventStore",
        "QuotaTracker",
        "GoalKernel",
        "Todo",
        "Gate",
        "Goal",
        "QuotaView",
        "IntegrityResult",
    ):
        check(f"export:{sym}", lambda s=sym: getattr(v, s))

    # ── 关键链路 1: 四闸门 O1 校验 (parse_ir → validate → compile → feasible) ──
    def gate_chain() -> None:
        ir = v.parse_ir(
            {
                "version": "o1.ir/v1",
                "intent": "selftest",
                "vars": [{"name": "x", "type": "int", "lo": 0, "hi": 10}],
                "constraints": [
                    {
                        "id": "c1",
                        "kind": "hard",
                        "intent": "x>=1",
                        "expr": {"op": ">=", "args": [{"var": "x"}, {"lit": 1}]},
                    }
                ],
            }
        )
        assert v.validate(ir) == [], f"IR 校验未通过: {v.validate(ir)}"
        compiled = v.compile_ir(ir)
        assert compiled is not None
        feas = v.check_feasible(compiled)
        assert feas.status == "sat", f"可满足性判定异常: {feas.status}"

    check("gate:validate→compile→feasible", gate_chain)

    # ── 关键链路 2: 分配 + VCG 支付 (2 工人 2 任务, 带报价) ──
    def vcg_chain() -> None:
        p = v.Problem(
            tasks=[v.Task("t1", {"s1": 1.0}), v.Task("t2", {"s1": 1.0})],
            workers=[v.Worker("a", {"s1": 5.0}), v.Worker("b", {"s1": 4.0})],
            bids=[
                v.Bid("a", "t1", 0.2),
                v.Bid("a", "t2", 0.2),
                v.Bid("b", "t1", 0.25),
                v.Bid("b", "t2", 0.25),
            ],
        )
        alloc = v.assign_one_to_one(p)
        assert alloc is not None and alloc.pairs, "分配结果为空"
        assert v.welfare(p, alloc) > 0.0
        pay = v.vcg(p, alloc)
        assert pay is not None

    check("gate:vcg", vcg_chain)

    # ── 关键链路 3: 死锁检测 (等待图环 p1↔p2 + 预检) ──
    def deadlock_chain() -> None:
        wfg = v.WaitForGraph()
        wfg.add_wait("p1", "p2", resource="r1")
        wfg.add_wait("p2", "p1", resource="r2")
        assert wfg.cycles() != [], "等待图环未检出"
        assert wfg.would_deadlock("p2", "p1") is True, "环预检漏报"
        assert wfg.would_deadlock("p3", "p1") is False, "无环误报"

    check("gate:deadlock", deadlock_chain)

    # ── 关键链路 4: P3 期望效用选型 (纯数值, 无 LLM) ──
    def loop_chain() -> None:
        cands = [
            v.InterventionCandidate("restart", delta_p=0.8, cost=0.2),
            v.InterventionCandidate("scale", delta_p=0.5, cost=0.4),
        ]
        sel = v.select_intervention(cands, lambda_cost=1.0)
        assert sel.best is not None, "期望效用选型未返回最优"
        assert sel.best.action_id == "restart", "未选中效用最高的干预"

    check("loop:expected_utility", loop_chain)

    # ── 关键链路 5: 审计 (AuditEmitter 内存 sink) ──
    def audit_chain() -> None:
        sink = v.MemorySink()
        em = v.AuditEmitter(sink)
        em.decide(decision={"chosen_strategy": "selftest"})
        assert len(sink.events) >= 1

    check("audit:emitter", audit_chain)

    # ── 关键链路 6: 长程状态内核 (事件溯源: goal → todo → 重建 → 续跑) ──
    def long_task_chain() -> None:
        import asyncio
        import tempfile
        from pathlib import Path

        async def run() -> None:
            store = v.AppendOnlyEventStore(Path(tempfile.mkdtemp()) / "selftest.jsonl")
            kernel = v.GoalKernel(store, goal_id="g-self")
            await kernel.add_goal("selftest", budget_usd=1.0)
            await kernel.update_todo("t1", title="写实现", status="done")
            await kernel.update_todo("t2", title="写测试")
            # 跨实例重建 (跨天恢复的根基)
            fresh = v.GoalKernel(store, goal_id="g-self").rebuild()
            assert fresh.goal.todos["t1"].status == "done"
            assert fresh.next_action().id == "t2"
            assert fresh.check_integrity().ok
            # 配额记账写入同一事件流
            quota = v.QuotaTracker(budget_usd=1.0, goal_id="g-self", store=store)
            await quota.record_usage(0.3)
            assert store.verify().ok

        asyncio.run(run())

    check("long_task:event_sourced_state", long_task_chain)

    failed = [c for c in checks if not c["ok"]]
    _json_out(
        {
            "ok": not failed,
            "version": __version__,
            "total": len(checks),
            "failed": len(failed),
            "checks": checks,
        },
        ok=not failed,
    )


def _load_json_flag(args: argparse.Namespace) -> dict[str, Any]:
    if not args.json:
        raise ValueError("--json 输入必填 (可省略内部字段, 用默认值)")
    data = json.loads(args.json)
    if not isinstance(data, dict):
        raise ValueError("--json 必须是对象")
    return data


def _maybe_build_store(data: dict[str, Any]) -> Any | None:
    """从 --json 的 "graph" 字段构建 CausalGraphStore, 注入 plan/diagnose。

    格式: {"graph": {"nodes": [{"name": "db", "p_fail": 0.2}, "api", ...],
                      "edges": [["db", "api"], ["api", "task_outcome"]]}, ...}
    节点可为字符串 (名字) 或对象 (name + p_fail/cond_fail/... 属性)。
    无 graph 字段 → 返回 None (调用方走空图兜底或运行时全局图)。
    """
    graph = data.pop("graph", None)
    if not graph:
        return None
    if not isinstance(graph, dict):
        raise ValueError('"graph" 必须是对象 {nodes, edges}')
    import veya_loop as v

    store = v.CausalGraphStore()
    for node in graph.get("nodes", []):
        if isinstance(node, str):
            store.add_node(node)
        elif isinstance(node, dict) and node.get("name"):
            attrs = {k: val for k, val in node.items() if k != "name"}
            store.add_node(node["name"], **attrs)
        else:
            raise ValueError(f"节点格式非法: {node!r} (应为名字或 {{name, ...}})")
    for edge in graph.get("edges", []):
        if not (isinstance(edge, (list, tuple)) and len(edge) == 2):
            raise ValueError(f"边格式非法: {edge!r} (应为 [parent, child])")
        store.add_edge(edge[0], edge[1])
    return store


def cmd_plan(args: argparse.Namespace) -> None:
    """多步规划薄封装: veya-loop plan --json '{"failure_log": "...", "graph": {...}}'"""
    import veya_loop as v

    try:
        data = _load_json_flag(args)
        store = _maybe_build_store(data)
        if store is not None:
            data.setdefault("store", store)
        report = v.multi_step_plan(**data)
        _json_out({"ok": True, "report": report})
    except Exception as exc:
        _json_out({"ok": False, "error": str(exc)}, ok=False)


def cmd_diagnose(args: argparse.Namespace) -> None:
    """因果故障诊断薄封装: veya-loop diagnose --json '{"failure_log": "...", "graph": {...}}'"""
    import veya_loop as v

    try:
        data = _load_json_flag(args)
        store = _maybe_build_store(data)
        if store is not None:
            data.setdefault("store", store)
        report = v.causal_fault_diagnose(**data)
        _json_out({"ok": True, "report": report})
    except Exception as exc:
        _json_out({"ok": False, "error": str(exc)}, ok=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="veya-loop", description="Veya Loop Engine — 因果闭环控制基板")
    p.add_argument("--version", action="store_true", help="打印版本后退出")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("selftest", help="冒烟: 装配面 + 关键机制链路")
    sp = sub.add_parser("plan", help="多步规划 (multi_step_plan 薄封装)")
    sp.add_argument(
        "--json", help='multi_step_plan 输入 (JSON 对象; 可含 "graph": {nodes, edges} 构建因果图)'
    )
    sd = sub.add_parser("diagnose", help="因果故障诊断薄封装")
    sd.add_argument(
        "--json", help='causal_fault_diagnose 输入 (JSON 对象; 可含 "graph": {nodes, edges})'
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.version:
        cmd_version(args)
        return
    if args.cmd == "selftest":
        cmd_selftest(args)
    elif args.cmd == "plan":
        cmd_plan(args)
    elif args.cmd == "diagnose":
        cmd_diagnose(args)
    else:
        build_parser().print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

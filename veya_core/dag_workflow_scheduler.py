#!/usr/bin/env python3
"""Veya: 引用 3O 元素执行多 Agent DAG 拓扑与资源调度。

构建 A(数据获取) → B(推理分析) → C(结果发布) 的有向无环工作流。
利用:
  - 3O O1 元素 (约束校验) 验证拓扑合法性
  - 3O O2 元素 (匈牙利分配 + 死锁检测) 完成资源的全局最优调度

3O 元素依赖:
  - obase.causal_graph_store        (O1 — DAG 图存储与校验)
  - obase.wfg_deadlock_detector     (O2 — 等待图死锁检测)
  - oprim._scipy_linear_assign      (O2 — 匈牙利算法线性分配)
  - omodul.deterministic_operate_assign  (O2 事务 — 确定性操作分配)

若主库未安装，自动降级到 networkx + scipy 模拟执行。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# ── 环境映射 ──────────────────────────────────────────────────────────
_HICODE = Path(__file__).resolve().parent.parent
_PLATFORM = _HICODE / "platform" / "3O"

for _p in [
    str(_PLATFORM / "obase"),
    str(_PLATFORM / "obase" / "obase"),
    str(_PLATFORM / "omodul"),
    str(_PLATFORM / "omodul" / "omodul"),
    str(_PLATFORM / "oprim"),
    str(_PLATFORM / "oprim" / "oprim"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── 3O 元素加载 (容错降级) ──────────────────────────────────────────

_CausalGraphStore = None
_WaitForGraph = None
_scipy_linear_assign = None
_deterministic_operate_assign = None
_FALLBACK = False

try:
    from obase.causal_graph_store import CausalGraphStore
    _CausalGraphStore = CausalGraphStore
except ImportError:
    pass

try:
    from obase.wfg_deadlock_detector import WaitForGraph
    _WaitForGraph = WaitForGraph
except ImportError:
    pass

try:
    from oprim._scipy_linear_assign import _scipy_linear_assign
except ImportError:
    pass

try:
    from omodul.deterministic_operate_assign import deterministic_operate_assign
except ImportError:
    pass

if not all([_CausalGraphStore, _WaitForGraph, _scipy_linear_assign, _deterministic_operate_assign]):
    _FALLBACK = True


def _fallback_dag_workflow() -> Dict[str, Any]:
    """降级方案: 使用 networkx + scipy 实现 DAG 校验 + 匈牙利分配。

    模拟 3O O1(DAG校验) + O2(死锁检测 + 线性分配) 的完整行为。
    """
    # ── O1: DAG 拓扑校验 ──────────────────
    try:
        import networkx as nx
        G = nx.DiGraph()
        G.add_edge("Agent_A_Ingest", "Agent_B_Compute")
        G.add_edge("Agent_B_Compute", "Agent_C_Publish")

        if not nx.is_directed_acyclic_graph(G):
            raise ValueError("业务工作流中存在环形死锁依赖 — O1 校验失败")

        # 获取拓扑排序
        topo_order = list(nx.topological_sort(G))
        print(f"[veya_core] O1 DAG 拓扑排序: {' → '.join(topo_order)}")

    except ImportError:
        print("[veya_core] networkx 未安装，跳过 O1 DAG 校验")
        # 简单校验：没有反向边
        edges = [
            ("Agent_A_Ingest", "Agent_B_Compute"),
            ("Agent_B_Compute", "Agent_C_Publish"),
        ]
        graph = {}
        for s, t in edges:
            graph.setdefault(s, []).append(t)
        # 检测简单环
        visited = set()
        rec_stack = set()

        def has_cycle(node):
            visited.add(node)
            rec_stack.add(node)
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    if has_cycle(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            rec_stack.discard(node)
            return False

        for node in graph:
            if node not in visited:
                if has_cycle(node):
                    raise ValueError("环形死锁依赖 — O1 校验失败")

        topo_order = ["Agent_A_Ingest", "Agent_B_Compute", "Agent_C_Publish"]

    # ── O2: 死锁等待图注册 ────────────────
    wfg: Dict[str, set] = {}
    wfg.setdefault("Agent_B_Compute", set()).add("Agent_A_Ingest")
    wfg.setdefault("Agent_C_Publish", set()).add("Agent_B_Compute")
    # 死锁检测：等待图中无环
    wfg_visited = set()
    wfg_rec = set()

    def wfg_has_cycle(node):
        wfg_visited.add(node)
        wfg_rec.add(node)
        for neighbor in wfg.get(node, set()):
            if neighbor not in wfg_visited:
                if wfg_has_cycle(neighbor):
                    return True
            elif neighbor in wfg_rec:
                return True
        wfg_rec.discard(node)
        return False

    for node in wfg:
        if node not in wfg_visited:
            if wfg_has_cycle(node):
                raise ValueError("等待图存在死锁 — O2 熔断")

    # ── O2: 匈牙利算法资源分配 ────────────
    workers = ["Worker_GPU_1", "Worker_GPU_2", "Worker_CPU_1"]
    tasks = ["Agent_A_Ingest", "Agent_B_Compute", "Agent_C_Publish"]
    cost_matrix = [
        [0.8, 0.1, 0.9],
        [0.7, 0.2, 0.8],
        [0.2, 0.9, 0.1],
    ]

    try:
        from scipy.optimize import linear_sum_assignment
        import numpy as np
        cost = np.array(cost_matrix)
        row_ind, col_ind = linear_sum_assignment(cost)
        assignments = [(workers[r], tasks[c]) for r, c in zip(row_ind, col_ind)]
        total_cost = cost[row_ind, col_ind].sum()
    except ImportError:
        # 暴力最优分配 (n=3 规模小)
        import itertools
        best_cost = float("inf")
        best_perm = None
        for perm in itertools.permutations(range(len(workers))):
            c = sum(cost_matrix[i][perm[i]] for i in range(len(workers)))
            if c < best_cost:
                best_cost = c
                best_perm = perm
        assignments = [(workers[i], tasks[best_perm[i]]) for i in range(len(workers))]
        total_cost = best_cost

    return {
        "status": "completed",
        "method": "fallback_networkx_scipy",
        "topological_order": topo_order,
        "assignments": assignments,
        "total_cost": float(total_cost),
        "dag_nodes": 3,
        "dag_edges": 2,
    }


def run_dag_workflow() -> Dict[str, Any]:
    """
    业务逻辑：构建 A -> B -> C 工作流，并为节点分配 worker 资源。
    """
    if _FALLBACK:
        print("[veya_core] 3O O1/O2 元素未完整挂载，使用 networkx+scipy 降级执行")
        return _fallback_dag_workflow()

    # ── 3O 主路径 ────────────────────────────────────────────────────

    # 1. 业务拓扑定义 (O1 — 约束校验)
    dag_store = _CausalGraphStore()
    dag_store.add_edge("Agent_A_Ingest", "Agent_B_Compute")
    dag_store.add_edge("Agent_B_Compute", "Agent_C_Publish")

    if not dag_store.is_dag():
        raise ValueError("业务工作流中存在环形死锁依赖。")

    # 2. 资源可用性与分配代价定义
    workers = ["Worker_GPU_1", "Worker_GPU_2", "Worker_CPU_1"]
    tasks = ["Agent_A_Ingest", "Agent_B_Compute", "Agent_C_Publish"]
    cost_matrix = [
        [0.8, 0.1, 0.9],  # Worker_GPU_1
        [0.7, 0.2, 0.8],  # Worker_GPU_2
        [0.2, 0.9, 0.1],  # Worker_CPU_1
    ]

    # 3. 组合 3O O2 元素进行确定性分配
    wfg = _WaitForGraph()
    wfg.add_wait("Agent_B_Compute", "Agent_A_Ingest")

    config = {
        "assign_op": _scipy_linear_assign,
        "wfg_instance": wfg,
    }

    input_data = {
        "workers": workers,
        "tasks": tasks,
        "cost_matrix": cost_matrix,
    }

    # 触发 3O 的全局最优分配事务
    allocation_result = _deterministic_operate_assign(
        config=config,
        input_data=input_data,
        output_dir="/tmp/veya_runs/",
    )

    return allocation_result


if __name__ == "__main__":
    res = run_dag_workflow()
    print(f"DAG Allocation Result: {res.get('status', 'unknown')}")

    assignments = (
        res.get("assignments")
        or res.get("pillars", {}).get("report", {}).get("assignments")
        or []
    )
    print(f"Matches: {assignments}")

    if res.get("topological_order"):
        print(f"Topology: {res['topological_order']}")
    if res.get("total_cost") is not None:
        print(f"Total Cost: {res['total_cost']}")

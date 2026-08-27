"""O1 神经符号引擎测试 — 真 z3 求解 + 确定性断言。

断言具体数值: ok.json → cpu_api=8 / cpu_worker=16 / cpu_db=12 / objective=36,
unsat.json → MUS 精确等于真矛盾核心。哪条挂了就是 z3 API 假设对不上。
"""

from __future__ import annotations

import json

import pytest

from server.neuro_symbolic import VeyaNeuroSymbolic

z3 = pytest.importorskip("z3")

from veya.platform import oprim as load_oprim  # noqa: E402

oprim = load_oprim()

OK_IR = {
    "version": "o1.ir/v1",
    "intent": "为订单服务的三个组件分配 CPU 资源池",
    "vars": [
        {
            "name": "cpu_api",
            "type": "int",
            "desc": "API 服务的 CPU 核数",
            "unit": "核",
            "lo": 0,
            "hi": 64,
        },
        {
            "name": "cpu_worker",
            "type": "int",
            "desc": "Worker 服务的 CPU 核数",
            "unit": "核",
            "lo": 0,
            "hi": 64,
        },
        {
            "name": "cpu_db",
            "type": "int",
            "desc": "数据库的 CPU 核数",
            "unit": "核",
            "lo": 0,
            "hi": 64,
        },
        {"name": "replicas", "type": "int", "desc": "副本数", "unit": "个", "lo": 1, "hi": 10},
        {"name": "ha_enabled", "type": "bool", "desc": "是否开启高可用"},
    ],
    "constraints": [
        {
            "id": "c_total",
            "kind": "hard",
            "origin": "user_msg#1",
            "intent": "整个集群的 CPU 总量不超过 64 核",
            "expr": {
                "op": "<=",
                "args": [
                    {
                        "op": "sum",
                        "args": [{"var": "cpu_api"}, {"var": "cpu_worker"}, {"var": "cpu_db"}],
                    },
                    {"lit": 64},
                ],
            },
        },
        {
            "id": "c_api_min",
            "kind": "hard",
            "origin": "user_msg#1",
            "intent": "API 服务至少分配 8 核",
            "expr": {"op": ">=", "args": [{"var": "cpu_api"}, {"lit": 8}]},
        },
        {
            "id": "c_db_min",
            "kind": "hard",
            "origin": "policy.yaml#L12",
            "intent": "数据库至少分配 12 核",
            "expr": {"op": ">=", "args": [{"var": "cpu_db"}, {"lit": 12}]},
        },
        {
            "id": "c_worker_ratio",
            "kind": "hard",
            "origin": "user_msg#2",
            "intent": "Worker 的核数至少是 API 的 2 倍",
            "expr": {
                "op": ">=",
                "args": [
                    {"var": "cpu_worker"},
                    {"op": "*", "args": [{"lit": 2}, {"var": "cpu_api"}]},
                ],
            },
        },
        {
            "id": "c_ha_replicas",
            "kind": "hard",
            "protected": True,
            "origin": "policy.yaml#L30",
            "intent": "若开启高可用, 副本数不得少于 3 个",
            "expr": {
                "op": "implies",
                "args": [
                    {"var": "ha_enabled"},
                    {"op": ">=", "args": [{"var": "replicas"}, {"lit": 3}]},
                ],
            },
        },
        {
            "id": "p_worker_headroom",
            "kind": "soft",
            "weight": 5,
            "origin": "user_msg#3",
            "intent": "尽量让 Worker 拿到 24 核以上的余量",
            "expr": {"op": ">=", "args": [{"var": "cpu_worker"}, {"lit": 24}]},
        },
    ],
    "objective": {
        "sense": "min",
        "intent": "总成本最小",
        "expr": {
            "op": "sum",
            "args": [{"var": "cpu_api"}, {"var": "cpu_worker"}, {"var": "cpu_db"}],
        },
    },
}

UNSAT_IR = {
    "version": "o1.ir/v1",
    "intent": "在 32 核预算内分配三个组件(需求本身冲突)",
    "vars": [
        {
            "name": "cpu_api",
            "type": "int",
            "desc": "API 服务的 CPU 核数",
            "unit": "核",
            "lo": 0,
            "hi": 64,
        },
        {
            "name": "cpu_worker",
            "type": "int",
            "desc": "Worker 服务的 CPU 核数",
            "unit": "核",
            "lo": 0,
            "hi": 64,
        },
        {
            "name": "cpu_db",
            "type": "int",
            "desc": "数据库的 CPU 核数",
            "unit": "核",
            "lo": 0,
            "hi": 64,
        },
        {"name": "replicas", "type": "int", "desc": "副本数", "unit": "个", "lo": 1, "hi": 10},
        {"name": "ha_enabled", "type": "bool", "desc": "是否开启高可用"},
    ],
    "constraints": [
        {
            "id": "c_total",
            "kind": "hard",
            "origin": "user_msg#1",
            "intent": "整个集群的 CPU 总量不超过 32 核",
            "expr": {
                "op": "<=",
                "args": [
                    {
                        "op": "sum",
                        "args": [{"var": "cpu_api"}, {"var": "cpu_worker"}, {"var": "cpu_db"}],
                    },
                    {"lit": 32},
                ],
            },
        },
        {
            "id": "c_api_min",
            "kind": "hard",
            "origin": "user_msg#2",
            "intent": "API 服务至少分配 16 核",
            "expr": {"op": ">=", "args": [{"var": "cpu_api"}, {"lit": 16}]},
        },
        {
            "id": "c_worker_min",
            "kind": "hard",
            "origin": "user_msg#2",
            "intent": "Worker 服务至少分配 16 核",
            "expr": {"op": ">=", "args": [{"var": "cpu_worker"}, {"lit": 16}]},
        },
        {
            "id": "c_db_min",
            "kind": "hard",
            "origin": "policy.yaml#L12",
            "intent": "数据库至少分配 8 核",
            "expr": {"op": ">=", "args": [{"var": "cpu_db"}, {"lit": 8}]},
        },
    ],
}


# =========================================================================
# 一、闸门 1: 校验器 (纯, 无求解器)
# =========================================================================


def test_validator_catches_schema_errors():
    bad = {
        "vars": [
            {"name": "x", "type": "int", "lo": 0, "hi": 10},
            {"name": "y", "type": "int", "lo": 5, "hi": 3},
        ],  # 空定义域
        "constraints": [
            {
                "id": "c1",
                "kind": "hard",
                "intent": "x 乘 y 不超过 20",
                "expr": {
                    "op": "<=",
                    "args": [{"op": "*", "args": [{"var": "x"}, {"var": "y"}]}, {"lit": 20}],
                },
            },
            {
                "id": "c2",
                "kind": "hard",
                "intent": "x 的平方根小于 3",
                "expr": {"op": "sqrt", "args": [{"var": "x"}]},
            },
            {
                "id": "c3",
                "kind": "hard",
                "intent": "z 至少为 1",
                "expr": {"op": ">=", "args": [{"var": "z"}, {"lit": 1}]},
            },
            {
                "id": "c4",
                "kind": "hard",
                "intent": "",
                "expr": {"op": "<=", "args": [{"var": "x"}, {"lit": 9}]},
            },
            {
                "id": "c5",
                "kind": "hard",
                "intent": "x 除以 3 不超过 2",
                "expr": {
                    "op": "<=",
                    "args": [{"op": "/", "args": [{"var": "x"}, {"lit": 3}]}, {"lit": 2}],
                },
            },
        ],
    }
    r = VeyaNeuroSymbolic.plan  # noqa: F841 - 走 oprim 原子层直接验证
    errs = oprim.validate(oprim.parse_ir(bad))
    codes = {e.code for e in errs}
    assert {
        "E_NONLINEAR",
        "E_UNKNOWN_OP",
        "E_UNDEF_VAR",
        "E_NO_INTENT",
        "E_INT_DIV",
        "E_DOMAIN",
    } <= codes
    assert all(e.hint for e in errs)  # hint 可回灌 LLM


# =========================================================================
# 二、MUS 收缩 (mock oracle, 与 z3 解耦)
# =========================================================================


def test_mus_shrink_converges_and_deterministic():
    true_mus = {"A", "B", "C"}

    def oracle(subset):
        return "unsat" if true_mus <= set(subset) else "sat"

    r = oprim.shrink_to_mus(oracle, ["A", "B", "C", "D", "E", "F"])
    assert r.mus == ["A", "B", "C"]
    assert set(r.dropped) == {"D", "E", "F"}
    assert r.verified is True
    assert r.checks == 6

    # 输入顺序不影响结果 (确定性)
    r2 = oprim.shrink_to_mus(oracle, ["F", "A", "E", "B", "D", "C"])
    assert r2.mus == r.mus


def test_mus_unknown_conservative_and_budget():
    true_mus = {"A", "B", "C"}

    def flaky(subset):
        if "D" in subset and len(subset) == 3:
            return "unknown"
        return "unsat" if true_mus <= set(subset) else "sat"

    r = oprim.shrink_to_mus(flaky, ["A", "B", "C", "D"])
    assert r.verified is False  # unknown 必须取消 verified
    # unknown 时保守处理: 要么保留该约束, 要么明确记录剔除但不再可信
    assert r.notes and "unknown" in r.notes[0]

    def oracle(subset):
        return "unsat" if true_mus <= set(subset) else "sat"

    r2 = oprim.shrink_to_mus(oracle, ["A", "B", "C", "D", "E", "F"], max_checks=2)
    assert r2.checks <= 2 and r2.verified is False


# =========================================================================
# 三、闸门 2: 回译 diff (抓翻译幻觉)
# =========================================================================


def test_backtranslate_catches_drift_and_direction():
    drift = {
        "vars": [
            {
                "name": "cpu_api",
                "type": "int",
                "desc": "API 服务的 CPU 核数",
                "unit": "核",
                "lo": 0,
                "hi": 64,
            },
            {
                "name": "cpu_worker",
                "type": "int",
                "desc": "Worker 服务的 CPU 核数",
                "unit": "核",
                "lo": 0,
                "hi": 64,
            },
        ],
        "constraints": [
            {
                "id": "c_total",
                "kind": "hard",
                "intent": "整个集群的 CPU 总量不超过 64 核",
                "expr": {
                    "op": "<=",
                    "args": [
                        {"op": "+", "args": [{"var": "cpu_api"}, {"var": "cpu_worker"}]},
                        {"lit": 46},
                    ],
                },
            },  # 64 → 46 常量漂移
            {
                "id": "c_api_min",
                "kind": "hard",
                "intent": "API 服务至少分配 8 核",
                "expr": {"op": "<=", "args": [{"var": "cpu_api"}, {"lit": 8}]},
            },  # 至少 → <= 方向翻转
        ],
    }
    r = VeyaNeuroSymbolic.plan(drift)
    assert r["ok"] is False
    assert r["stage"] == "backtranslate"  # 进求解器之前就被拦下
    codes = {f["code"] for d in r["diffs"] for f in d["findings"] if f["severity"] == "FAIL"}
    assert {"NUM_DRIFT", "DIR_FLIP"} <= codes


def test_backtranslate_clean_ir_not_blocked():
    r = VeyaNeuroSymbolic.plan(OK_IR)
    assert r["ok"] is True
    assert all(not d["blocked"] for d in r["diffs"])


# =========================================================================
# 四、真 z3: 求解语义 + 期望数值
# =========================================================================


def test_ok_ir_optimal_solution_exact_numbers():
    r = VeyaNeuroSymbolic.plan(OK_IR)
    assert r["ok"] is True
    assert r["stage"] == "done"
    s = r["solution"]
    assert s["assignment"]["cpu_api"] == 8
    assert s["assignment"]["cpu_worker"] == 16
    assert s["assignment"]["cpu_db"] == 12
    assert s["objective_value"] == "36"
    # 软约束 worker>=24 与 min(total) 冲突: lex 下目标优先, 偏好被牺牲
    assert s["relaxed_soft"] == ["p_worker_headroom"]
    # 字典序打桩: bool 压到 False, replicas 压到下界 1
    assert s["assignment"]["ha_enabled"] is False
    assert s["assignment"]["replicas"] == 1


def test_unsat_ir_exact_mus():
    r = VeyaNeuroSymbolic.plan(UNSAT_IR)
    assert r["ok"] is False
    assert r["stage"] == "feasibility"
    assert r["feasibility"]["status"] == "unsat"
    mus = set(r["feasibility"]["mus"]["ids"])
    assert mus == {"c_total", "c_api_min", "c_worker_min", "c_db_min"}
    assert r["feasibility"]["mus"]["verified"] is True
    # RepairPayload 喂回的是自然语言意图, 不是裸 id
    items = r["repair"]["items"]
    assert all(it["intent"] for it in items)
    assert any(it["origin"] for it in items)


def test_plan_id_and_solution_deterministic():
    runs = [VeyaNeuroSymbolic.plan(OK_IR) for _ in range(3)]
    assert len({r["plan_id"] for r in runs}) == 1
    assert len({json.dumps(r["solution"]["assignment"], sort_keys=True) for r in runs}) == 1
    # 换 seed → plan_id 变 (内容寻址包含 seed)
    assert runs[0]["plan_id"] != VeyaNeuroSymbolic.plan(OK_IR, seed=1)["plan_id"]

"""达尔文算子自进化闭环测试 — 注入 fake 回测/通知, 全确定性."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from server.darwin_evolution import VeyaDarwinEvolution

OPERATOR = (
    'def run_strategy(df, alpha=0.3, window=20):\n'
    '    df["ma"] = df["close"].rolling(window).mean()\n'
    '    df["signal"] = alpha * (df["close"] / df["ma"] - 1)\n'
    '    df["daily_return"] = df["signal"].shift(1).fillna(0) * df["close"].pct_change().fillna(0)\n'
    '    df["cum_return"] = (1 + df["daily_return"]).cumprod()\n'
    '    return df\n'
)


class FakeBacktest:
    """夏普 = alpha × 3 → 变种 v2 (×1.25) 必然胜出, 结果可断言."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, code: str, params: dict, asset_id: str) -> dict:
        self.calls.append(code)
        m = re.search(r"alpha\s*=\s*([0-9.]+)", code)
        alpha = float(m.group(1)) if m else 0.3
        return {"sharpe": round(alpha * 3, 4), "total_return": alpha, "error": None}


@pytest.fixture
def evolution(tmp_path) -> tuple[VeyaDarwinEvolution, FakeBacktest]:
    fb = FakeBacktest()
    evo = VeyaDarwinEvolution(
        state_dir=str(tmp_path / "darwin"),
        backtest_fn=fb,
        variant_fn=None,  # 确定性 AST 突变路径
        notify_fn=None,
        shadow_min_samples=3,
        decay_accuracy_below=0.6,
        decay_slippage_above=0.02,
    )
    return evo, fb


# =========================================================================
# 注册 + 影子测试 + 衰减检测
# =========================================================================

async def test_register_and_shadow_decay(evolution):
    evo, _ = evolution
    op_id = evo.register_operator(OPERATOR, "alpha_momentum")
    ops = evo.list_operators()
    assert len(ops) == 1
    assert ops[0]["status"] == "ACTIVE"
    assert ops[0]["code_len"] > 0

    # 样本不足 → 不判衰减
    r = evo.record_shadow(op_id, slippage=0.001, accuracy=0.7)
    assert r["decayed"] is False

    # 达标 + 准确率跌破阈值 → 衰减
    evo.record_shadow(op_id, slippage=0.001, accuracy=0.55)
    r = evo.record_shadow(op_id, slippage=0.001, accuracy=0.5)
    assert r["decayed"] is True
    assert r["accuracy_avg"] == pytest.approx(0.5833, abs=0.01)


# =========================================================================
# 进化闭环: 突变 → 并发回测 → 择优 → PRD
# =========================================================================

async def test_evolve_cycle_selects_best_variant(evolution):
    evo, fb = evolution
    op_id = evo.register_operator(OPERATOR, "alpha_momentum")
    for acc in (0.7, 0.65, 0.5):
        evo.record_shadow(op_id, slippage=0.001, accuracy=acc)

    r = await evo.evolve(op_id)

    assert r["status"] == "candidate_ready"
    assert len(r["results"]) == 3
    assert len(fb.calls) == 3  # 3 变种并发回测

    sharpe = [x["sharpe"] for x in r["results"]]
    assert sharpe == [0.72, 1.125, 0.99]
    assert r["winner"]["index"] == 1  # v2 (alpha×1.25) 胜出
    assert r["winner"]["sharpe"] == 1.125

    # PRD 落盘 + 候选状态
    assert Path(r["prd_path"]).exists()
    op = evo.get_operator(op_id)
    assert op["status"] == "CANDIDATE"
    prd = evo.get_prd(op_id)
    assert "达尔文升级申请" in prd
    assert "1.125" in prd


async def test_evolve_uses_deterministic_mutation(evolution):
    """无 Genesis → 引擎内 AST 突变: 3 变种均为合法 Python 且互不相同."""
    evo, _ = evolution
    op_id = evo.register_operator(OPERATOR, "alpha_momentum")
    for acc in (0.7, 0.65, 0.5):
        evo.record_shadow(op_id, slippage=0.001, accuracy=acc)

    r = await evo.evolve(op_id)
    variant_codes = [x["code"] for x in r["results"]]
    assert len(set(variant_codes)) == 3
    for v in variant_codes:
        ast.parse(v)  # 合法 Python
    # v3 含非线性惩罚项
    assert "abs(" in variant_codes[2]


async def test_promote_replaces_operator_with_lineage(evolution):
    evo, _ = evolution
    op_id = evo.register_operator(OPERATOR, "alpha_momentum")
    for acc in (0.7, 0.65, 0.5):
        evo.record_shadow(op_id, slippage=0.001, accuracy=acc)
    await evo.evolve(op_id)

    r = evo.promote(op_id)
    assert r["status"] == "promoted"
    assert r["lineage_depth"] == 1
    op = evo.get_operator(op_id)
    assert op["status"] == "ACTIVE"
    assert len(op["lineage"]) == 1
    # 候选已清空 → PRD 不再可用
    assert evo.get_prd(op_id) is None


async def test_promote_without_candidate_raises(evolution):
    evo, _ = evolution
    op_id = evo.register_operator(OPERATOR, "alpha_momentum")
    with pytest.raises(ValueError, match="no pending candidate"):
        evo.promote(op_id)


# =========================================================================
# 通知注入 + 持久化恢复
# =========================================================================

async def test_evolve_notifies_prd_review(tmp_path):
    notified: list[dict] = []
    fb = FakeBacktest()
    evo = VeyaDarwinEvolution(
        state_dir=str(tmp_path / "darwin"),
        backtest_fn=fb,
        variant_fn=None,
        notify_fn=notified.append,
        shadow_min_samples=3,
        decay_accuracy_below=0.6,
    )
    op_id = evo.register_operator(OPERATOR, "alpha_momentum")
    for acc in (0.7, 0.65, 0.5):
        evo.record_shadow(op_id, slippage=0.001, accuracy=acc)
    await evo.evolve(op_id)

    assert len(notified) == 1
    msg = notified[0]
    assert msg["type"] == "PRD_REVIEW_REQUIRED"
    assert msg["operator_id"] == op_id
    assert msg["new_sharpe"] == 1.125
    assert "/promote" in msg["approve_endpoint"]


async def test_state_persists_across_restart(tmp_path):
    state_dir = tmp_path / "darwin"
    fb = FakeBacktest()
    evo1 = VeyaDarwinEvolution(state_dir=str(state_dir), backtest_fn=fb, variant_fn=None)
    op_id = evo1.register_operator(OPERATOR, "alpha_momentum")
    evo1.record_shadow(op_id, slippage=0.001, accuracy=0.8)

    # 模拟服务重启: 新实例同一 state_dir
    evo2 = VeyaDarwinEvolution(state_dir=str(state_dir), backtest_fn=fb, variant_fn=None)
    ops = evo2.list_operators()
    assert len(ops) == 1
    assert ops[0]["id"] == op_id
    assert ops[0]["shadow"]["samples"] == 1


async def test_health_report(evolution):
    evo, _ = evolution
    h = evo.health()
    assert h["status"] == "healthy"
    assert h["details"]["operators"] >= 0

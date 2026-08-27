"""optimize_loop 行为测试矩阵 — 效用数值 / 硬门禁 / 指纹缓存 / BO 寻峰 / OOS 采纳。

规格 (Helivex 适配协议):
  - 搜索只在 train_window 最大化多目标效用;
  - 采纳看 gate_window (OOS) + RiskGateConfig;
  - 缓存键 = fingerprint(params, window, meta), 同一键同一结果。
"""

from __future__ import annotations

import pytest

from veya_loop import (
    DEFAULT_UTILITY_WEIGHTS,
    EvalCache,
    EvalWindow,
    RiskGateConfig,
    fingerprint_eval,
    multi_objective_utility,
    optimize_loop,
)

TRAIN = EvalWindow("2020-01-01", "2022-12-31", "train")
TEST = EvalWindow("2023-01-01", "2023-12-31", "test")


# =========================================================================
# 1. 多目标效用: 数值精确 + 权重覆盖 + 缺指标按 0
# =========================================================================


def test_utility_default_weights_numeric():
    metrics = {
        "sharpe": 2.0,
        "total_return": 0.3,
        "max_drawdown": 0.1,
        "turnover": 2.0,
        "cost_drag": 0.2,
    }
    # U = 1.0·2.0 + 0.25·0.3 − 1.0·0.1 − 0.05·2.0 − 0.5·0.2
    assert multi_objective_utility(metrics) == pytest.approx(1.775)


def test_utility_custom_weights_override():
    metrics = {"sharpe": 2.0, "max_drawdown": 0.1}
    # 默认 + 覆盖: sharpe 2.0, max_drawdown −2.0
    u = multi_objective_utility(metrics, weights={"sharpe": 2.0, "max_drawdown": -2.0})
    assert u == pytest.approx(2.0 * 2.0 + (-2.0) * 0.1)  # = 3.8


def test_multi_objective_config_utility_method():
    from veya_loop import MultiObjectiveConfig

    cfg = MultiObjectiveConfig(weights={"sharpe": 1.0, "max_drawdown": -1.5})
    metrics = {"sharpe": 2.0, "max_drawdown": 0.1, "turnover": 0.5}
    assert cfg.utility(metrics) == pytest.approx(
        multi_objective_utility(metrics, weights=cfg.weights)
    )


def test_utility_missing_metrics_zero():
    assert multi_objective_utility({"sharpe": 1.0}) == pytest.approx(1.0)
    assert multi_objective_utility({}) == pytest.approx(0.0)
    assert DEFAULT_UTILITY_WEIGHTS["max_drawdown"] == -1.0  # 风险惩罚默认


# =========================================================================
# 2. 指纹: 稳定 / 键序无关 / 区间与 meta 敏感
# =========================================================================


def test_fingerprint_stable_and_order_independent():
    a = fingerprint_eval({"x": 1, "y": 2}, TRAIN)
    b = fingerprint_eval({"y": 2, "x": 1}, TRAIN)  # 键序无关
    assert a == b
    assert len(a) == 16  # sha256 前 16 位


def test_fingerprint_sensitive_to_window_and_meta():
    f1 = fingerprint_eval({"x": 1}, TRAIN)
    assert f1 != fingerprint_eval({"x": 1}, TEST)
    assert f1 != fingerprint_eval({"x": 1}, TRAIN, meta={"seed": 1})
    assert fingerprint_eval({"x": 1}, TRAIN, meta={"seed": 1}) == fingerprint_eval(
        {"x": 1}, TRAIN, meta={"seed": 1}
    )


# =========================================================================
# 3. 缓存: hit 跳过 evaluate / 统计 / 磁盘持久化
# =========================================================================


def test_cache_hit_skips_evaluate():
    calls = {"n": 0}

    def evaluate(params, window):
        calls["n"] += 1
        return {"sharpe": 1.0}

    cache = EvalCache()
    fp = fingerprint_eval({"x": 1}, TRAIN)
    assert cache.get(fp) is None
    cache.put(fp, {"sharpe": 1.0})
    assert cache.get(fp) == {"sharpe": 1.0}  # 命中
    assert cache.get(fp) == {"sharpe": 1.0}  # 再命中
    assert cache.stats()["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert calls["n"] == 0  # 全程未调 evaluate


def test_cache_disk_persistence(tmp_path):
    db = tmp_path / "eval_cache.jsonl"
    c1 = EvalCache(disk_path=db)
    c1.put("fp1", {"sharpe": 3.0})
    c2 = EvalCache(disk_path=db)  # 新实例跨进程
    assert c2.get("fp1") == {"sharpe": 3.0}


# =========================================================================
# 4. 风险门禁: 过 / 拒 / 缺指标安全默认
# =========================================================================


def test_risk_gate_pass():
    gate = RiskGateConfig(min_sharpe=0.5, max_drawdown=0.25, min_trades=30)
    ok, reason = gate.evaluate({"sharpe": 1.2, "max_drawdown": 0.15, "n_trades": 50})
    assert ok and reason == "ok"


def test_risk_gate_reject_reasons():
    gate = RiskGateConfig(min_sharpe=0.5, max_drawdown=0.25, min_trades=30)
    ok, reason = gate.evaluate({"sharpe": 0.3, "max_drawdown": 0.4, "n_trades": 10})
    assert not ok
    assert "sharpe" in reason and "max_drawdown" in reason and "n_trades" in reason


def test_risk_gate_missing_metric_fails_closed():
    gate = RiskGateConfig(min_sharpe=0.5)
    ok, reason = gate.evaluate({"max_drawdown": 0.1})  # 缺 sharpe
    assert not ok and "sharpe" in reason  # 缺数据不给过 (安全)


def test_risk_gate_optional_checks_skipped():
    gate = RiskGateConfig(min_sharpe=0.5, max_turnover=None, max_cost_drag=None)
    ok, _ = gate.evaluate({"sharpe": 1.0, "n_trades": 5, "max_drawdown": 0.1})
    assert ok


# =========================================================================
# 5. BO 寻峰: 凸函数上找到峰点
# =========================================================================


def test_optimize_loop_finds_peak():
    """sharpe 峰在 (lookback=30, threshold=0.02), 其余指标常量。"""

    def evaluate(params, window):
        lb, th = params["lookback"], params["threshold"]
        sharpe = 1.0 - ((lb - 30) / 40.0) ** 2 - ((th - 0.02) / 0.05) ** 2
        return {
            "sharpe": sharpe,
            "total_return": 0.1,
            "max_drawdown": 0.1,
            "n_trades": 100,
            "turnover": 1.0,
            "cost_drag": 0.01,
        }

    result = optimize_loop(
        search_space={"lookback": (5, 60), "threshold": (0.0, 0.05)},
        evaluate=evaluate,
        train_window=TRAIN,
        gate_window=TEST,
        risk_gate=RiskGateConfig(min_sharpe=0.0),
        n_init=4,
        n_iter=16,
        seed=0,
    )
    assert result.accepted
    assert result.best_params is not None
    assert abs(result.best_params["lookback"] - 30) < 10
    assert abs(result.best_params["threshold"] - 0.02) < 0.02
    assert result.utility > 0.8  # 峰处效用高


# =========================================================================
# 6. OOS 采纳: train 好 gate 差 → 拒; 放宽门禁 → 接受
# =========================================================================


def _window_aware_evaluate(window_label: str) -> dict[str, float]:
    if window_label == "train":
        return {
            "sharpe": 2.0,
            "total_return": 0.5,
            "max_drawdown": 0.05,
            "n_trades": 100,
            "turnover": 1.0,
            "cost_drag": 0.01,
        }
    return {
        "sharpe": 0.1,
        "total_return": -0.3,
        "max_drawdown": 0.6,
        "n_trades": 5,
        "turnover": 5.0,
        "cost_drag": 0.2,
    }


def test_optimize_loop_gate_rejects_oos():
    """train 指标亮眼 (U 高) 但 OOS 差 → 拒绝, gate_reason 说明。"""

    def evaluate(params, window):
        return _window_aware_evaluate(window.label)

    result = optimize_loop(
        search_space={"lookback": (5, 60)},
        evaluate=evaluate,
        train_window=TRAIN,
        gate_window=TEST,
        risk_gate=RiskGateConfig(min_sharpe=0.5, max_drawdown=0.3, min_trades=30),
        n_init=2,
        n_iter=4,
        seed=0,
    )
    assert result.accepted is False
    assert result.utility > 0.5  # train 效用确实高
    assert "sharpe" in result.gate_reason  # 拒绝原因指向 sharpe
    assert result.gate_window_label == TEST.key()  # 门禁在 OOS 区间


def test_optimize_loop_relaxed_gate_accepts():
    """同一结果, 放宽门禁 → 接受 (拒绝/接受可复现判定)。"""

    def evaluate(params, window):
        return _window_aware_evaluate(window.label)

    kwargs = dict(
        search_space={"lookback": (5, 60)},
        evaluate=evaluate,
        train_window=TRAIN,
        gate_window=TEST,
        n_init=2,
        n_iter=4,
        seed=0,
    )
    strict = optimize_loop(**kwargs, risk_gate=RiskGateConfig(min_sharpe=0.5))
    relaxed = optimize_loop(**kwargs, risk_gate=RiskGateConfig(min_sharpe=0.0))
    assert strict.accepted is False
    assert relaxed.accepted is True
    assert relaxed.best_params == strict.best_params  # 同参数, 仅门禁不同


def test_optimize_loop_gate_on_train():
    """gate_on='train' → 用 train 区间过门禁 (无 OOS 数据时降级)。"""

    def evaluate(params, window):
        return _window_aware_evaluate(window.label)

    result = optimize_loop(
        search_space={"lookback": (5, 60)},
        evaluate=evaluate,
        train_window=TRAIN,
        gate_window=None,
        gate_on="train",
        risk_gate=RiskGateConfig(min_sharpe=1.0),
        n_init=2,
        n_iter=4,
        seed=0,
    )
    assert result.accepted is True  # train sharpe=2.0 ≥ 1.0
    assert result.gate_window_label == TRAIN.key()


# =========================================================================
# 7. 缓存集成: 循环内重复评价命中
# =========================================================================


def test_optimize_loop_cache_integration():
    calls = {"n": 0}

    def evaluate(params, window):
        calls["n"] += 1
        return {
            "sharpe": 1.0 - abs(params["x"] - 0.5),
            "max_drawdown": 0.1,
            "n_trades": 50,
            "total_return": 0.0,
            "turnover": 0.0,
        }

    cache = EvalCache()
    r1 = optimize_loop(
        search_space={"x": (0.0, 1.0)},
        evaluate=evaluate,
        train_window=TRAIN,
        gate_window=TEST,
        risk_gate=RiskGateConfig(),
        cache=cache,
        n_init=3,
        n_iter=6,
        seed=0,
    )
    calls_after_first = calls["n"]
    # 第二次跑: 初始点/迭代点全部命中缓存 → evaluate 调用显著减少
    r2 = optimize_loop(
        search_space={"x": (0.0, 1.0)},
        evaluate=evaluate,
        train_window=TRAIN,
        gate_window=TEST,
        risk_gate=RiskGateConfig(),
        cache=cache,
        n_init=3,
        n_iter=6,
        seed=0,
    )
    assert calls_after_first > 0
    assert calls["n"] < calls_after_first + 3  # 复用生效
    assert cache.stats()["hits"] > 0
    assert r1.best_params == r2.best_params  # 结果可复现

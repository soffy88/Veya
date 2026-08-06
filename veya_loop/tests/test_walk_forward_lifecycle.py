"""walk_forward + 生命周期 + 优化工程化 (ParamSpec/早停/指纹版本) 行为测试。

覆盖: ParamSpec 整数/对数解码、指纹版本打穿缓存、BO 早停、walk_forward
聚合 (accept_rate/OOS 效用/分位数)、生命周期升降级与审计事件。
"""

from __future__ import annotations

import pytest

from veya_loop import (
    EvalCache,
    EvalWindow,
    OptimizeLoopResult,
    ParamSpec,
    RiskGateConfig,
    StrategyLifecycle,
    fingerprint_eval,
    optimize_loop,
    walk_forward,
)

TRAIN = EvalWindow("2020-01-01", "2022-12-31", "train")
TEST = EvalWindow("2023-01-01", "2023-12-31", "test")
TEST2 = EvalWindow("2024-01-01", "2024-12-31", "test2")


# =========================================================================
# 1. ParamSpec: 整数 / 对数 / 校验
# =========================================================================

def test_param_spec_integer_decode():
    spec = ParamSpec(5, 60, kind="integer")
    assert spec.opt_bounds() == (5.0, 60.0)
    assert spec.decode(30.6) == 31.0            # round
    assert spec.decode(30.4) == 30.0


def test_param_spec_log_decode():
    spec = ParamSpec(0.01, 0.2, kind="log")
    lo, hi = spec.opt_bounds()
    assert lo == pytest.approx(-4.6052, abs=1e-3)   # ln(0.01)
    assert hi == pytest.approx(-1.6094, abs=1e-3)   # ln(0.2)
    assert spec.decode(lo) == pytest.approx(0.01)   # exp 还原
    assert spec.decode(hi) == pytest.approx(0.2)


def test_param_spec_validation():
    with pytest.raises(ValueError):
        ParamSpec(5, 5)                            # low == high
    with pytest.raises(ValueError):
        ParamSpec(0.0, 1.0, kind="log")            # log 要求 low > 0
    with pytest.raises(ValueError):
        ParamSpec(0.0, 1.0, kind="bogus")


def test_optimize_loop_integer_log_integration():
    """整数 + 对数参数: 峰在 lookback=30 (整数), thr=0.1 (对数)。"""
    def evaluate(params, window):
        lb, thr = params["lookback"], params["thr"]
        sharpe = 1.0 - ((lb - 30) / 30.0) ** 2 - ((thr - 0.1) / 0.08) ** 2
        return {"sharpe": sharpe, "max_drawdown": 0.1, "n_trades": 50,
                "total_return": 0.1, "turnover": 1.0}

    r = optimize_loop(
        search_space={"lookback": ParamSpec(5, 60, kind="integer"),
                      "thr": ParamSpec(0.01, 0.2, kind="log")},
        evaluate=evaluate, train_window=TRAIN, gate_window=TEST,
        risk_gate=RiskGateConfig(min_sharpe=0.0),
        n_init=4, n_iter=16, seed=0,
    )
    assert r.accepted
    assert r.best_params is not None
    assert r.best_params["lookback"] == round(r.best_params["lookback"])  # 整数
    assert abs(r.best_params["lookback"] - 30) < 10
    assert abs(r.best_params["thr"] - 0.1) < 0.05


# =========================================================================
# 2. 指纹版本: eval_meta 升级自动打穿缓存
# =========================================================================

def test_fingerprint_eval_meta_invalidates_cache():
    cache = EvalCache()
    meta_v1 = {"data_version": "2026q1"}
    meta_v2 = {"data_version": "2026q2"}
    fp1 = fingerprint_eval({"x": 1}, TRAIN, meta_v1)
    fp2 = fingerprint_eval({"x": 1}, TRAIN, meta_v2)
    assert fp1 != fp2, "数据版本升级必须改变指纹 (打穿缓存)"
    cache.put(fp1, {"sharpe": 1.0})
    assert cache.get(fp2) is None, "新版本不得命中旧缓存"


def test_optimize_loop_eval_meta_threaded():
    """eval_meta 透传进 optimize_loop → 指纹含版本 → 两次版本不同结果独立。"""
    calls = {"n": 0}

    def evaluate(params, window):
        calls["n"] += 1
        return {"sharpe": 1.0, "max_drawdown": 0.1, "n_trades": 50}

    cache = EvalCache()
    optimize_loop(search_space={"x": (0.0, 1.0)}, evaluate=evaluate,
                  train_window=TRAIN, gate_window=TEST,
                  risk_gate=RiskGateConfig(), cache=cache,
                  eval_meta={"data_version": "v1"}, n_init=2, n_iter=3, seed=0)
    calls_v1 = calls["n"]
    assert calls_v1 == 6                       # 2 init + 3 BO + 1 gate
    # v2 升级 → 指纹全变 → 全部重评 (打穿缓存)
    optimize_loop(search_space={"x": (0.0, 1.0)}, evaluate=evaluate,
                  train_window=TRAIN, gate_window=TEST,
                  risk_gate=RiskGateConfig(), cache=cache,
                  eval_meta={"data_version": "v2"}, n_init=2, n_iter=3, seed=0)
    calls_v2 = calls["n"]
    assert calls_v2 == calls_v1 + 6            # v2 独立评全量
    # 同版本重跑 → 零新增 (缓存全命中)
    optimize_loop(search_space={"x": (0.0, 1.0)}, evaluate=evaluate,
                  train_window=TRAIN, gate_window=TEST,
                  risk_gate=RiskGateConfig(), cache=cache,
                  eval_meta={"data_version": "v2"}, n_init=2, n_iter=3, seed=0)
    assert calls["n"] == calls_v2


# =========================================================================
# 3. BO 早停: early_stop_rounds / ei_stop 透传 + 标记
# =========================================================================

def _flat_objective(params, window):
    """常数目标 → BO 无改进 → early_stop_rounds 触发。"""
    return {"sharpe": 1.0, "max_drawdown": 0.1, "n_trades": 50,
            "total_return": 0.1, "turnover": 1.0}


def test_early_stop_rounds_marks_result():
    r_full = optimize_loop(search_space={"x": (0.0, 1.0)}, evaluate=_flat_objective,
                           train_window=TRAIN, gate_window=TEST,
                           risk_gate=RiskGateConfig(), n_init=2, n_iter=20, seed=0,
                           early_stop_rounds=3)
    assert r_full.early_stopped is True
    # 不启用早停: 跑满 n_iter (常数目标 BO 重复采样被缓存兜底,
    # n_evals 只计真实 evaluate, 但早停版必须更少)
    r_no = optimize_loop(search_space={"x": (0.0, 1.0)}, evaluate=_flat_objective,
                         train_window=TRAIN, gate_window=TEST,
                         risk_gate=RiskGateConfig(), n_init=2, n_iter=20, seed=0)
    assert r_no.early_stopped is False
    assert r_full.n_evals < r_no.n_evals
    assert r_no.n_evals <= 2 + 20


def test_early_stop_ei_stop():
    from oprim import bayesian_optimize

    def obj(x):
        return (x[0] - 0.5) ** 2

    res = bayesian_optimize(obj, [(0.0, 1.0)], n_init=2, n_iter=20, seed=0,
                            ei_stop=1e-6)
    assert res["early_stopped"] is True
    assert res["rounds_done"] < 20


# =========================================================================
# 4. walk_forward: 每折独立 + 共享缓存 + 聚合
# =========================================================================

def _wf_evaluate(params, window):
    """OOS 好坏的折: label 含 bad 的折差 → 部分折拒绝。"""
    if "bad" in window.label:
        return {"sharpe": 0.1, "max_drawdown": 0.5, "n_trades": 5,
                "total_return": -0.2, "turnover": 3.0}
    return {"sharpe": 1.5, "max_drawdown": 0.1, "n_trades": 60,
            "total_return": 0.3, "turnover": 1.0}


def test_walk_forward_aggregation():
    folds = [(TRAIN, TEST), (TEST, TEST2)]
    # 折 2 的 test 是 TEST2 (label 非 bad → 好); 让折 1 的 TEST 也好
    # 构造: 让一个折的 gate 差 → accept_rate 0.5
    def evaluate(params, window):
        if window.label == "bad":
            return _wf_evaluate(params, window)
        return {"sharpe": 1.5, "max_drawdown": 0.1, "n_trades": 60,
                "total_return": 0.3, "turnover": 1.0}

    wf = walk_forward(
        folds=folds,
        search_space={"lookback": (5, 40)},
        evaluate=evaluate,
        risk_gate=RiskGateConfig(min_sharpe=0.5, max_drawdown=0.3, min_trades=30),
        n_init=2, n_iter=4, seed=0,
    )
    assert wf.accept_rate == 1.0
    assert wf.aggregate_accepted is True
    assert len(wf.folds) == 2
    # OOS 效用聚合: 均值 > 0, std ≥ 0, 分位数单调
    assert wf.oos_utility_mean > 0.0
    assert wf.oos_utility_std >= 0.0
    assert "sharpe" in wf.metric_summaries
    s = wf.metric_summaries["sharpe"]
    assert s["p25"] <= s["p50"] <= s["p75"]
    assert s["mean"] == pytest.approx((1.5 + 1.5) / 2)


def test_walk_forward_rejects_below_rate():
    folds = [(TRAIN, EvalWindow("2023-01-01", "2023-12-31", "bad")),
             (TEST, EvalWindow("2024-01-01", "2024-12-31", "bad2"))]
    wf = walk_forward(
        folds=folds,
        search_space={"lookback": (5, 40)},
        evaluate=_wf_evaluate,
        risk_gate=RiskGateConfig(min_sharpe=0.5),
        min_accept_rate=0.5,
        n_init=2, n_iter=4, seed=0,
    )
    assert wf.accept_rate == 0.0
    assert wf.aggregate_accepted is False
    assert wf.oos_utility_mean < 0.0           # 差折 OOS 效用为负


def test_walk_forward_shared_cache():
    calls = {"n": 0}

    def evaluate(params, window):
        calls["n"] += 1
        return {"sharpe": 1.0, "max_drawdown": 0.1, "n_trades": 50}

    cache = EvalCache()
    wf1 = walk_forward(folds=[(TRAIN, TEST), (TEST, TEST2)], search_space={"x": (0.0, 1.0)},
                       evaluate=evaluate, risk_gate=RiskGateConfig(), cache=cache,
                       n_init=2, n_iter=3, seed=0)
    n1 = calls["n"]
    # 第二次: 指纹 (含 eval_meta 一致) → 全命中
    wf2 = walk_forward(folds=[(TRAIN, TEST), (TEST, TEST2)], search_space={"x": (0.0, 1.0)},
                       evaluate=evaluate, risk_gate=RiskGateConfig(), cache=cache,
                       n_init=2, n_iter=3, seed=0)
    assert calls["n"] < n1 + 2
    assert cache.stats()["hits"] > 0
    assert wf1.oos_utility_mean == wf2.oos_utility_mean


# =========================================================================
# 5. 策略生命周期: research → candidate → paper → degraded → retired
# =========================================================================

def _result(accepted: bool, gate_reason: str = "") -> OptimizeLoopResult:
    return OptimizeLoopResult(accepted=accepted, best_params={}, best_train_metrics={},
                              gate_metrics={}, gate_reason=gate_reason)


def test_lifecycle_promote_to_candidate_then_paper():
    lc = StrategyLifecycle("mom_v1")
    assert lc.rec.phase == "research"
    ev = lc.apply_optimize_result(_result(accepted=True))
    assert ev is not None and ev.event == "promote_to_candidate"
    assert lc.rec.phase == "candidate"
    # 保持: candidate 通过不再动
    assert lc.apply_optimize_result(_result(accepted=True)) is None
    assert lc.rec.phase == "candidate"
    # 晋升 paper (审计事件)
    ev2 = lc.promote_to_paper("wf accept_rate ok")
    assert lc.rec.phase == "paper"
    assert ev2.event == "promote_to_paper" and ev2.reason == "wf accept_rate ok"


def test_lifecycle_degrade_then_retire():
    lc = StrategyLifecycle("mom_v2", max_gate_failures=2)
    # 第一次失败: 计数, 不降级
    assert lc.apply_optimize_result(_result(accepted=False, gate_reason="sharpe 低")) is None
    assert lc.rec.phase == "research" and lc.rec.gate_failures == 1
    # 第二次失败: degraded
    ev = lc.apply_optimize_result(_result(accepted=False, gate_reason="sharpe 低"))
    assert lc.rec.phase == "degraded"
    assert ev.event == "degrade" and "sharpe" in ev.gate_reason
    # degraded 恢复: 通过 → research
    ev2 = lc.apply_optimize_result(_result(accepted=True))
    assert lc.rec.phase == "research"
    assert ev2.event == "recover_from_degraded" and lc.rec.gate_failures == 0
    # 再连续失败: research → degraded → retired
    lc.apply_optimize_result(_result(accepted=False))
    lc.apply_optimize_result(_result(accepted=False))
    assert lc.rec.phase == "degraded"
    lc.apply_optimize_result(_result(accepted=False))
    assert lc.rec.phase == "retired"


def test_lifecycle_snapshot_audit_trail():
    lc = StrategyLifecycle("mom_v3")
    lc.apply_optimize_result(_result(accepted=True))
    lc.promote_to_paper("wf ok")
    snap = lc.snapshot()
    assert snap["strategy_id"] == "mom_v3"
    assert snap["phase"] == "paper"
    assert [e["event"] for e in snap["history"]] == \
           ["promote_to_candidate", "promote_to_paper"]
    assert all(e["ts"] > 0 for e in snap["history"])


def test_lifecycle_promote_to_paper_requires_candidate():
    lc = StrategyLifecycle("mom_v4")
    with pytest.raises(ValueError):
        lc.promote_to_paper("too early")        # research 不可直接晋升

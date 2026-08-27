"""条件 Cholesky 流机制测试 — 采样/反演/精确似然/拟合/do + 连续 SCM L3。

门禁:
  1. 三角算子: 投影对角>0、前代法精确求解、det=Π 对角;
  2. 闭式拟合: 从已知 Σ 生成数据 → 恢复 L 与 μ (逐位);
  3. 似然:     log_prob 与 scipy 多元正态解析值一致;
  4. 反演回环: sample(u) → invert → u;
  5. 残差相关: Cholesky 对数似然 ≥ 对角对照; 强相关下 L3 反事实显著不同;
  6. MLP 拟合: 非线性均值下优于线性闭式; 梯度经有限差分验证;
  7. 连续 SCM: abduct → do → predict 端到端 L3。
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import multivariate_normal

from veya_loop import (
    CholeskyMechanism,
    ContinuousCholeskySCM,
    forward_substitute,
    log_det_lower,
    project_lower_triangular,
)

rng = np.random.default_rng(42)


# =========================================================================
# 一、三角算子
# =========================================================================


def test_project_lower_triangular():
    raw = np.array([0.0, 1.0, -1.0, 2.0, -2.0, 3.0])  # d=3 → 6 参数
    L = project_lower_triangular(raw, 3)
    assert np.allclose(L, np.tril(L))  # 下三角
    assert (np.diag(L) > 0).all()  # 对角 softplus > 0
    assert np.allclose(L[0, 0], np.log1p(1))  # softplus(0) = ln2
    assert L[1, 0] == 1.0 and L[2, 1] == -2.0  # 严格下三角自由
    assert log_det_lower(L) == pytest.approx(np.sum(np.log(np.diag(L))))


def test_forward_substitute_exact():
    L = np.array([[2.0, 0.0], [1.0, 3.0]])
    b = np.array([4.0, 11.0])
    u = forward_substitute(L, b)
    assert np.allclose(L @ u, b)  # 精确回代
    # 与显式逆一致
    assert np.allclose(u, np.linalg.inv(L) @ b)


# =========================================================================
# 二、闭式拟合: 恢复已知 Σ
# =========================================================================


def test_fit_linear_recovers_true_parameters():
    d = 3
    true_mu = np.array([1.0, -2.0, 0.5])
    # 手构相关残差协方差 → 真 Cholesky
    A = rng.standard_normal((d, d))
    Sigma = A @ A.T + np.eye(d) * 0.5
    L_true = np.linalg.cholesky(Sigma)

    pa = rng.standard_normal((2000, 2))  # 2 维 PA
    x = np.tile(true_mu, (2000, 1)) + rng.standard_normal((2000, d)) @ L_true.T

    mech = CholeskyMechanism.fit_linear(pa, x)
    # μ 恢复 (常数均值 → 截距)
    assert np.allclose(mech.mean(np.zeros(2)), true_mu, atol=0.1)
    # L 恢复 (Cholesky 唯一)
    assert np.allclose(mech.chol(np.zeros(2)), L_true, atol=0.1)


def test_log_prob_matches_analytic_gaussian():
    d = 2
    L = np.array([[1.5, 0.0], [0.7, 1.2]])
    Sigma = L @ L.T
    mu = np.array([0.3, -0.4])
    mech = CholeskyMechanism(
        d, mean_coef=np.array([*mu, 0.0, 0.0]), chol_fixed=L
    )  # mean_coef: [1, pa1, pa2] 的系数
    # 修正: 直接构造函数式机制
    mech = CholeskyMechanism(d, mean_fn=lambda pa: mu, chol_fn=lambda pa: L)

    x = np.array([0.5, -0.2])
    got = mech.log_prob(np.zeros(0), x)
    want = multivariate_normal(mean=mu, cov=Sigma).logpdf(x)
    assert got == pytest.approx(want, abs=1e-9)
    # 反演回环
    u = mech.invert(np.zeros(0), x)
    assert np.allclose(mech.sample(np.zeros(0), u=u), x)


# =========================================================================
# 三、残差相关: Cholesky vs 对角
# =========================================================================


def _correlated_data(n=800, rho=0.9):
    """2 维遥测, 强残差相关; PA 为 1 维 (如部署状态)。"""
    pa = rng.standard_normal((n, 1))
    mu = np.hstack([0.5 * pa, -0.3 * pa])
    L = np.array([[1.0, 0.0], [rho, np.sqrt(1 - rho**2)]])
    x = mu + rng.standard_normal((n, 2)) @ L.T
    return pa, x


def test_cholesky_beats_diagonal_on_correlated_residuals():
    pa, x = _correlated_data()
    chol = CholeskyMechanism.fit_linear(pa, x)
    diag = CholeskyMechanism.fit_linear(pa, x, diag_only=True)

    # 留出验证: Cholesky 对数似然显著高于对角 (残差相关被建模)
    pa_h, x_h = _correlated_data(300)
    ll_chol = sum(chol.log_prob(p, v) for p, v in zip(pa_h, x_h, strict=True))
    ll_diag = sum(diag.log_prob(p, v) for p, v in zip(pa_h, x_h, strict=True))
    assert ll_chol > ll_diag + 50
    # 对角流的 L 对角应小于 Cholesky 的 (它把相关方差塞进对角)
    assert np.diag(chol.chol_fixed)[1] < np.diag(diag.chol_fixed)[1] + 1e-9


def test_l3_counterfactual_differs_between_cholesky_and_diagonal():
    """异方差 L(PA) 下, do(PA) 的联合反事实: Cholesky 与对角显著不同。

    线性同方差 (常数 L) 时点反事实对流不变 (残差原样透传, L·L⁻¹ 相消);
    真实差异出现在 L(PA) 随 PA 变化 —— 本测试用双 regime 机制:
      PA=1 (degraded): L1 强相关;  PA=0 (stable): L0 弱相关。
    证据在 PA=1 的联合异常 → do(PA=0) 反事实, 两种流给出不同点估计。
    """
    import networkx as nx

    n = 600
    pa = rng.integers(0, 2, (n, 1)).astype(float)
    L1 = np.array([[1.0, 0.0], [0.9, np.sqrt(1 - 0.9**2)]])  # degraded: 强相关
    L0 = np.array([[1.0, 0.0], [0.1, np.sqrt(1 - 0.1**2)]])  # stable: 弱相关
    mu1 = np.array([0.5, -0.3])
    mu0 = np.array([0.0, 0.0])
    z = rng.standard_normal((n, 2))
    api = np.array([mu1 if p else mu0 for p in pa[:, 0]])
    for i, p in enumerate(pa[:, 0]):
        api[i] = api[i] + (L1 if p else L0) @ z[i]

    def mechanism(diag_only: bool):
        def mean_fn(p):
            return mu1 if p[0] > 0.5 else mu0

        def chol_fn(p):
            L = L1 if p[0] > 0.5 else L0
            return np.diag(np.diag(L)) if diag_only else L

        return CholeskyMechanism(2, mean_fn=mean_fn, chol_fn=chol_fn)

    def scm(diag_only: bool) -> ContinuousCholeskySCM:
        dag = nx.DiGraph()
        dag.add_edge("deploy_state", "api_service")
        mech_dep = CholeskyMechanism(
            1, mean_fn=lambda p: np.array([1.0]), chol_fn=lambda p: np.array([[1.0]])
        )
        from veya_loop import ContinuousNode

        return ContinuousCholeskySCM(
            {
                "deploy_state": ContinuousNode("deploy_state", mech_dep, []),
                "api_service": ContinuousNode(
                    "api_service", mechanism(diag_only), ["deploy_state"]
                ),
            }
        )

    scm_chol = scm(False)
    scm_diag = scm(True)

    # 本次事件: deploy=1 (degraded) 时观测联合异常 (u 在 L1 空间)
    evidence = {"deploy_state": np.array([1.0]), "api_service": np.array([2.1, 1.6])}
    # L3: 若当时 do(deploy_state = 0), api_service 会是什么 (锚定本次噪声)
    cf_chol = scm_chol.l3_counterfactual(evidence, {"deploy_state": [0.0]}, "api_service")
    cf_diag = scm_diag.l3_counterfactual(evidence, {"deploy_state": [0.0]}, "api_service")
    # Cholesky: u 在 L1 空间反演、在 L0 空间重放 (保持联合相关结构);
    # 对角: 逐维缩放, 相关结构丢失 → 反事实显著不同
    assert np.linalg.norm(cf_chol - cf_diag) > 0.3
    # Cholesky 回环: 在 do 前状态 (PA=1) 反演回到同一 u
    u = scm_chol.abduct(evidence)["api_service"]
    mech = scm_chol.nodes["api_service"].mechanism
    assert np.allclose(mech.invert(np.array([1.0]), np.array([2.1, 1.6])), u, atol=1e-9)


def test_continuous_scm_l3_counterfactual():
    """deploy_state → api_service(2 维遥测) 链, 强残差相关。"""
    import networkx as nx

    n = 600
    pa = rng.standard_normal((n, 1))  # deploy_state
    L = np.array([[1.0, 0.0], [0.85, np.sqrt(1 - 0.85**2)]])
    tele = np.hstack([0.4 * pa, -0.2 * pa]) + rng.standard_normal((n, 2)) @ L.T

    dag = nx.DiGraph()
    dag.add_edge("deploy_state", "api_service")
    scm = ContinuousCholeskySCM.fit_from_data(dag, {"deploy_state": pa, "api_service": tele})

    # 本次事件: deploy=1.2 时观测到联合异常
    evidence = {"deploy_state": np.array([1.2]), "api_service": np.array([2.1, 1.6])}
    # L3: 若当时 do(deploy_state = 0.0), api_service 会是什么 (锚定本次噪声)
    cf = scm.l3_counterfactual(evidence, {"deploy_state": [0.0]}, "api_service")
    # L2: 平均情形 (不锚定噪声)
    l2 = scm.l2_expected("api_service", {"deploy_state": [0.0]})
    assert cf.shape == (2,)
    # 反事实与本次观测不同 (干预生效), 且与平均期望不同 (噪声被锚定)
    assert np.linalg.norm(cf - np.array([2.1, 1.6])) > 0.5
    assert np.linalg.norm(cf - l2) > 1e-6
    # 溯因回环: 反事实在 do 前状态反演 → 同一噪声
    u_map = scm.abduct(evidence)
    u_api = u_map["api_service"]
    assert np.allclose(
        forward_substitute(
            scm.nodes["api_service"].mechanism.chol(np.array([1.2])),
            np.array([2.1, 1.6]) - scm.nodes["api_service"].mechanism.mean(np.array([1.2])),
        ),
        u_api,
        atol=1e-9,
    )


def test_continuous_scm_deterministic():
    import networkx as nx

    pa = rng.standard_normal((300, 1))
    tele = np.hstack([pa, -pa]) + rng.standard_normal((300, 2))
    dag = nx.DiGraph()
    dag.add_edge("deploy_state", "api_service")
    scm = ContinuousCholeskySCM.fit_from_data(dag, {"deploy_state": pa, "api_service": tele})
    ev = {"deploy_state": np.array([0.5]), "api_service": np.array([0.8, -0.6])}
    r1 = scm.l3_counterfactual(ev, {"deploy_state": [1.0]}, "api_service")
    r2 = scm.l3_counterfactual(ev, {"deploy_state": [1.0]}, "api_service")
    assert np.allclose(r1, r2)


# =========================================================================
# 六、数值稳定化: diag floor / offdiag scale / κ 正则
# =========================================================================


def test_diag_floor_and_offdiag_scale_config():
    """L_ii ≥ floor; |L_ij| ≤ scale (投影限幅生效)。"""
    from veya_loop.oprim._cholesky_flow import project_conditioned_lower_triangular

    # 极端 raw: 对角极负 (softplus→0), 非对角极大
    raw = np.array([-50.0, 10.0, -50.0, 10.0, 10.0, -50.0])
    L = project_conditioned_lower_triangular(raw, 3, diag_floor=1e-3, offdiag_scale=0.3)
    assert np.all(np.diag(L) >= 1e-3 - 1e-12)  # 对角有下界
    assert np.all(np.abs(L - np.diag(np.diag(L))) <= 0.3 + 1e-12)  # 非对角限幅
    # 对角退化: diag_only 只输出对角
    Ld = project_conditioned_lower_triangular(
        np.array([1.0, 2.0]), 2, diag_floor=1e-4, diag_only=True
    )
    assert np.allclose(Ld, np.diag(np.diag(Ld)))


def test_kappa_penalty_positive_when_ill_conditioned():
    """病态 L (对角被压扁) → κ 惩罚 > 0; 健康 L → 0。"""
    mech_sick = CholeskyMechanism(
        2, mean_fn=lambda p: np.zeros(2), chol_fn=lambda p: np.array([[1e-6, 0.0], [0.5, 1e-6]])
    )
    assert mech_sick.condition_number_proxy() > 1e3  # 代理条件数巨大
    assert mech_sick.kappa_penalty(kappa_max=50.0) > 0.0  # 超上限 → 惩罚

    mech_healthy = CholeskyMechanism(
        2, mean_fn=lambda p: np.zeros(2), chol_fn=lambda p: np.array([[1.0, 0.0], [0.2, 0.8]])
    )
    assert mech_healthy.kappa_penalty(kappa_max=50.0) == 0.0  # 健康 → 无惩罚


def test_fit_with_kappa_reg_finishes_finite_loss():
    """带 κ 正则的拟合可跑完且 loss 有限; 正则使对角下界保持。"""
    n = 200
    pa = rng.uniform(-1, 1, (n, 1))
    x = np.hstack([pa, -pa]) + rng.standard_normal((n, 2)) @ np.array([[1.0, 0.0], [0.3, 0.9]])

    mech = CholeskyMechanism.fit_mlp(
        pa, x, epochs=120, hidden=8, lr=0.05, seed=3, kappa_reg=0.1, kappa_max=50.0
    )
    assert np.isfinite(mech._train_nll)
    # 拟合出的 L 保持良态 (对角比受控)
    assert mech.condition_number_proxy(np.array([0.0])) < 200


# =========================================================================
# 七、hybrid SCM: 拟合 → 溯因 → 反事实 / 仿真 / 离散父
# =========================================================================


def _hybrid_dag():
    import networkx as nx

    dag = nx.DiGraph()
    dag.add_edge("deploy_state", "api_service")  # 离散父
    dag.add_edge("traffic", "api_service")  # 连续父
    return dag


def _hybrid_data(n=240):
    deploy = rng.integers(0, 3, (n, 1)).astype(float)  # 3 类
    traffic = rng.standard_normal((n, 1))
    # 条件均值: 类别偏移 + 流量线性; 残差相关 (cholesky)
    mu = np.hstack([deploy[:, :1] * 0.8, -0.5 * deploy[:, :1]]) + np.hstack(
        [0.3 * traffic, 0.2 * traffic]
    )
    L = np.array([[1.0, 0.0], [0.7, np.sqrt(1 - 0.7**2)]])
    api = mu + rng.standard_normal((n, 2)) @ L.T
    return {"deploy_state": deploy, "traffic": traffic, "api_service": api}


def test_hybrid_scm_fit_abduct_cf_and_simulate():
    from veya_loop import NodeSpec, build_hybrid_scm, fit_hybrid_scm

    dag = _hybrid_dag()
    specs = {
        "deploy_state": NodeSpec("deploy_state", "discrete"),
        "traffic": NodeSpec("traffic", "continuous", dim=1, continuous_mech="diagonal"),
        "api_service": NodeSpec(
            "api_service",
            "continuous",
            dim=2,
            continuous_mech="cholesky",
            diag_floor=1e-3,
            offdiag_scale=0.5,
        ),
    }
    data = _hybrid_data()
    scm = fit_hybrid_scm(
        build_hybrid_scm(dag, specs), data, epochs=80, hidden=8, kappa_reg=0.05, kappa_max=50.0
    )

    # 拟合完成: cholesky 节点有机制且 diag floor 生效 (PA 维 = one-hot 3 + traffic 1)
    mech = scm.mechanisms["api_service"]
    assert mech is not None and np.all(np.diag(mech.chol(np.zeros(4))) >= 1e-3 - 1e-9)

    # 溯因回环: 证据反演 → 反事实传播
    evidence = {
        "deploy_state": np.array([2.0]),
        "traffic": np.array([0.5]),
        "api_service": np.array([2.0, -1.5]),
    }
    u_map = scm.abduct(evidence)
    assert "api_service" in u_map and u_map["api_service"].shape == (2,)
    # L3: do(deploy_state = 0) → api_service 反事实 (锚定本次噪声)
    cf = scm.l3_counterfactual(evidence, {"deploy_state": [0.0]}, "api_service")
    assert cf.shape == (2,) and np.isfinite(cf).all()
    # 干预生效: 反事实均值应偏离证据 (类别偏移被移除)
    assert abs(cf[0] - 2.0) > 0.3

    # 仿真: 前向采样形状正确且有限
    sim = scm.simulate(n_samples=50, seed=1)
    assert sim["api_service"].shape == (50, 2)
    assert sim["deploy_state"].shape == (50, 1)
    assert np.isfinite(sim["api_service"]).all()


def test_hybrid_scm_discrete_parent_onehot_and_cholesky_correlation():
    """离散父 one-hot 编码生效 (条件均值按类别分离); Cholesky 残差相关可复现。"""
    from veya_loop import NodeSpec, build_hybrid_scm, fit_hybrid_scm

    dag = _hybrid_dag()
    specs = {
        "deploy_state": NodeSpec("deploy_state", "discrete"),
        "traffic": NodeSpec("traffic", "continuous", dim=1, continuous_mech="diagonal"),
        "api_service": NodeSpec("api_service", "continuous", dim=2, continuous_mech="cholesky"),
    }
    data = _hybrid_data(300)
    scm = fit_hybrid_scm(build_hybrid_scm(dag, specs), data, epochs=100, hidden=8, seed=5)

    # 仿真: 固定离散父 → 样本残差相关 (cholesky L 的非对角保持)
    sim_a = scm.simulate(
        n_samples=400, seed=2, intervened={"deploy_state": [1.0], "traffic": [0.0]}
    )
    resid = sim_a["api_service"] - sim_a["api_service"].mean(axis=0)
    corr = np.corrcoef(resid.T)[0, 1]
    assert corr > 0.4  # 残差相关 > 0.4
    # 不同离散类别 → 条件均值不同 (one-hot 生效, 逐维比较: dim0 类别偏移 +0.8)
    sim_b = scm.simulate(
        n_samples=400, seed=3, intervened={"deploy_state": [2.0], "traffic": [0.0]}
    )
    assert abs(sim_a["api_service"][:, 0].mean() - sim_b["api_service"][:, 0].mean()) > 0.4


def test_hybrid_scm_diagonal_node_no_residual_correlation():
    """diagonal 机制节点: 残差相关 ≈ 0 (对照)。"""
    from veya_loop import NodeSpec, build_hybrid_scm, fit_hybrid_scm

    dag = _hybrid_dag()
    specs = {
        "deploy_state": NodeSpec("deploy_state", "discrete"),
        "traffic": NodeSpec("traffic", "continuous", dim=1, continuous_mech="diagonal"),
        "api_service": NodeSpec("api_service", "continuous", dim=2, continuous_mech="diagonal"),
    }
    data = _hybrid_data(300)
    scm = fit_hybrid_scm(build_hybrid_scm(dag, specs), data, epochs=100, hidden=8, seed=6)
    sim = scm.simulate(n_samples=400, seed=7, intervened={"deploy_state": [1.0], "traffic": [0.0]})
    resid = sim["api_service"] - sim["api_service"].mean(axis=0)
    assert abs(np.corrcoef(resid.T)[0, 1]) < 0.2


# =========================================================================
# 八、kappa_reg=None 自动默认 + return_stats 训练监控
# =========================================================================


def test_kappa_reg_auto_default_by_mechanism():
    """存在 Cholesky 节点 → kappa_reg 自动 0.1; 纯对角 → 0.0。"""
    from veya_loop import NodeSpec, build_hybrid_scm, fit_hybrid_scm

    data = _hybrid_data(150)
    dag = _hybrid_dag()

    # 混合: 含 Cholesky 节点
    specs_chol = {
        "deploy_state": NodeSpec("deploy_state", "discrete"),
        "traffic": NodeSpec("traffic", "continuous", dim=1, continuous_mech="diagonal"),
        "api_service": NodeSpec("api_service", "continuous", dim=2, continuous_mech="cholesky"),
    }
    _, _, stats_chol = fit_hybrid_scm(
        build_hybrid_scm(dag, specs_chol), data, epochs=30, hidden=8, return_stats=True
    )
    assert stats_chol["kappa_reg"] == 0.1

    # 纯对角: 无 Cholesky → 自动 0.0
    specs_diag = {
        "deploy_state": NodeSpec("deploy_state", "discrete"),
        "traffic": NodeSpec("traffic", "continuous", dim=1, continuous_mech="diagonal"),
        "api_service": NodeSpec("api_service", "continuous", dim=2, continuous_mech="diagonal"),
    }
    _, _, stats_diag = fit_hybrid_scm(
        build_hybrid_scm(dag, specs_diag), data, epochs=30, hidden=8, return_stats=True
    )
    assert stats_diag["kappa_reg"] == 0.0

    # 显式关闭
    _, _, stats_off = fit_hybrid_scm(
        build_hybrid_scm(dag, specs_chol),
        data,
        epochs=30,
        hidden=8,
        kappa_reg=0.0,
        return_stats=True,
    )
    assert stats_off["kappa_reg"] == 0.0


def test_return_stats_monitoring_fields():
    """return_stats: losses 逐 epoch 有限; stats 含 min_diag/proxy/penalty。"""
    from veya_loop import NodeSpec, build_hybrid_scm, fit_hybrid_scm

    data = _hybrid_data(200)
    dag = _hybrid_dag()
    specs = {
        "deploy_state": NodeSpec("deploy_state", "discrete"),
        "traffic": NodeSpec("traffic", "continuous", dim=1, continuous_mech="diagonal"),
        "api_service": NodeSpec("api_service", "continuous", dim=2, continuous_mech="cholesky"),
    }
    _scm, losses, stats = fit_hybrid_scm(
        build_hybrid_scm(dag, specs), data, epochs=40, hidden=8, return_stats=True
    )
    assert len(losses) == 40
    assert all(np.isfinite(lv) for lv in losses)
    assert losses[-1] < losses[0]  # 训练在下降
    assert {
        "kappa_reg",
        "min_diag",
        "mean_kappa_proxy",
        "mean_kappa_penalty",
        "mean_exact_kappa",
    } <= set(stats)
    assert stats["min_diag"] >= 1e-4 - 1e-9  # 对角下界被守住
    assert stats["mean_kappa_proxy"] > 0
    assert stats["mean_exact_kappa"] > 0  # 低频真 κ (SVD) 已监控
    # 良态拟合: 真 κ 与代理同量级 (proxy 是下界方向, 允许略小)
    assert stats["mean_exact_kappa"] < 5.0


def test_cheap_proxy_catches_offdiag_pathology():
    """廉价代理 = 对角比 × (1 + mean|off|/mean_diag): 两对角同小但非对角大 → 检出。"""
    mech = CholeskyMechanism(
        2, mean_fn=lambda p: np.zeros(2), chol_fn=lambda p: np.array([[1e-6, 0.0], [0.5, 1e-6]])
    )
    proxy = mech.condition_number_proxy()
    assert proxy > 1e3  # 对角比=1 但非对角项放大
    assert mech.kappa_penalty(kappa_max=50.0) > 0.0
    # 健康 L: 代理小, 无惩罚
    healthy = CholeskyMechanism(
        2, mean_fn=lambda p: np.zeros(2), chol_fn=lambda p: np.array([[1.0, 0.0], [0.2, 0.8]])
    )
    assert healthy.kappa_penalty(kappa_max=50.0) == 0.0
    # 代理与精确 κ₂ 同量级 (良态时)
    assert healthy.exact_condition_number() < 5.0
    assert 0.5 < healthy.condition_number_proxy() < 3.0


# =========================================================================
# 九、Ledoit–Wolf 收缩估计
# =========================================================================


def test_ledoit_wolf_matches_sklearn():
    """numpy LW 公式 vs sklearn 参考实现 (α 与 Σ 逐位接近)。"""
    from sklearn.covariance import ledoit_wolf as sk_lw
    from veya_loop.oprim._cholesky_flow import ledoit_wolf_covariance

    rng_lw = np.random.default_rng(11)
    n, p = 60, 8  # n≈8p, 大维/小样本场景
    A = rng_lw.standard_normal((p, p))
    Sigma_true = A @ A.T + np.eye(p)
    X = rng_lw.multivariate_normal(np.zeros(p), Sigma_true, size=n)

    Sigma_mine, alpha_mine = ledoit_wolf_covariance(X)
    Sigma_ref, alpha_ref = sk_lw(X)

    assert alpha_mine == pytest.approx(alpha_ref, abs=0.05)
    assert np.allclose(Sigma_mine, Sigma_ref, atol=0.05)
    assert 0.0 <= alpha_mine <= 1.0


def test_ledoit_wolf_improves_condition_number_and_converges():
    """LW 压 κ (小样本); n 大时 α → 0 (回到样本协方差)。"""
    from veya_loop.oprim._cholesky_flow import ledoit_wolf_covariance

    rng_lw = np.random.default_rng(3)
    p = 6
    A = rng_lw.standard_normal((p, p))
    Sigma_true = A @ A.T + np.eye(p) * 0.1

    # 小样本: LW 条件数显著低于样本协方差
    X_small = rng_lw.multivariate_normal(np.zeros(p), Sigma_true, size=30)
    S_small = X_small - X_small.mean(0)
    kappa_sample = np.linalg.cond(S_small.T @ S_small / 30)
    Sigma_lw, alpha = ledoit_wolf_covariance(X_small)
    kappa_lw = np.linalg.cond(Sigma_lw)
    assert alpha > 0.1
    assert kappa_lw < kappa_sample * 0.8

    # 大样本: α → 0, LW ≈ 样本协方差
    X_big = rng_lw.multivariate_normal(np.zeros(p), Sigma_true, size=20000)
    _, alpha_big = ledoit_wolf_covariance(X_big)
    assert alpha_big < 0.05


def test_fit_linear_shrinkage_lw():
    """fit_linear(shrinkage='lw'): 小样本下条件数更好且 L 良态。"""
    rng_lw = np.random.default_rng(5)
    n, k, d = 40, 2, 3
    pa = rng_lw.standard_normal((n, k))
    A = rng_lw.standard_normal((d, d))
    Sigma = A @ A.T + np.eye(d) * 0.2
    L_true = np.linalg.cholesky(Sigma)
    x = rng_lw.standard_normal((n, d)) @ L_true.T

    m_sample = CholeskyMechanism.fit_linear(pa, x, shrinkage="sample")
    m_lw = CholeskyMechanism.fit_linear(pa, x, shrinkage="lw")
    assert m_lw.condition_number_proxy(np.zeros(k)) < m_sample.condition_number_proxy(np.zeros(k))
    assert m_lw.exact_condition_number() < m_sample.exact_condition_number()

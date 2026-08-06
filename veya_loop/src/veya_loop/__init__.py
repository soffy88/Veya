"""Veya Loop Engine / 维亚闭环引擎 — 因果闭环控制基板 (Phase 2/3/4)。

边界: 3O 主库 (oprim/oskill/omodul/obase) 是独立主数据包, 本包只装配不混用;
Veya 业务项目通过依赖本包接入因果闭环能力。

公共 API 面 (惰性装配, 见 _assembly):
  P1 神经符号: PlanIR 四闸门 (validate/MUS/MaxSMT optimize/回译 diff) ·
               vcg/check_strategyproof · LeaseManager/WaitForGraph ·
               Game/pure_nash · SnapshotStore · MCTS/puct · lookahead ·
               ActionPlan/Applier · LocalSandbox/SandboxPool
  Phase 2 因果测谎: CausalGraphStore / causal_fault_diagnose /
                    build_binary_failure_cpd_map / BayesianBeliefUpdater
  Phase 3 反脆弱闭环: closed_loop_intervene / threat_model_evolve /
                      select_intervention / expected_utility /
                      CategoricalCPD / update_cpd / AuditEmitter / JsonlSink
  Phase 4 长视距规划: multi_step_plan / counterfactual_rollout / StrategyEvolver
  L3 反事实: StructuralSCM / CholeskyMechanism / HybridSCM /
             bayesian_optimize / fit_deep_scm / counterfactual_diagnose
  代码可靠性: run_code_reliability_loop / CodeTask / CodeLoopResult
  Veya Loop 自有组件: HardenedExecutor / PermissionContract /
                      dispatch_intervention / ExecutionAdapter / veya-loop CLI
"""

from __future__ import annotations

from typing import Any

from ._version import __version__
from .execution_adapters import (  # noqa: F401 — 公共 API 面
    ExecutionAdapter,
    RestartAdapter,
    dispatch_via_adapter,
)

# Veya Loop 自有组件 (业务装配层, 立即导入)
from .hardened import (
    DispatchResult,
    ExecOutcome,
    HardenedExecutor,
    PermissionContract,
    PermissionDecision,
    dispatch_intervention,
)

# 3O 主库元素: 惰性解析 (直接 import 会因主库未安装而失败, 用 __getattr__ 延迟)
_ELEMENT_MAP: dict[str, tuple[str, str]] = {
    # (主库, 符号) — 符号在主库包顶层或模块内
    "CausalGraphStore": ("obase", "CausalGraphStore"),
    "causal_fault_diagnose": ("omodul", "causal_fault_diagnose"),
    "CausalDiagnosisReport": ("omodul", "CausalDiagnosisReport"),
    "adversarial_honeypot_observe": ("omodul", "adversarial_honeypot_observe"),
    "build_binary_failure_cpd_map": ("oprim", "build_binary_failure_cpd_map"),
    "_do_calculus_intervention": ("oprim", "_do_calculus_intervention"),
    "counterfactual_rollout": ("oprim", "counterfactual_rollout"),
    "BayesianBeliefUpdater": ("oskill", "BayesianBeliefUpdater"),
    "StrategyEvolver": ("oskill", "StrategyEvolver"),
    "STRATEGY_NAMES": ("oskill", "STRATEGY_NAMES"),
    "CategoricalCPD": ("oskill", "CategoricalCPD"),
    "update_cpd": ("oskill", "update_cpd"),
    "select_intervention": ("oprim", "select_intervention"),
    "InterventionCandidate": ("oprim", "InterventionCandidate"),
    "closed_loop_intervene": ("omodul", "closed_loop_intervene"),
    "ClosedLoopConfig": ("omodul", "ClosedLoopConfig"),
    "ClosedLoopInput": ("omodul", "ClosedLoopInput"),
    "threat_model_evolve": ("omodul", "threat_model_evolve"),
    "ThreatModelConfig": ("omodul", "ThreatModelConfig"),
    "ThreatModelInput": ("omodul", "ThreatModelInput"),
    "multi_step_plan": ("omodul", "multi_step_plan"),
    "MultiStepPlanReport": ("omodul", "MultiStepPlanReport"),
    "counterfactual_diagnose": ("omodul", "counterfactual_diagnose"),
    "CounterfactualDiagnosisReport": ("omodul", "CounterfactualDiagnosisReport"),
    "CounterfactualReport": ("omodul", "CounterfactualReport"),
    "StructuralSCM": ("oprim", "StructuralSCM"),
    "SCMNode": ("oprim", "SCMNode"),
    "CholeskyMechanism": ("oprim", "CholeskyMechanism"),
    "ConditionalCouplingMechanism": ("oprim", "ConditionalCouplingMechanism"),
    "CouplingLayer": ("oprim", "CouplingLayer"),
    "RBFGP": ("oprim", "RBFGP"),
    "bayesian_optimize": ("oprim", "bayesian_optimize"),
    "continuous_plan_with_hybrid_bo": ("oprim", "continuous_plan_with_hybrid_bo"),
    "fit_deep_scm": ("oprim", "fit_deep_scm"),
    "calibrate_deep_scm_temperature": ("oprim", "calibrate_deep_scm_temperature"),
    "NodeSpec": ("oprim", "NodeSpec"),
    "run_code_reliability_loop": ("omodul", "run_code_reliability_loop"),
    "CodeTask": ("omodul", "CodeTask"),
    "CodeLoopResult": ("omodul", "CodeLoopResult"),
    "FailureSignature": ("omodul", "FailureSignature"),
    "FailureKind": ("omodul", "FailureKind"),
    "PatchArtifact": ("omodul", "PatchArtifact"),
    "TestResult": ("omodul", "TestResult"),
    "HybridSCM": ("oprim", "HybridSCM"),
    "build_hybrid_scm": ("oprim", "build_hybrid_scm"),
    "fit_hybrid_scm": ("oprim", "fit_hybrid_scm"),
    "forward_substitute": ("oprim", "forward_substitute"),
    "project_lower_triangular": ("oprim", "project_lower_triangular"),
    "log_det_lower": ("oprim", "log_det_lower"),
    "ContinuousCholeskySCM": ("omodul", "ContinuousCholeskySCM"),
    "ContinuousNode": ("omodul", "ContinuousNode"),
    "AuditEmitter": ("oprim", "AuditEmitter"),
    "AuditEvent": ("oprim", "AuditEvent"),
    "JsonlSink": ("oprim", "JsonlSink"),
    "MemorySink": ("oprim", "MemorySink"),
    "update_cpd_from_repair": ("omodul", "update_cpd_from_repair"),
    "get_runtime_causal_store": ("obase", "get_runtime_causal_store"),
    "CheckpointStore": ("obase", "CheckpointStore"),
    "get_intervention_cache": ("oprim", "get_intervention_cache"),
    "InferenceCache": ("oprim", "InferenceCache"),
    "graph_fingerprint": ("oprim", "graph_fingerprint"),
    "path_frequency_counts": ("oprim", "path_frequency_counts"),
    "count_simple_paths_dag": ("oprim", "count_simple_paths_dag"),
    "set_intervention_cache_capacity": ("oprim", "set_intervention_cache_capacity"),
    # ── P1 神经符号 · O1 四闸门 (Z3 校验 / MUS / MaxSMT / 回译) ──────
    "PlanIR": ("oprim", "PlanIR"),
    "parse_ir": ("oprim", "parse_ir"),
    "validate": ("oprim", "validate"),
    "compile_expr": ("oprim", "compile_expr"),
    "compile_ir": ("oprim", "compile_ir"),
    "domain_constraint_meta": ("oprim", "domain_constraint_meta"),
    "explain": ("oprim", "explain"),
    "shrink_to_mus": ("oprim", "shrink_to_mus"),
    "check_feasible": ("oprim", "check_feasible"),
    "install_determinism": ("oprim", "install_determinism"),
    "optimize": ("oprim", "optimize"),
    "diff_all": ("oprim", "diff_all"),
    "diff_one": ("oprim", "diff_one"),
    "render": ("oprim", "render"),
    # ── P1 神经符号 · O2 分配 (匈牙利/容量) + VCG 支付 + 策略证明 ────
    "assign_one_to_one": ("oprim", "assign_one_to_one"),
    "assign_with_capacity": ("oprim", "assign_with_capacity"),
    "cost_matrix": ("oprim", "cost_matrix"),
    "welfare": ("oprim", "welfare"),
    "first_price": ("oprim", "first_price"),
    "second_price_per_task": ("oprim", "second_price_per_task"),
    "vcg": ("oprim", "vcg"),
    "check_strategyproof": ("oprim", "check_strategyproof"),
    # ── P1 神经符号 · O2 死锁检测 (租约/资源序/等待图) ──────────────
    "LeaseManager": ("oprim", "LeaseManager"),
    "ResourceOrder": ("oprim", "ResourceOrder"),
    "WaitForGraph": ("oprim", "WaitForGraph"),
    # ── P1 神经符号 · 博弈论 (纯纳什/帕累托/主导策略) ────────────────
    "Game": ("oprim", "Game"),
    "dominant_strategies": ("oprim", "dominant_strategies"),
    "nash_vs_pareto_report": ("oprim", "nash_vs_pareto_report"),
    "pareto_optimal": ("oprim", "pareto_optimal"),
    "prisoners_dilemma": ("oprim", "prisoners_dilemma"),
    "pure_nash": ("oprim", "pure_nash"),
    # ── P1 神经符号 · 拍卖账本 (任务/工人/出价) ──────────────────────
    "Bid": ("oprim", "Bid"),
    "Ledger": ("oprim", "Ledger"),
    "Problem": ("oprim", "Problem"),
    "Task": ("oprim", "Task"),
    "Worker": ("oprim", "Worker"),
    # ── P1 神经符号 · O3 沙箱推演 (快照/稠密奖励/PUCT/lookahead) ────
    "HardlinkBackend": ("oprim", "HardlinkBackend"),
    "SnapshotStore": ("oprim", "SnapshotStore"),
    "atomic_write": ("oprim", "atomic_write"),
    "tree_digest": ("oprim", "tree_digest"),
    "DiffSizeProbe": ("oprim", "DiffSizeProbe"),
    "FileFrozenProbe": ("oprim", "FileFrozenProbe"),
    "py_syntax_gate": ("oprim", "py_syntax_gate"),
    "run_probes": ("oprim", "run_probes"),
    "unittest_probe": ("oprim", "unittest_probe"),
    "MCTS": ("oprim", "MCTS"),
    "best_path": ("oprim", "best_path"),
    "puct": ("oprim", "puct"),
    "Divergence": ("oprim", "Divergence"),
    "lookahead": ("oprim", "lookahead"),
    "render_verdict": ("oprim", "render_verdict"),
    "Action": ("oprim", "Action"),
    "ActionPlan": ("oprim", "ActionPlan"),
    "Applier": ("oprim", "Applier"),
    "Reversibility": ("oprim", "Reversibility"),
    "compensation_chain": ("oprim", "compensation_chain"),
    "gate": ("oprim", "gate"),
    "LocalSandbox": ("oprim", "LocalSandbox"),
    "SandboxPool": ("oprim", "SandboxPool"),
    # ── P3 期望效用 (补漏: 选型报告/效用/诊断接入) ────────────────────
    "SelectionResult": ("oprim", "SelectionResult"),
    "expected_utility": ("oprim", "expected_utility"),
    "from_diagnosis_report": ("oprim", "from_diagnosis_report"),
    # ── 审计补漏: 复合 sink ──────────────────────────────────────────
    "CompositeSink": ("oprim", "CompositeSink"),
}

__all__ = [
    "__version__",
    # Veya Loop 自有组件
    "DispatchResult", "ExecOutcome", "HardenedExecutor",
    "PermissionContract", "PermissionDecision", "dispatch_intervention",
    # 3O 主库元素
    *sorted(_ELEMENT_MAP),
]


def __getattr__(name: str) -> Any:
    if name not in _ELEMENT_MAP:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import _assembly

    lib, symbol = _ELEMENT_MAP[name]
    mod = _assembly.load(lib)
    value = getattr(mod, symbol)
    globals()[name] = value          # 缓存: 后续直接命中
    return value

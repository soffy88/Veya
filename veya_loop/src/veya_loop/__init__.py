"""Veya Loop Engine / 维亚闭环引擎 — 因果闭环控制基板 (Phase 2/3/4)。

边界: 3O 主库 (oprim/oskill/omodul/obase) 是独立主数据包, 本包只装配不混用;
Veya 业务项目通过依赖本包接入因果闭环能力。

公共 API 面 (惰性装配, 见 _assembly):
  Phase 2 因果测谎: CausalGraphStore / causal_fault_diagnose /
                    build_binary_failure_cpd_map / BayesianBeliefUpdater
  Phase 3 反脆弱闭环: closed_loop_intervene / threat_model_evolve /
                      select_intervention / CategoricalCPD / update_cpd /
                      AuditEmitter / JsonlSink
  Phase 4 长视距规划: multi_step_plan / counterfactual_rollout / StrategyEvolver
  Veya Loop 自有组件: HardenedExecutor / PermissionContract / dispatch_intervention
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
    "get_intervention_cache": ("oprim", "get_intervention_cache"),
    "InferenceCache": ("oprim", "InferenceCache"),
    "graph_fingerprint": ("oprim", "graph_fingerprint"),
    "path_frequency_counts": ("oprim", "path_frequency_counts"),
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

"""veya_loop.oprim._optimize_loop — 优化工程化 (ParamSpec/早停/指纹版本)
+ walk-forward + 策略生命周期 (单一来源转发)。

本体在主库 oprim._optimize_loop; 本模块只 re-export。
"""

from .._assembly import oprim as _load_oprim

_oprim = _load_oprim()

DEFAULT_UTILITY_WEIGHTS = _oprim._optimize_loop.DEFAULT_UTILITY_WEIGHTS
EvalCache = _oprim._optimize_loop.EvalCache
EvalWindow = _oprim._optimize_loop.EvalWindow
FoldResult = _oprim._optimize_loop.FoldResult
LifecycleEvent = _oprim._optimize_loop.LifecycleEvent
LifecycleRecord = _oprim._optimize_loop.LifecycleRecord
MultiObjectiveConfig = _oprim._optimize_loop.MultiObjectiveConfig
OptimizeLoopResult = _oprim._optimize_loop.OptimizeLoopResult
PHASES = _oprim._optimize_loop.PHASES
ParamSpec = _oprim._optimize_loop.ParamSpec
RiskGateConfig = _oprim._optimize_loop.RiskGateConfig
StrategyLifecycle = _oprim._optimize_loop.StrategyLifecycle
WalkForwardResult = _oprim._optimize_loop.WalkForwardResult
fingerprint_eval = _oprim._optimize_loop.fingerprint_eval
multi_objective_utility = _oprim._optimize_loop.multi_objective_utility
optimize_loop = _oprim._optimize_loop.optimize_loop
walk_forward = _oprim._optimize_loop.walk_forward

__all__ = [
           "DEFAULT_UTILITY_WEIGHTS",
           "PHASES",
           "EvalCache",
           "EvalWindow",
           "FoldResult",
           "LifecycleEvent",
           "LifecycleRecord",
           "MultiObjectiveConfig",
           "OptimizeLoopResult",
           "ParamSpec",
           "RiskGateConfig",
           "StrategyLifecycle",
           "WalkForwardResult",
           "fingerprint_eval",
           "multi_objective_utility",
           "optimize_loop",
           "walk_forward",
]

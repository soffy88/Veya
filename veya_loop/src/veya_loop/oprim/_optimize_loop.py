"""veya_loop.oprim._optimize_loop — 多目标效用优化循环机制 (单一来源转发)。

本体在主库 oprim._optimize_loop; 本模块只 re-export。
"""

from .._assembly import oprim as _load_oprim

_oprim = _load_oprim()

DEFAULT_UTILITY_WEIGHTS = _oprim._optimize_loop.DEFAULT_UTILITY_WEIGHTS
EvalCache = _oprim._optimize_loop.EvalCache
EvalWindow = _oprim._optimize_loop.EvalWindow
MultiObjectiveConfig = _oprim._optimize_loop.MultiObjectiveConfig
OptimizeLoopResult = _oprim._optimize_loop.OptimizeLoopResult
RiskGateConfig = _oprim._optimize_loop.RiskGateConfig
fingerprint_eval = _oprim._optimize_loop.fingerprint_eval
multi_objective_utility = _oprim._optimize_loop.multi_objective_utility
optimize_loop = _oprim._optimize_loop.optimize_loop

__all__ = ["DEFAULT_UTILITY_WEIGHTS", "EvalCache", "EvalWindow",
           "MultiObjectiveConfig", "OptimizeLoopResult", "RiskGateConfig",
           "fingerprint_eval", "multi_objective_utility", "optimize_loop"]

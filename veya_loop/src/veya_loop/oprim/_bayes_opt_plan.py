"""veya_loop.oprim._bayes_opt_plan — BO 规划 (单一来源转发)。"""

from .._assembly import oprim as _load_oprim

_oprim = _load_oprim()

RBFGP = _oprim._bayes_opt_plan.RBFGP
bayesian_optimize = _oprim._bayes_opt_plan.bayesian_optimize
continuous_plan_with_hybrid_bo = _oprim._bayes_opt_plan.continuous_plan_with_hybrid_bo

__all__ = ["RBFGP", "bayesian_optimize", "continuous_plan_with_hybrid_bo"]

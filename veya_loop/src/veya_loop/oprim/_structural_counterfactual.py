"""veya_loop.oprim._structural_counterfactual — L3 反事实 SCM (单一来源转发)。

本体在主库 oprim._structural_counterfactual; 本模块只 re-export。
"""

from .._assembly import oprim as _load_oprim

_oprim = _load_oprim()

SCMNode = _oprim._structural_counterfactual.SCMNode
StructuralSCM = _oprim._structural_counterfactual.StructuralSCM

__all__ = ["SCMNode", "StructuralSCM"]

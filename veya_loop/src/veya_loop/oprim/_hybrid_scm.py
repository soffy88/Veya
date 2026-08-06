"""veya_loop.oprim._hybrid_scm — 混合离散–连续 SCM (单一来源转发)。

本体在主库 oprim._hybrid_scm; 本模块只 re-export。
"""

from .._assembly import oprim as _load_oprim

_oprim = _load_oprim()

NodeSpec = _oprim._hybrid_scm.NodeSpec
HybridSCM = _oprim._hybrid_scm.HybridSCM
build_hybrid_scm = _oprim._hybrid_scm.build_hybrid_scm
fit_hybrid_scm = _oprim._hybrid_scm.fit_hybrid_scm

__all__ = ["HybridSCM", "NodeSpec", "build_hybrid_scm", "fit_hybrid_scm"]

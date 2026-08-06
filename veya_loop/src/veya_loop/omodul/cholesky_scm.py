"""veya_loop.omodul.cholesky_scm — 连续 Cholesky SCM (单一来源转发)。

本体在主库 omodul.cholesky_scm; 本模块只 re-export。
"""

from .._assembly import omodul as _load_omodul

_omodul = _load_omodul()

ContinuousCholeskySCM = _omodul.cholesky_scm.ContinuousCholeskySCM
ContinuousNode = _omodul.cholesky_scm.ContinuousNode

__all__ = ["ContinuousCholeskySCM", "ContinuousNode"]

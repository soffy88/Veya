"""veya_loop.oprim._deep_scm_train — 深度训练课表/校准 (单一来源转发)。"""

from .._assembly import oprim as _load_oprim

_oprim = _load_oprim()

fit_deep_scm = _oprim._deep_scm_train.fit_deep_scm
calibrate_deep_scm_temperature = _oprim._deep_scm_train.calibrate_deep_scm_temperature

__all__ = ["calibrate_deep_scm_temperature", "fit_deep_scm"]

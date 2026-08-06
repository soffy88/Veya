"""veya_loop.omodul.counterfactual_diagnose — L3 反事实诊断事务 (单一来源转发)。

本体在主库 omodul.counterfactual_diagnose; 本模块只 re-export。
"""

from .._assembly import omodul as _load_omodul

_omodul = _load_omodul()

CounterfactualDiagnosisReport = _omodul.counterfactual_diagnose.CounterfactualDiagnosisReport
CounterfactualReport = _omodul.counterfactual_diagnose.CounterfactualReport
counterfactual_diagnose = _omodul.counterfactual_diagnose.counterfactual_diagnose

__all__ = ["CounterfactualDiagnosisReport", "CounterfactualReport",
           "counterfactual_diagnose"]

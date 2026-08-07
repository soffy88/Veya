"""veya_loop.omodul.counterfactual_diagnose — L3 反事实诊断事务 (单一来源转发)。

本体在主库 omodul.counterfactual_diagnose; 本模块只 re-export。
"""

import importlib

from .._assembly import omodul as _load_omodul

_load_omodul()  # 确保 3O 路径已注入 (pip 或 submodule 兜底)
# 注意: omodul 顶层把 counterfactual_diagnose 导出为**函数**, 会遮蔽同名子模块 ——
# 必须显式 import_module 取子模块, 不能走 _omodul.counterfactual_diagnose 属性。
_cf = importlib.import_module("omodul.counterfactual_diagnose")

CounterfactualDiagnosisReport = _cf.CounterfactualDiagnosisReport
CounterfactualReport = _cf.CounterfactualReport
counterfactual_diagnose = _cf.counterfactual_diagnose

__all__ = ["CounterfactualDiagnosisReport", "CounterfactualReport", "counterfactual_diagnose"]

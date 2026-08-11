"""veya/utils — 3O 归位门面 (sys.modules 别名 → veya/obase/utils).

veya 包按 3O 范式重构 (SPEC v3.0 §2.1): 顶层平铺模块归位到分层包
veya/obase/utils.py。本文件注册 sys.modules 别名, 使 import veya.utils
拿到与 veya.obase.utils 完全相同的模块对象 — 属性访问/monkeypatch/
私有符号全部等价, 旧导入路径零成本兼容。新代码应直接
import veya.obase.utils。
"""
import sys

from veya.obase import utils as _impl

sys.modules[__name__] = _impl

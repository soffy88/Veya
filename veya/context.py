"""veya/context — 3O 归位门面 (sys.modules 别名 → veya/oservi/context).
"""
import sys
from veya.oservi import context as _impl
sys.modules[__name__] = _impl

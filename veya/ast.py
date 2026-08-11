"""veya/ast — 3O 归位门面 (sys.modules 别名 → veya/oprim/ast).
"""
import sys
from veya.oprim import ast as _impl
sys.modules[__name__] = _impl

"""veya/tools — 3O 归位门面 (sys.modules 别名 → veya/oskill/tools).
"""
import sys
from veya.oskill import tools as _impl
sys.modules[__name__] = _impl

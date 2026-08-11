"""veya/intent — 3O 归位门面 (sys.modules 别名 → veya/oskill/intent).
"""
import sys
from veya.oskill import intent as _impl
sys.modules[__name__] = _impl

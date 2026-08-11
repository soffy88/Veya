"""veya/memory_hub — 3O 归位门面 (sys.modules 别名 → veya/oskill/memory_hub).
"""
import sys
from veya.oskill import memory_hub as _impl
sys.modules[__name__] = _impl

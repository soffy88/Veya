"""veya/memory_store — 3O 归位门面 (sys.modules 别名 → veya/oskill/memory_store)."""

import sys
from veya.oskill import memory_store as _impl

sys.modules[__name__] = _impl

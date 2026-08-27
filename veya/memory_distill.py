"""veya/memory_distill — 3O 归位门面 (sys.modules 别名 → veya/oskill/memory_distill)."""

import sys

from veya.oskill import memory_distill as _impl

sys.modules[__name__] = _impl

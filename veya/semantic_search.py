"""veya/semantic_search — 3O 归位门面 (sys.modules 别名 → veya/oskill/semantic_search)."""

import sys

from veya.oskill import semantic_search as _impl

sys.modules[__name__] = _impl

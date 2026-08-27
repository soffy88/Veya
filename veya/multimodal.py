"""veya/multimodal — 3O 归位门面 (sys.modules 别名 → veya/oskill/multimodal)."""

import sys

from veya.oskill import multimodal as _impl

sys.modules[__name__] = _impl

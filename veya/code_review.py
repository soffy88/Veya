"""veya/code_review — 3O 归位门面 (sys.modules 别名 → veya/oskill/code_review)."""

import sys

from veya.oskill import code_review as _impl

sys.modules[__name__] = _impl

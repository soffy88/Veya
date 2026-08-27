"""veya/cross_language — 3O 归位门面 (sys.modules 别名 → veya/oprim/cross_language)."""

import sys

from veya.oprim import cross_language as _impl

sys.modules[__name__] = _impl

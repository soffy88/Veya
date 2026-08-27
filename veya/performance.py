"""veya/performance — 3O 归位门面 (sys.modules 别名 → veya/omodul/performance)."""

import sys
from veya.omodul import performance as _impl

sys.modules[__name__] = _impl

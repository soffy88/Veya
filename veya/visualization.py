"""veya/visualization — 3O 归位门面 (sys.modules 别名 → veya/omodul/visualization)."""

import sys
from veya.omodul import visualization as _impl

sys.modules[__name__] = _impl

"""veya/execution — 3O 归位门面 (sys.modules 别名 → veya/omodul/execution)."""

import sys
from veya.omodul import execution as _impl

sys.modules[__name__] = _impl

"""veya/history_store — 3O 归位门面 (sys.modules 别名 → veya/oservi/history_store)."""

import sys
from veya.oservi import history_store as _impl

sys.modules[__name__] = _impl

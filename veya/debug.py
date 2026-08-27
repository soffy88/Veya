"""veya/debug — 3O 归位门面 (sys.modules 别名 → veya/oservi/debug)."""

import sys

from veya.oservi import debug as _impl

sys.modules[__name__] = _impl

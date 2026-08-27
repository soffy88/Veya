"""veya/collaboration — 3O 归位门面 (sys.modules 别名 → veya/omodul/collaboration)."""

import sys

from veya.omodul import collaboration as _impl

sys.modules[__name__] = _impl

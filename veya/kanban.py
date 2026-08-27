"""veya/kanban — 3O 归位门面 (sys.modules 别名 → veya/omodul/kanban)."""

import sys

from veya.omodul import kanban as _impl

sys.modules[__name__] = _impl

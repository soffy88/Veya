"""veya/autonomous_agent — 3O 归位门面 (sys.modules 别名 → veya/omodul/autonomous_agent)."""

import sys

from veya.omodul import autonomous_agent as _impl

sys.modules[__name__] = _impl

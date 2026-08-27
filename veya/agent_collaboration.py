"""veya/agent_collaboration — 3O 归位门面 (sys.modules 别名 → veya/omodul/agent_collaboration)."""

import sys

from veya.omodul import agent_collaboration as _impl

sys.modules[__name__] = _impl

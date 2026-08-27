"""veya/agent_project — 3O 归位门面 (sys.modules 别名 → veya/omodul/agent_project)."""

import sys
from veya.omodul import agent_project as _impl

sys.modules[__name__] = _impl

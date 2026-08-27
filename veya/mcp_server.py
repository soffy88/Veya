"""veya/mcp_server — 3O 归位门面 (sys.modules 别名 → veya/oservi/mcp_server)."""

import sys

from veya.oservi import mcp_server as _impl

sys.modules[__name__] = _impl

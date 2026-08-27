"""veya/streaming — 3O 归位门面 (sys.modules 别名 → veya/oservi/streaming)."""

import sys
from veya.oservi import streaming as _impl

sys.modules[__name__] = _impl

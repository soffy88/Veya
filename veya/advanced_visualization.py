"""veya/advanced_visualization — 3O 归位门面 (sys.modules 别名 → veya/omodul/advanced_visualization)."""

import sys
from veya.omodul import advanced_visualization as _impl

sys.modules[__name__] = _impl

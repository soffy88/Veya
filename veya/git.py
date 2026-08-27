"""veya/git — 3O 归位门面 (sys.modules 别名 → veya/oprim/git)."""

import sys

from veya.oprim import git as _impl

sys.modules[__name__] = _impl

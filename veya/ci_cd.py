"""veya/ci_cd — 3O 归位门面 (sys.modules 别名 → veya/omodul/ci_cd).
"""
import sys
from veya.omodul import ci_cd as _impl
sys.modules[__name__] = _impl

"""veya/integrations — 3O 归位门面 (sys.modules 别名 → veya/omodul/integrations).
"""
import sys
from veya.omodul import integrations as _impl
sys.modules[__name__] = _impl

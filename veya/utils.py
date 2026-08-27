"""Compatibility facade that aliases ``veya.utils`` to ``veya.obase.utils``.

The historical import path resolves to the canonical utilities module with
the same module-object behavior for attributes and monkeypatching.
"""

import sys

from veya.obase import utils as _impl

sys.modules[__name__] = _impl

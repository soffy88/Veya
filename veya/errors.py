"""Compatibility facade that aliases ``veya.errors`` to ``veya.obase.errors``.

The historical import path resolves to the canonical errors module with the
same module-object behavior for attributes and monkeypatching.
"""

import sys

from veya.obase import errors as _impl

sys.modules[__name__] = _impl

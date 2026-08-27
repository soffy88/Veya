"""Compatibility facade that aliases ``veya.sandbox`` to the obase module.

The historical import path resolves to the canonical sandbox implementation,
including equivalent attribute access and monkeypatch behavior.
"""

import sys

from veya.obase import sandbox as _impl

sys.modules[__name__] = _impl

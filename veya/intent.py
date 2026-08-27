"""Compatibility facade that aliases ``veya.intent`` to ``veya.oskill.intent``."""

import sys

from veya.oskill import intent as _impl

sys.modules[__name__] = _impl

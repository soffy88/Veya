"""Compatibility facade that aliases ``veya.multimodal`` to the oskill module."""

import sys

from veya.oskill import multimodal as _impl

sys.modules[__name__] = _impl

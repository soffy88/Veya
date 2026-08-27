"""Compatibility facade that aliases ``veya.llm`` to ``veya.obase.llm``.

The module preserves the historical import path while exposing the canonical
implementation object, including attribute access and monkeypatch behavior.
New code should import ``veya.obase.llm`` directly.
"""

import sys

from veya.obase import llm as _impl

sys.modules[__name__] = _impl

"""veya.compat — legacy compatibility facade over the 3O assembly.

Low-level compatibility helpers remain in veya.obase.compat. Public names that
now have canonical omodul implementations are resolved lazily from the mounted
3O main library. This preserves the old veya.compat import surface without
keeping a second implementation in Veya.
"""

from __future__ import annotations

import sys
from typing import Any

from veya.obase import compat as _impl

_CANONICAL_OMODUL_EXPORTS = frozenset(
    {
        "SubagentConfig",
        "SubagentInput",
        "compact_session",
        "execute_tool",
        "init_project",
        "process_prompt",
        "run_subagent",
        "run_subagent_task",
    }
)
_previous_getattr = getattr(_impl, "__getattr__", None)


def _compat_getattr(name: str) -> Any:
    if name in _CANONICAL_OMODUL_EXPORTS:
        from veya.platform import omodul as _load_omodul

        value = getattr(_load_omodul(), name)
        setattr(_impl, name, value)
        return value
    if _previous_getattr is not None:
        return _previous_getattr(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_impl.__getattr__ = _compat_getattr
sys.modules[__name__] = _impl

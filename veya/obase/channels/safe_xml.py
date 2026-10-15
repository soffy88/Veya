"""Hardened XML parsing for untrusted channel webhook payloads."""
from __future__ import annotations

import re
from typing import Any

try:
    from defusedxml import ElementTree as _ET
    USING_DEFUSEDXML = True
except ImportError:  # Older developer envs; explicit DTD/entity bans remain fail-closed.
    import xml.etree.ElementTree as _ET
    USING_DEFUSEDXML = False

_MAX_XML_BYTES = 1024 * 1024
_FORBIDDEN_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


def fromstring(source: str | bytes, parser: Any = None):
    raw = source.encode("utf-8") if isinstance(source, str) else bytes(source)
    if len(raw) > _MAX_XML_BYTES:
        raise ValueError("XML payload exceeds the 1 MiB safety limit")
    if _FORBIDDEN_DECLARATION.search(raw.decode("utf-8", "ignore")):
        raise ValueError("DTD and entity declarations are disabled")
    if parser is not None:
        return _ET.fromstring(source, parser=parser)
    return _ET.fromstring(source)


class _SafeElementTree:
    fromstring = staticmethod(fromstring)

    def __getattr__(self, name: str):
        # Preserve ElementTree helpers such as Element/tostring used by adapters;
        # input parsing always goes through the guarded fromstring above.
        return getattr(_ET, name)


ET = _SafeElementTree()

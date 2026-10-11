"""3O-PURE — parse_runner_mention: leading @<runner-name> extraction.

Pure text parsing for NANO-W6 @runner routing. A mention is an ``@`` followed
by a runner-name token at the very start of the message; the remainder of the
message is the turn text. Matching against the P6 runner registry
(names/aliases, case-insensitive) happens in the service layer.
"""

from __future__ import annotations

import re

_MENTION_RE = re.compile(r"^@([A-Za-z0-9][A-Za-z0-9._-]{0,63})(?:\s+|$)(.*)", re.DOTALL)


def parse_runner_mention(text: str) -> dict[str, str] | None:
    """→ {"name": <matched token>, "rest": <turn text>} or None.

    The name is returned lowercased so the registry lookup is a plain
    case-insensitive compare; surrounding whitespace of ``rest`` is stripped.
    """

    if not text or not text.startswith("@"):
        return None
    match = _MENTION_RE.match(text)
    if match is None:
        return None
    return {"name": match.group(1).lower(), "rest": match.group(2).strip()}


__all__ = ["parse_runner_mention"]

"""3O-PURE — chat_anchors: anchors for read_own_chat (NANO-W10 ADDENDUM N).

Older parts and condensed messages stay in the EXISTING session FTS index. What
the model needs on top of the raw snippet is a stable address it can cite back
to the user: ``part 2 #1234``. That address is pure formatting, so it lives here
and nowhere else.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["ANCHOR_RE", "build_anchor", "parse_anchor", "render_snippet"]

#: ``part <n> #<id>`` — the one spelling used in tool results and in the UI.
ANCHOR_RE = re.compile(r"^part\s+(\d+)\s+#([A-Za-z0-9_.:-]+)$")


def build_anchor(*, part: int, message_id: str) -> str:
    """Render one anchor.

    Args:
        part: 1-based part number of the conversation.
        message_id: the message/turn id inside that part.

    Returns:
        ``"part <n> #<id>"``. Non-numeric ids fall back to a hash-free slug so the
        anchor is always citable back.

    Example:
        >>> build_anchor(part=2, message_id="1234")
        'part 2 #1234'
    """
    number = max(1, int(part or 1))
    ident = "".join(ch for ch in str(message_id or "") if ch.isalnum() or ch in "_.:-")
    return f"part {number} #{ident or 'unknown'}"


def parse_anchor(anchor: str) -> tuple[int, str] | None:
    """Inverse of :func:`build_anchor`; ``None`` when the anchor is malformed.

    Example:
        >>> parse_anchor("part 3 #abc")
        (3, 'abc')
        >>> parse_anchor("nope") is None
        True
    """
    match = ANCHOR_RE.match(str(anchor or "").strip())
    if match is None:
        return None
    return (int(match.group(1)), match.group(2))


def render_snippet(snippet: str, *, anchor: str, max_chars: int = 240) -> dict[str, Any]:
    """Trim a FTS snippet and attach its anchor.

    The provider's ``<mark>`` highlight markers are stripped: they are an FTS
    rendering detail, and leaving them in would leak markup into the model's
    context.

    Args:
        snippet: raw FTS snippet.
        anchor: the anchor for this hit.
        max_chars: maximum returned length.

    ``chars`` counts the characters of the **original** snippet that this row
    represents, not the length of the rendered string: the trailing ellipsis is a
    rendering marker, not content, and counting it would make a row claim to
    carry more of the transcript than it does.

    Returns:
        ``{"anchor", "text", "chars", "truncated"}``.

    Example:
        >>> render_snippet("<mark>hi</mark> there", anchor="part 1 #a")["text"]
        'hi there'
        >>> render_snippet("x" * 500, anchor="part 1 #a", max_chars=10)["chars"]
        10
    """
    text = str(snippet or "").replace("<mark>", "").replace("</mark>", "").strip()
    limit = max(1, int(max_chars))
    truncated = len(text) > limit
    if truncated:
        text = text[:limit].rstrip() + "…"
    return {
        "anchor": anchor,
        "text": text,
        "chars": min(len(text), limit),
        "truncated": truncated,
    }
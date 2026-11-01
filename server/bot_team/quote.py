"""Quote replies (Codync ``replyTo`` method, adapted).

A message may quote one earlier message from the *same* transcript. The
quote is built once, at send time, by the server -- never by the client
-- from the referenced message as stored: ``{id, author, text}`` with the
text capped at :data:`QUOTE_MAX_CHARS`. Quoting a hidden message (a PASS
reply, stored but never shown) is refused, so a quote can never leak one,
and quoting a message from another transcript is impossible by
construction: the caller supplies only the transcript's own messages.

The cap is counted in characters, not bytes: Codync caps at 500 bytes,
which would cut a Chinese quote to a third of an English one. The quote
is a snapshot, not a live reference -- editing or deleting the original
later does not rewrite history.
"""

from __future__ import annotations

from typing import Any

#: Longest quoted text, in characters.
QUOTE_MAX_CHARS = 500


def build_quote(
    messages: list[dict[str, Any]],
    message_id: str,
    *,
    max_chars: int = QUOTE_MAX_CHARS,
) -> dict[str, Any]:
    """Build the quote snapshot for *message_id* from *messages*.

    Raises :class:`ValueError` when the id is unknown in this transcript
    or the target is hidden.
    """

    wanted = str(message_id or "").strip()
    if not wanted:
        raise ValueError("reply_to must name a message id")
    for message in messages:
        if str(message.get("id") or "") != wanted:
            continue
        if message.get("hidden"):
            raise ValueError("cannot quote a hidden message")
        text = str(message.get("text") or "")
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        author = message.get("bot_id") or (
            "user" if message.get("role") == "user" else "bot"
        )
        return {"id": wanted, "author": str(author), "text": text}
    raise ValueError(f"quoted message {wanted!r} not found in this conversation")


__all__ = ["QUOTE_MAX_CHARS", "build_quote"]

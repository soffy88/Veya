"""veya/oskill/pure/channel_ladder.py — ADDENDUM V: web-reading channel ladder.

Pure decision logic for two related ladders:

1. **Read ladder** — how to obtain the content of a web page, cheapest
   structured source first::

       http_fetch → readable_extract → browser_dom → browser_screenshot

   ``http_fetch`` is a plain GET; ``readable_extract`` is the DOM→Markdown
   reader (see :mod:`veya.oskill.pure.html_to_markdown`); ``browser_dom``
   renders with a real browser; ``browser_screenshot`` is the pixels-only
   last resort.

2. **Channel ladder** — which configured chat channel to deliver a long
   read-out through, given per-channel liveness from the real probe
   (``veya.obase.channels.probe``).  Platforms are ordered by delivery
   preference; the chosen channel must be *live* and able to carry the payload
   (chunking is planned, never silent truncation).

Pure: no IO, no network.  Liveness is supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "READ_LADDER",
    "CHANNEL_LADDER",
    "ReadPlan",
    "ChannelPlan",
    "ChannelLadderError",
    "plan_read",
    "plan_channel_delivery",
    "plan_chunks",
]

# Cheapest sufficient → most expensive for *reading a page*.
READ_LADDER: tuple[str, ...] = (
    "http_fetch",
    "readable_extract",
    "browser_dom",
    "browser_screenshot",
)

# Delivery preference for chat channels (matches obase PLATFORMS spelling).
CHANNEL_LADDER: tuple[str, ...] = (
    "telegram",
    "feishu",
    "dingtalk",
    "wecom",
)

# Conservative per-platform single-message character limits (documented caps,
# not exact vendor maxima) used to plan chunking.
CHANNEL_MESSAGE_LIMIT: dict[str, int] = {
    "telegram": 4096,
    "feishu": 4096,
    "dingtalk": 4096,
    "wecom": 2048,
}


class ChannelLadderError(ValueError):
    """Raised when no read strategy or channel is available."""


@dataclass(frozen=True)
class ReadPlan:
    strategy: str
    fallback_strategies: tuple[str, ...]
    reason: str

    def next_after_failure(self) -> "ReadPlan | None":
        if not self.fallback_strategies:
            return None
        head, *rest = self.fallback_strategies
        return ReadPlan(
            strategy=head,
            fallback_strategies=tuple(rest),
            reason=f"fallback after {self.strategy}",
        )


@dataclass(frozen=True)
class ChannelPlan:
    platform: str
    message_limit: int
    chunk_count: int
    reason: str


def plan_read(available: Mapping[str, bool] | Sequence[str]) -> ReadPlan:
    """Pick the cheapest available read strategy and expose the fallback chain."""
    if isinstance(available, Mapping):
        avail = {k for k, ok in available.items() if ok}
    else:
        avail = set(available)
    known = [s for s in READ_LADDER if s in avail]
    if not known:
        raise ChannelLadderError(
            "no read strategy available (need one of " + ", ".join(READ_LADDER) + ")"
        )
    return ReadPlan(
        strategy=known[0],
        fallback_strategies=tuple(known[1:]),
        reason="read ladder",
    )


def plan_chunks(text: str, limit: int) -> list[str]:
    """Split ``text`` into ordered chunks of at most ``limit`` characters.

    Splits prefer paragraph/line/space boundaries so read-outs stay readable.
    Never drops content; a hard split is used only when a single token
    exceeds the limit.
    """
    if limit <= 0:
        raise ChannelLadderError("message limit must be positive")
    text = text or ""
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = max(
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind(" "),
        )
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def plan_channel_delivery(
    text: str,
    live_channels: Mapping[str, bool] | Sequence[str],
    *,
    preference: Sequence[str] = CHANNEL_LADDER,
) -> ChannelPlan:
    """Choose the first live preferred channel able to carry ``text``.

    Raises :class:`ChannelLadderError` when no preferred channel is live.
    """
    if isinstance(live_channels, Mapping):
        live = {k for k, ok in live_channels.items() if ok}
    else:
        live = set(live_channels)
    chosen = next((p for p in preference if p in live), None)
    if chosen is None:
        raise ChannelLadderError(
            "no live channel among " + ", ".join(preference)
        )
    limit = CHANNEL_MESSAGE_LIMIT.get(chosen, 4096)
    chunks = plan_chunks(text, limit)
    return ChannelPlan(
        platform=chosen,
        message_limit=limit,
        chunk_count=len(chunks),
        reason=f"channel ladder (live: {', '.join(sorted(live))})",
    )

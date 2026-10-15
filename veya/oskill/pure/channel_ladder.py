"""veya/oskill/pure/channel_ladder.py — ADDENDUM V: web-reading channel ladder.

Pure decision logic for three related ladders:

1. **Read ladder** — how to obtain the content of a web page, cheapest
   structured source first:
       http_fetch → readable_extract → browser_dom → browser_screenshot

2. **Web-reading channel ladder** (ADDENDUM V) — per-channel ordered backends:
       web:          jina → dom_reader → http_fetch
       youtube:      yt_dlp → browser
       rss:          feedparser → http_fetch
       github:       gh_cli → api
       bilibili:     cli_bilibili → browser
       reddit:       cli_reddit → browser
       twitter:      cli_twitter → browser
       xiaohongshu:  cli_xiaohongshu → browser
       linkedin:     cli_linkedin → browser
   Changing backend = reordering the list, not code.

3. **Channel delivery ladder** — which configured chat channel to deliver a long
   read-out through (telegram, feishu, dingtalk, wecom).

Pure: no IO, no network. Liveness is supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "CHANNEL_LADDER",
    "CHANNEL_MESSAGE_LIMIT",
    "LOGIN_REQUIRED_BACKENDS",
    "READ_LADDER",
    "WEB_READING_CHANNELS",
    "ChannelLadderError",
    "ChannelPlan",
    "ReadPlan",
    "WebChannelPlan",
    "plan_channel_delivery",
    "plan_chunks",
    "plan_read",
    "plan_web_channel_backend",
    "reorder_channel_backends",
]

# Cheapest sufficient → most expensive for *reading a page*.
READ_LADDER: tuple[str, ...] = (
    "http_fetch",
    "readable_extract",
    "browser_dom",
    "browser_screenshot",
)

# Ordered backend ladders for reading external sources per ADDENDUM V:
WEB_READING_CHANNELS: dict[str, tuple[str, ...]] = {
    "web": ("jina", "dom_reader", "http_fetch"),
    "youtube": ("yt_dlp", "browser"),
    "rss": ("feedparser", "http_fetch"),
    "github": ("gh_cli", "api"),
    "bilibili": ("cli_bilibili", "browser"),
    "reddit": ("cli_reddit", "browser"),
    "twitter": ("cli_twitter", "browser"),
    "xiaohongshu": ("cli_xiaohongshu", "browser"),
    "linkedin": ("cli_linkedin", "browser"),
}

# Backends that require credentials/cookies (disabled by default until approved)
LOGIN_REQUIRED_BACKENDS: frozenset[str] = frozenset(
    {
        "cli_xiaohongshu",
        "cli_linkedin",
        "cli_twitter",
    }
)

# Delivery preference for chat channels (matches obase PLATFORMS spelling).
CHANNEL_LADDER: tuple[str, ...] = (
    "telegram",
    "feishu",
    "dingtalk",
    "wecom",
)

# Conservative per-platform single-message character limits
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

    def next_after_failure(self) -> ReadPlan | None:
        if not self.fallback_strategies:
            return None
        head, *rest = self.fallback_strategies
        return ReadPlan(
            strategy=head,
            fallback_strategies=tuple(rest),
            reason=f"fallback after {self.strategy}",
        )


@dataclass(frozen=True)
class WebChannelPlan:
    channel: str
    active_backend: str
    fallback_backends: tuple[str, ...]
    needs_login: bool = False
    reason: str = ""

    def next_after_failure(self) -> WebChannelPlan | None:
        if not self.fallback_backends:
            return None
        head, *rest = self.fallback_backends
        return WebChannelPlan(
            channel=self.channel,
            active_backend=head,
            fallback_backends=tuple(rest),
            needs_login=head in LOGIN_REQUIRED_BACKENDS,
            reason=f"fallback after {self.active_backend}",
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


def plan_web_channel_backend(
    channel: str,
    available_backends: Mapping[str, bool] | Sequence[str],
    *,
    custom_order: Sequence[str] | None = None,
) -> WebChannelPlan:
    """Select the active backend for a web reading channel according to its ladder.

    Raises ChannelLadderError if channel is unknown or no backend is available.
    """
    if channel not in WEB_READING_CHANNELS:
        raise ChannelLadderError(f"unknown web reading channel '{channel}'")

    ladder = custom_order if custom_order is not None else WEB_READING_CHANNELS[channel]

    if isinstance(available_backends, Mapping):
        avail = {k for k, ok in available_backends.items() if ok}
    else:
        avail = set(available_backends)

    known = [b for b in ladder if b in avail]
    if not known:
        raise ChannelLadderError(
            f"no backend available for channel '{channel}' (ladder: {', '.join(ladder)})"
        )

    chosen = known[0]
    return WebChannelPlan(
        channel=channel,
        active_backend=chosen,
        fallback_backends=tuple(known[1:]),
        needs_login=chosen in LOGIN_REQUIRED_BACKENDS,
        reason=f"channel ladder for {channel}",
    )


def reorder_channel_backends(
    channel: str,
    new_order: Sequence[str],
) -> tuple[str, ...]:
    """Pure reordering of backends for a channel. Changing backend = reordering list, not code."""
    if channel not in WEB_READING_CHANNELS:
        raise ChannelLadderError(f"unknown web reading channel '{channel}'")
    current = set(WEB_READING_CHANNELS[channel])
    for item in new_order:
        if item not in current:
            raise ChannelLadderError(f"backend '{item}' is not valid for channel '{channel}'")
    ordered = tuple(new_order) + tuple(
        b for b in WEB_READING_CHANNELS[channel] if b not in new_order
    )
    return ordered


def plan_chunks(text: str, limit: int) -> list[str]:
    """Split ``text`` into ordered chunks of at most ``limit`` characters."""
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
    """Choose the first live preferred channel able to carry ``text``."""
    if isinstance(live_channels, Mapping):
        live = {k for k, ok in live_channels.items() if ok}
    else:
        live = set(live_channels)
    chosen = next((p for p in preference if p in live), None)
    if chosen is None:
        raise ChannelLadderError("no live channel among " + ", ".join(preference))
    limit = CHANNEL_MESSAGE_LIMIT.get(chosen, 4096)
    chunks = plan_chunks(text, limit)
    return ChannelPlan(
        platform=chosen,
        message_limit=limit,
        chunk_count=len(chunks),
        reason=f"channel ladder (live: {', '.join(sorted(live))})",
    )

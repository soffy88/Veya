"""server/channels/base.py — Channel adapter common interface (Layer 4).

A channel adapter is the transport-specific half of NANO-W7:

    verify inbound webhook signature
      -> map external user/chat -> Veya user + bot/group
        -> send reply (chunking long replies)

Framework-free by design: HTTP wiring lives in ``server/routes/channels.py``
so the adapters stay unit-testable with no FastAPI/TestClient involved.

Composition only: adapters use the obase HTTP/crypto clients and the pure
oskill formatters, and never reimplement either (§1.4 single source).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from veya.obase.channels import ChannelMessage, chunk_text

_log = logging.getLogger(__name__)


class SignatureRejected(Exception):
    """Raised when an inbound webhook fails signature verification."""


class ChannelAdapter(ABC):
    """Common interface every chat platform adapter implements."""

    #: Stable platform identifier, matching veya.obase.channels constants.
    platform: str = ""

    @abstractmethod
    def verify(
        self, *, payload: Mapping[str, Any], headers: Mapping[str, str], query: Mapping[str, str]
    ) -> bool:
        """Return True when the inbound webhook is authenticated.

        Implementations must fail closed: an unconfigured secret means the
        webhook cannot be authenticated, which is False, not True.
        """

    @abstractmethod
    def parse_inbound(
        self, *, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> ChannelMessage | None:
        """Normalise a platform payload into a :class:`ChannelMessage`.

        Returns None when the payload is not a user message (delivery
        receipts, non-text messages, handshakes).
        """

    @abstractmethod
    async def send_reply(self, client: Any, *, external_chat_id: str, text: str) -> dict[str, Any]:
        """Send (and chunk) a reply through ``client``.

        ``client`` is the obase platform client for this platform; it is
        passed in rather than constructed here so credentials stay owned by
        the service layer.
        """

    # ---- shared helpers -------------------------------------------------

    def check(self, **kwargs: Any) -> None:
        """Verify or raise :class:`SignatureRejected`."""
        if not self.verify(**kwargs):
            raise SignatureRejected(f"{self.platform}: signature verification failed")

    @staticmethod
    def chunk_for_platform(text: str, *, platform: str, max_len: int | None = None) -> list[str]:
        """Split a long reply into platform-sized chunks (delegates to obase)."""
        return chunk_text(text, platform=platform, max_len=max_len)

    @staticmethod
    def empty_reply() -> dict[str, Any]:
        """Uniform no-op acknowledgement for platforms that expect a 200."""
        return {"ok": True}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} platform={self.platform!r}>"

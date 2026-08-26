"""Canonical identifiers for user-facing Veya sessions."""

from __future__ import annotations

import uuid


def new_session_id() -> str:
    """Return a sortable, entry-point-independent session identifier."""
    uuid7 = getattr(uuid, "uuid7", None)
    value = uuid7() if uuid7 is not None else uuid.uuid4()
    return f"sess_{value}"

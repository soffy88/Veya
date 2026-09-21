"""Explicit state and identity mapping; no Mission state mutation lives here."""

from __future__ import annotations

import hashlib

from .errors import CfKanbanErrorClass, CfKanbanProviderError, UnsupportedOperation

CFKANBAN_REOPEN_OPERATION = "NOT_SUPPORTED"
_KNOWN_STATES = frozenset({"backlog", "todo", "in_progress", "canceled", "done"})


def to_provider_state(state: str) -> str:
    if state not in _KNOWN_STATES:
        raise CfKanbanProviderError(
            CfKanbanErrorClass.INVALID_STATE,
            f"Unknown cfKanban state: {state}",
            provider_code="UNKNOWN_STATE",
        )
    return state


def from_provider_state(state: str) -> str:
    # This is deliberately a provider-state projection, not a Veya Mission
    # projection.  In particular, provider ``done`` never means ACCEPTED.
    return to_provider_state(state)


def reopen(*_args: object, **_kwargs: object) -> None:
    raise UnsupportedOperation("reopen")


def derive_idempotency_key(
    mission_id: str, execution_id: str, operation: str, logical_identity: str
) -> str:
    material = "\x1f".join((mission_id, execution_id, operation, logical_identity))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"veya-cfk-{digest}"[:128]

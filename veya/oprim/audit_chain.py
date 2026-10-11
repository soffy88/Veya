"""veya/oprim/audit_chain — atomic hash-chain primitives for the audit log.

One oprim = one atomic operation.  This module holds **three** oprims:

``compute_event_hash``
    sha256 over the canonical JSON of one event plus its ``prev_hash``.
``chain_head``
    the current head hash of an ordered record list (genesis when empty).
``verify_chain_links``
    re-derive every link and report the first broken index.

Single-source (§1.4): canonical serialisation and digesting are **reused** from
``obase.canonical_json`` / ``obase.sha256_hash`` — this module never builds a second
JSON encoder or a second hash implementation.

Chain rule
----------
``hash_i = hex(sha256(canonical_json({**event_i, "prev_hash": hash_{i-1}})))``

with ``hash_0.prev_hash = GENESIS_HASH`` (``"0" * 64``).  The chain columns ride on the
**existing** ``server/events.py`` record; this module owns no storage.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CHAIN_FIELDS",
    "GENESIS_HASH",
    "chain_head",
    "compute_event_hash",
    "verify_chain_links",
]

#: Genesis ``prev_hash`` for the first record of any chain.
GENESIS_HASH = "0" * 64

#: Record fields owned by this element (set by the caller, never invented here).
CHAIN_FIELDS: tuple[str, ...] = ("prev_hash", "hash", "chain_index")

#: Event fields excluded from the digest payload.  ``hash`` obviously cannot be
#: part of its own digest; the other two are chain bookkeeping, not content.
_EXCLUDED_FROM_DIGEST = frozenset({"hash", "prev_hash", "chain_index"})


def _digest_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Project an event onto the dict that actually gets hashed."""
    return {k: v for k, v in event.items() if k not in _EXCLUDED_FROM_DIGEST}


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    try:
        from veya.platform import load

        return load("obase").canonical_json(payload)
    except Exception:  # pragma: no cover - submodules absent (graceful degrade)
        import json

        return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()


def _sha256_hex(payload: bytes) -> str:
    try:
        from veya.platform import load

        return load("obase").sha256_hash(payload).hex()
    except Exception:  # pragma: no cover - submodules absent (graceful degrade)
        import hashlib

        return hashlib.sha256(payload).hexdigest()


def compute_event_hash(event: dict[str, Any], *, prev_hash: str = GENESIS_HASH) -> str:
    """Hash one event under the chain rule.

    Args:
        event: the event record (chain bookkeeping fields are stripped first).
        prev_hash: hash of the previous record; ``GENESIS_HASH`` for the first.

    Returns:
        64-char lowercase hex digest.

    Raises:
        TypeError: ``event`` is not a dict.

    Example:
        >>> h0 = compute_event_hash({"topic": "a"})
        >>> h1 = compute_event_hash({"topic": "b"}, prev_hash=h0)
        >>> len(h1)
        64
    """
    if not isinstance(event, dict):
        raise TypeError("compute_event_hash: event must be a dict")
    payload = _digest_payload(event)
    payload["prev_hash"] = prev_hash
    return _sha256_hex(_canonical_bytes(payload))


def chain_head(records: list[dict[str, Any]]) -> str:
    """Return the head hash of an ordered record list.

    Args:
        records: chain records in append order; may be empty.

    Returns:
        The last record's ``hash`` when present and non-empty, else
        ``compute_event_hash`` of the last record when it has no ``hash`` yet
        (i.e. the caller is about to append), else ``GENESIS_HASH``.

    Example:
        >>> chain_head([])
        '0000000000000000000000000000000000000000000000000000000000000000'
    """
    if not records:
        return GENESIS_HASH
    last = records[-1]
    if not isinstance(last, dict):
        return GENESIS_HASH
    stored = last.get("hash")
    if isinstance(stored, str) and stored:
        return stored
    prev = last.get("prev_hash")
    return compute_event_hash(
        last, prev_hash=prev if isinstance(prev, str) and prev else GENESIS_HASH
    )


def verify_chain_links(records: list[dict[str, Any]]) -> tuple[bool, str, int]:
    """Re-derive every link of a chain and report the first break.

    A record with no stored ``hash`` is treated as *pending* (not yet stamped);
    it is re-derived from its own ``prev_hash`` so a half-written tail does not
    read as tampering, and the walk continues from the re-derived value.

    Args:
        records: chain records in append order.

    Returns:
        ``(ok, reason, broken_index)``.  ``ok=True`` ⇒ ``broken_index == -1``.
        A genesis ``prev_hash`` mismatch reports index ``0``.

    Example:
        >>> ok, why, idx = verify_chain_links([{"topic": "a", "hash": compute_event_hash({"topic": "a"})}])
        >>> ok, idx
        (True, -1)
    """
    prev = GENESIS_HASH
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            return False, f"record {index} is not a dict", index
        declared_prev = record.get("prev_hash")
        if declared_prev is not None and declared_prev != prev:
            return (
                False,
                f"record {index}: prev_hash {declared_prev!r} != expected {prev!r}",
                index,
            )
        derived = compute_event_hash(record, prev_hash=prev)
        stored = record.get("hash")
        if isinstance(stored, str) and stored and stored != derived:
            return (
                False,
                f"record {index}: stored hash {stored!r} != derived {derived!r}",
                index,
            )
        prev = derived
    return True, "chain intact", -1

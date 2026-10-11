"""veya/oprim/turn_envelope — inbound-message envelope atoms (NANO-W10 §1).

Two atomic digests over one inbound turn envelope:

* :func:`idem_key` — the per-inbound-message idempotency key. A client retry
  produces the same key, so the queue drops the duplicate instead of opening a
  second turn.
* :func:`envelope_fingerprint` — the dedup digest of one envelope's
  identity fields.

One op per function, no oprim→oprim calls, no decision trail: the trail is
written by the omodul above. Both reuse the canonical hash atom
(:func:`veya.oprim.audit_chain.compute_event_hash`) rather than re-deriving a
digest, per the single-source rule (§1.4).
"""

from __future__ import annotations

from typing import Any

from veya.oprim.audit_chain import GENESIS_HASH, compute_event_hash


def idem_key(
    conversation_ref: str,
    source: str,
    external_id: str,
    *,
    salt: str = "",
) -> str:
    """Stable idempotency key for one inbound message.

    Args:
        conversation_ref: stable non-PII conversation reference.
        source: where the message came from ("user", "group", "channel", ...).
        external_id: the provider's own message id, when it has one.
        salt: optional deployment salt.

    Returns:
        24 hex characters. Two deliveries of the same message (client retry,
        gateway redelivery) yield the same key; a genuinely new message does not.

    Example:
        >>> idem_key("c1", "user", "m42") == idem_key("c1", "user", "m42")
        True
    """

    payload = {
        "conversation_ref": str(conversation_ref or ""),
        "source": str(source or ""),
        "external_id": str(external_id or ""),
        "salt": str(salt or ""),
    }
    return compute_event_hash(payload, prev_hash=GENESIS_HASH)[:24]


def envelope_fingerprint(envelope: dict[str, Any], fields: tuple[str, ...]) -> str:
    """Digest over the named identity fields of one envelope.

    Args:
        envelope: the inbound envelope.
        fields: which keys take part in the digest.

    Returns:
        24 hex characters; an empty field tuple yields an empty string rather
        than a digest over nothing.

    Example:
        >>> envelope_fingerprint({"a": 1}, ()) 
        ''
    """

    if not fields:
        return ""
    payload = {name: envelope.get(name) for name in sorted(fields)}
    return compute_event_hash(payload, prev_hash=GENESIS_HASH)[:24]


__all__ = ["envelope_fingerprint", "idem_key"]
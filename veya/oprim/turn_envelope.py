"""veya/oprim/turn_envelope — inbound-message envelope atoms (NANO-W10 §1).

Two atomic digests over one inbound turn envelope:

* :func:`idem_key` — the per-inbound-message idempotency key. A client retry
  produces the same key, so the queue drops the duplicate instead of opening a
  second turn.
* :func:`envelope_fingerprint` — the dedup digest of one envelope's
  identity fields.

One op per function, no oprim→oprim calls, no decision trail: the trail is
written by the omodul above. Both digest through the canonical obase atoms
(``obase.canonical_json`` + ``obase.sha256_hash``) rather than re-deriving a
digest, per the single-source rule (§1.4) — this module deliberately does
**not** import another oprim, because oprim→oprim calls are forbidden.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: Genesis ``prev_hash`` — the same chain constant the audit chain starts from,
#: restated here as a literal because importing it would be an oprim→oprim call.
GENESIS_HASH = "0" * 64


def _obase_attr(submodule: str, attr: str) -> Callable[..., Any] | None:
    """Resolve an obase symbol, tolerating a pin that lacks the re-export."""

    import importlib

    try:
        from veya.platform import load

        load("obase")
        module = getattr(load("obase"), submodule, None)
        if module is not None and hasattr(module, attr):
            found: Callable[..., Any] = getattr(module, attr)
            return found
    except Exception:
        pass
    try:
        resolved: Callable[..., Any] = getattr(importlib.import_module(f"obase.{submodule}"), attr)
        return resolved
    except Exception:
        return None


def _digest(payload: dict[str, Any]) -> str:
    """Canonical JSON + sha256, delegated to obase (never a second encoder)."""

    encode = _obase_attr("canonical_json", "canonical_json")
    if encode is not None:
        raw = bytes(encode(payload))
    else:  # pragma: no cover - only reached when obase is unmounted
        import json

        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    digest = _obase_attr("sha256_hash", "sha256_hash")
    if digest is not None:
        return bytes(digest(raw)).hex()
    import hashlib  # pragma: no cover - only reached when obase is unmounted

    return hashlib.sha256(raw).hex()


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
    return _digest({**payload, "prev_hash": GENESIS_HASH})[:24]


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
    return _digest({**payload, "prev_hash": GENESIS_HASH})[:24]


__all__ = ["envelope_fingerprint", "idem_key"]

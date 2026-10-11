"""server/nano_w9/audit_chain — hash-chained audit over the EXISTING event store.

NANO-W9 element 4.  The rule is::

    hash_i = hex(sha256(canonical_json({**event_i, "prev_hash": hash_{i-1}})))

computed by :mod:`veya.oprim.audit_chain` (which reuses ``obase.canonical_json``
and ``obase.sha256_hash`` — single source, no second JSON/hash path).

**No new event store.**  The three chain columns (``prev_hash`` / ``hash`` /
``chain_index``) ride on the record that ``server/events.py`` already appends, so
everything that already reads events keeps working unchanged.

Coverage wired here (per spec): group messages, tool side-effects, approvals and
git events — each is a topic filter over the same log, not a separate log.
"""

from __future__ import annotations

from typing import Any

from veya.oprim.audit_chain import (
    GENESIS_HASH,
    chain_head,
    compute_event_hash,
    verify_chain_links,
)

__all__ = [
    "AUDITED_TOPICS",
    "CHAINED_TOPIC",
    "audit_envelopes",
    "audit_records",
    "chained_topic",
    "stamp_event",
    "verify_audit_chain",
]

#: Topic under which chained audit records are appended.  It is deliberately a
#: *new* topic so the existing lifecycle projections do not have to learn about
#: chain columns, and it points at the original record via ``payload.source_topic``.
CHAINED_TOPIC = "audit.chained"

#: The topics that get chained.  Everything else is appended unchanged (an
#: operator can widen this without a migration).
AUDITED_TOPICS: frozenset[str] = frozenset(
    {
        "group.message",
        "action_gateway.audit",
        "side_effect.declared",
        "side_effect.started",
        "side_effect.committed",
        "side_effect.failed",
        "tool.approval_required",
        "tool.approved",
        "tool.denied",
        "git.event",
    }
)


def _read_records(topic_filter: set[str] | None = None) -> list[dict[str, Any]]:
    """Read the existing JSONL event log, optionally filtered by topic."""
    from server.events import event_store

    return list(event_store.read_all(topics=topic_filter) or [])


def audit_records(topic_filter: set[str] | None = None) -> list[dict[str, Any]]:
    """Return the chain-bearing records, in append order.

    Chain columns are persisted inside ``payload.audit`` of an ordinary
    ``audit.chained`` envelope, so the raw event log stays a valid
    ``EventStore`` stream.  This projects those columns back out **pristine** —
    verification re-derives the digest from exactly these keys, so adding any
    display field here would corrupt it (see :func:`audit_envelopes` for the
    display-friendly join).

    Args:
        topic_filter: optional topic allowlist applied to raw events.

    Returns:
        ``[{source_topic, actor, payload, chain_index, prev_hash, hash}]``.
    """
    chained: list[dict[str, Any]] = []
    for envelope in _read_records({CHAINED_TOPIC}):
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        audit = payload.get("audit") if isinstance(payload, dict) else None
        if isinstance(audit, dict) and audit.get("hash"):
            chained.append(dict(audit))
    if chained:
        return chained
    # Fallback: a chain written directly onto the envelope by an older build.
    return [
        event
        for event in _read_records(topic_filter)
        if isinstance(event, dict) and ("hash" in event or "prev_hash" in event)
    ]


def audit_envelopes(limit: int = 50) -> list[dict[str, Any]]:
    """Display-friendly join of chain records with their envelope metadata.

    Args:
        limit: maximum rows returned (newest last).

    Returns:
        ``[{"event_id", "ts", "chain_index", "source_topic", "hash", "body"}]``
        — for a replay/verify UI; **never** feed this to the verifier.
    """
    rows: list[dict[str, Any]] = []
    for envelope in _read_records({CHAINED_TOPIC}):
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(payload, dict):
            continue
        audit = payload.get("audit")
        if not isinstance(audit, dict):
            continue
        body = {k: v for k, v in payload.items() if k != "audit"}
        rows.append(
            {
                "event_id": envelope.get("event_id"),
                "ts": envelope.get("ts"),
                "chain_index": audit.get("chain_index"),
                "source_topic": audit.get("source_topic"),
                "hash": audit.get("hash"),
                "body": body,
            }
        )
    rows.sort(key=lambda r: (r.get("chain_index") or 0, r.get("ts") or 0))
    return rows[-max(0, limit) :]


def chained_topic(original_topic: str) -> str:
    """Topic a given original topic is chained under.

    Args:
        original_topic: the topic of the fact being audited.

    Returns:
        :data:`CHAINED_TOPIC` when the topic is audited, else the topic itself
        (so callers can use the return value unconditionally).

    Example:
        >>> chained_topic("git.event") == CHAINED_TOPIC
        True
        >>> chained_topic("chat.token")
        'chat.token'
    """
    return CHAINED_TOPIC if original_topic in AUDITED_TOPICS else original_topic


def stamp_event(
    topic: str,
    payload: dict[str, Any] | None = None,
    *,
    actor: str = "system",
    audit_topic_writer: Any = None,
) -> dict[str, Any]:
    """Append one fact to the existing event store, chained.

    Args:
        topic: the fact's topic (must be in :data:`AUDITED_TOPICS` to be chained).
        payload: the fact body (never contains real user PII; the caller is
            expected to have pseudonymized identifiers already).
        actor: who produced the fact.
        audit_topic_writer: injection seam for tests/alternate stores; defaults
            to ``server.events.append_canonical_event``.

    Returns:
        The appended envelope, with the chain columns merged in.  Non-audited
        topics are appended unchanged and returned as-is.

    Example:
        >>> # env = stamp_event("git.event", {"branch": "feat/x"})
    """
    from server.events import append_canonical_event

    body = dict(payload or {})
    writer = audit_topic_writer or append_canonical_event

    if topic not in AUDITED_TOPICS:
        return writer(topic, body, actor=actor)

    previous = audit_records()
    prev_hash = chain_head(previous)
    record = {
        "source_topic": topic,
        "actor": actor,
        "payload": body,
        "chain_index": len(previous),
    }
    digest = compute_event_hash(record, prev_hash=prev_hash)
    chained = {
        **record,
        "prev_hash": prev_hash,
        "hash": digest,
    }
    envelope = writer(CHAINED_TOPIC, {**body, "audit": chained}, actor=actor)
    if isinstance(envelope, dict):
        envelope.update(chained)
    return envelope


def verify_audit_chain(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Verify the chain end to end.

    Re-derives every link from the raw records; a stored hash is never trusted.

    Args:
        records: explicit records to verify; ``None`` reads the store.

    Returns:
        ``{"status", "ok", "reason", "broken_index", "length", "head"}``.
        ``status`` is ``"ok"`` / ``"broken"`` / ``"empty"`` — never an exception.

    Example:
        >>> verify_audit_chain([])["status"]
        'empty'
    """
    chain = list(records) if records is not None else audit_records()
    if not chain:
        return {
            "status": "empty",
            "ok": True,
            "reason": "no chained records",
            "broken_index": -1,
            "length": 0,
            "head": GENESIS_HASH,
        }
    ok, reason, index = verify_chain_links(chain)
    return {
        "status": "ok" if ok else "broken",
        "ok": ok,
        "reason": reason,
        "broken_index": index,
        "length": len(chain),
        "head": chain[-1].get("hash", GENESIS_HASH),
    }

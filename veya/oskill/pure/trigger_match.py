"""3O-PURE — trigger_match: event-trigger predicates for routines.

Pure predicates that decide whether an inbound *signal* fires a routine trigger.
Three trigger kinds (NANO-W9 spec §2):

``webhook``
    already verified upstream by ``obase.webhook.sign_payload``; the pure part is
    only the header/event-name allowlist check.
``message``
    regex search over the message body, or an explicit mention/handle list.
``reaction``
    an emoji/key on a specific message, optionally restricted to a bot author.

Deliberately **not** here: HMAC verification (crypto = obase), routing/dispatch
(a business decision = omodul), and any registry lookup (IO = oprim/layer4).
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "TRIGGER_KINDS",
    "match_message_trigger",
    "match_reaction_trigger",
    "match_webhook_trigger",
    "signal_matches_triggers",
]

#: The only trigger kinds this element understands.
TRIGGER_KINDS: tuple[str, ...] = ("webhook", "message", "reaction")

_DEFAULT_TIMEOUT_S = 300.0


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v)]
    return [str(value)]


def match_webhook_trigger(signal: dict[str, Any], trigger: dict[str, Any]) -> bool:
    """Does a verified webhook signal satisfy this webhook trigger?

    Args:
        signal: ``{"kind": "webhook", "event": str, "verified": bool, ...}``.
        trigger: ``{"kind": "webhook", "event": "git.tag", "source": "github"}``.

    Returns:
        True only when the signal was cryptographically verified upstream
        **and** the declared event/source are in the trigger's allowlist.
        An unverified signal never matches (fail-closed).

    Example:
        >>> match_webhook_trigger({"kind": "webhook", "event": "push", "verified": True},
        ...                      {"kind": "webhook", "event": "push"})
        True
        >>> match_webhook_trigger({"kind": "webhook", "event": "push", "verified": False},
        ...                      {"kind": "webhook", "event": "push"})
        False
    """
    if not signal.get("verified"):
        return False
    events = _as_list(trigger.get("events") or trigger.get("event"))
    if events and signal.get("event") not in events:
        return False
    sources = _as_list(trigger.get("sources") or trigger.get("source"))
    return not (sources and signal.get("source") not in sources)


def match_message_trigger(signal: dict[str, Any], trigger: dict[str, Any]) -> bool:
    """Does a group/bot message satisfy this message trigger?

    Mention matching is OR: any handle in ``mentions`` appearing in the body
    matches.  Regex matching is a ``re.search`` (first hit anywhere).

    Args:
        signal: ``{"kind": "message", "text": str, "author": str, ...}``.
        trigger: ``{"kind": "message", "mentions": [...], "regex": str}``.

    Returns:
        True when at least one configured condition matches.  A trigger with no
        condition at all never matches (fail-closed, never match-all).

    Example:
        >>> match_message_trigger({"kind": "message", "text": "hey @ci fix it"},
        ...                       {"kind": "message", "mentions": ["@ci"]})
        True
        >>> match_message_trigger({"kind": "message", "text": "release v1"},
        ...                       {"kind": "message", "regex": r"^release v\\d+$"})
        True
    """
    text = str(signal.get("text") or signal.get("pattern") or signal.get("content") or "")
    if not text:
        return False
    matched = False
    mentions = _as_list(trigger.get("mentions"))
    if mentions:
        matched = any(m in text for m in mentions)
    pattern = trigger.get("regex") or trigger.get("pattern")
    if not matched and isinstance(pattern, str) and pattern:
        try:
            matched = re.search(pattern, text) is not None
        except re.error:
            matched = False

    sources = _as_list(trigger.get("sources") or trigger.get("source"))
    if sources:
        sig_source = str(signal.get("source") or "")
        if sig_source not in sources:
            return False
        if not mentions and not pattern:
            matched = True

    return matched


def match_reaction_trigger(signal: dict[str, Any], trigger: dict[str, Any]) -> bool:
    """Does an inbound reaction satisfy this reaction trigger?

    Args:
        signal: ``{"kind": "reaction", "reaction": "👍", "message_id": str,
                  "author": str, "message_author": str}``.
        trigger: ``{"kind": "reaction", "reaction": ["👍"], "action": "approve",
                  "on_message_author": "bot"}``.

    Returns:
        True when the reaction key matches and, when
        ``on_message_author`` is set, the reacted-to message was written by
        that author (e.g. only a bot's own message can be approved).

    Example:
        >>> match_reaction_trigger(
        ...     {"kind": "reaction", "reaction": "👍", "message_author": "bot"},
        ...     {"kind": "reaction", "reaction": "👍", "on_message_author": "bot"})
        True
    """
    wanted = _as_list(trigger.get("reaction") or trigger.get("reactions"))
    if not wanted:
        return False
    if str(signal.get("reaction") or "") not in wanted:
        return False
    author_filter = trigger.get("on_message_author")
    return not (author_filter and str(signal.get("message_author") or "") != str(author_filter))


def signal_matches_triggers(signal: dict[str, Any], triggers: list[dict[str, Any]]) -> list[int]:
    """Return the indices of every trigger in ``triggers`` that fires.

    Unknown trigger kinds are skipped (fail-closed) so a typo can never widen
    the blast radius of a signal.

    Args:
        signal: the inbound signal dict (``kind`` selects the matcher).
        triggers: trigger dicts, each with its own ``kind``.

    Returns:
        Ascending list of matching indices; empty when nothing fires.

    Example:
        >>> signal_matches_triggers(
        ...     {"kind": "reaction", "reaction": "👍"},
        ...     [{"kind": "message", "mentions": ["@x"]}, {"kind": "reaction", "reaction": "👍"}])
        [1]
    """
    kind = str(signal.get("kind") or "")
    matchers = {
        "webhook": match_webhook_trigger,
        "message": match_message_trigger,
        "reaction": match_reaction_trigger,
    }
    matcher = matchers.get(kind)
    if matcher is None:
        return []
    hits: list[int] = []
    for index, trigger in enumerate(triggers or []):
        if not isinstance(trigger, dict):
            continue
        try:
            if matcher(signal, trigger):
                hits.append(index)
        except (re.error, TypeError, ValueError):
            continue
    return hits

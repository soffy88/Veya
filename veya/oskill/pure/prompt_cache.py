"""3O-PURE — prompt_cache: prompt-cache discipline (NANO-W10 item 4).

Provider prompt caches are keyed on **prefix identity**. Four pure algorithms
make that a property of the assembly rather than a hope:

``ordered_sections``
    render named sections in the one fixed order (``system``, ``tools``,
    ``core_memory``, ``scratch``, then ``conversation``) and stamp each with a
    content digest, so a caller can prove nothing upstream of the conversation
    moved between two calls.

``frozen_tool_set`` / ``toolset_unchanged``
    reduce the tool list to an immutable name tuple plus one digest. The turn
    freezes it; ``tool_search`` results ride as messages, so the digest moving
    is the signal that the cache prefix was broken.

``usage_cache_rate``
    aggregate per-call records into a hit rate over *input tokens*, not calls.

``should_compact_idle`` / ``prewarm_due``
    the two timing judgements (compact while the cache is still warm; warm on a
    debounced typing signal). Both take ``now`` as an argument — this layer may
    not read a clock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from veya.oprim.prompt_cache import (
    PROMPT_SECTION_NAMES,
    cached_token_split,
    section_digest,
)

__all__ = [
    "DEFAULT_COMPACT_THRESHOLD_RATIO",
    "DEFAULT_PREWARM_DEBOUNCE_SECONDS",
    "SECTION_ORDER",
    "assemble_messages",
    "frozen_tool_set",
    "ordered_sections",
    "prewarm_due",
    "should_compact_idle",
    "toolset_unchanged",
    "usage_cache_rate",
]

#: The one fixed order. Tuple order is the contract (see prompt_cache oprim).
SECTION_ORDER: tuple[str, ...] = PROMPT_SECTION_NAMES

#: Compact when the conversation reaches this fraction of the usable window.
DEFAULT_COMPACT_THRESHOLD_RATIO: float = 0.7

#: Collapse repeated typing signals into at most one prewarm per this many seconds.
DEFAULT_PREWARM_DEBOUNCE_SECONDS: float = 2.0


def ordered_sections(
    sections: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Order named prompt sections and stamp each with its content digest.

    Unknown section names are **dropped**, not appended: a section this
    assembler does not know about would land at an unpredictable position and
    silently break the prefix contract. Missing known sections render as empty,
    so the ordering is stable regardless of which layers produced content.

    Args:
        sections: ``[(name, text), ...]`` in any order; later duplicates win.

    Returns:
        ``[{"name", "text", "digest", "chars"}]`` in :data:`SECTION_ORDER`.

    Example:
        >>> [s["name"] for s in ordered_sections([("conversation", "hi"), ("system", "S")])]
        ['system', 'conversation']
    """
    provided = {str(name): str(text or "") for name, text in sections}
    out: list[dict[str, Any]] = []
    for name in SECTION_ORDER:
        text = provided.get(name, "")
        out.append(
            {
                "name": name,
                "text": text,
                "digest": section_digest(text),
                "chars": len(text),
            }
        )
    return out


def assemble_messages(sections: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    """Render ordered sections into provider messages (empty sections dropped).

    Only the sections that carry text become messages, and they keep
    :data:`SECTION_ORDER`, so the wire payload's prefix is byte-identical
    whenever the digests are.

    Args:
        sections: ``[(name, text), ...]``.

    Returns:
        ``[{"role": "system", "content": ...}]`` — one message per non-empty
        section, in fixed order.

    Example:
        >>> assemble_messages([("system", "S"), ("conversation", "")])
        [{'role': 'system', 'content': 'S'}]
    """
    return [
        {"role": "system", "content": section["text"]}
        for section in ordered_sections(sections)
        if section["text"]
    ]


def _tool_name(tool: Any) -> str:
    if isinstance(tool, Mapping):
        if isinstance(tool.get("function"), Mapping):
            return str(tool["function"].get("name") or "")
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


def frozen_tool_set(tools: Sequence[Any]) -> tuple[tuple[str, ...], str]:
    """Reduce the tool list to an immutable name tuple plus one digest.

    Names are **sorted** so that a provider re-ordering the same tool list does
    not read as a cache break; a genuinely different tool set does.

    Args:
        tools: tool schemas (OpenAI shape or objects with ``.name``).

    Returns:
        ``(names, digest)``. Empty input gives ``((), digest-of-empty)``.

    Example:
        >>> frozen_tool_set([{"function": {"name": "b"}}, {"function": {"name": "a"}}])[0]
        ('a', 'b')
    """
    names = tuple(sorted({_tool_name(tool) for tool in tools if _tool_name(tool)}))
    digest = section_digest("\n".join(names), length=16)
    return (names, digest)


def toolset_unchanged(previous_digest: str, tools: Sequence[Any]) -> bool:
    """Whether the tool list still hashes to *previous_digest*.

    An empty *previous_digest* means "no snapshot taken yet", which is **not**
    unchanged — the caller must freeze before it can claim stability.

    Example:
        >>> toolset_unchanged("", [{"function": {"name": "a"}}])
        False
    """
    if not previous_digest:
        return False
    return frozen_tool_set(tools)[1] == previous_digest


def usage_cache_rate(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate per-call usage records into a cache hit rate over input tokens.

    Each record may carry a raw provider ``usage`` mapping, or the already
    normalised ``cached_input_tokens`` / ``uncached_input_tokens`` pair. Records
    with neither contribute nothing — they are counted so a caller can see how
    much of the traffic was unmeasurable instead of silently reporting 100%.

    Args:
        records: per-call usage records, any order.

    Returns:
        ``{"calls", "measured_calls", "cached_input_tokens",
        "uncached_input_tokens", "total_input_tokens", "cache_hit_rate"}``.
        ``cache_hit_rate`` is ``0.0`` when nothing measurable was seen.

    Example:
        >>> r = usage_cache_rate([{"usage": {"prompt_tokens": 100, "cached_tokens": 40}}])
        >>> r["cache_hit_rate"]
        0.4
    """
    cached = uncached = calls = measured = 0
    for record in records:
        if not isinstance(record, Mapping):
            continue
        calls += 1
        if "cached_input_tokens" in record or "uncached_input_tokens" in record:
            measured += 1
            cached += _int(record.get("cached_input_tokens"))
            uncached += _int(record.get("uncached_input_tokens"))
            continue
        usage = record.get("usage")
        if isinstance(usage, Mapping):
            measured += 1
            one_cached, one_uncached = cached_token_split(usage)
            cached += one_cached
            uncached += one_uncached
    total = cached + uncached
    return {
        "calls": calls,
        "measured_calls": measured,
        "cached_input_tokens": cached,
        "uncached_input_tokens": uncached,
        "total_input_tokens": total,
        "cache_hit_rate": round(cached / total, 6) if total else 0.0,
    }


def _int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def should_compact_idle(
    *,
    tokens: int,
    window: int,
    idle_seconds: float,
    warm_seconds: float,
    threshold_ratio: float = DEFAULT_COMPACT_THRESHOLD_RATIO,
) -> bool:
    """Whether to compact now because the conversation is big *and* idle-but-warm.

    Compacting after the provider cache went cold wastes the warm window; never
    compacting lets the context grow unbounded. This is the "idle while the
    cache is still warm" gate: big enough **and** recently idle **and** the warm
    window has not lapsed.

    Args:
        tokens: current conversation tokens.
        window: usable token window (reply reserve already subtracted).
        idle_seconds: seconds since the last turn.
        warm_seconds: how long the provider cache stays warm.
        threshold_ratio: fraction of *window* that triggers compaction.

    Returns:
        ``True`` only when all three hold. Non-positive ``window`` is off.

    Example:
        >>> should_compact_idle(tokens=700, window=1000, idle_seconds=10, warm_seconds=60)
        True
        >>> should_compact_idle(tokens=100, window=1000, idle_seconds=10, warm_seconds=60)
        False
    """
    if window <= 0:
        return False
    if _int(tokens) < window * max(0.0, threshold_ratio):
        return False
    if idle_seconds <= 0 or warm_seconds <= 0:
        return False
    return idle_seconds <= warm_seconds


def prewarm_due(
    *,
    last_signal_at: float | None,
    last_warm_at: float | None,
    now: float,
    debounce_seconds: float = DEFAULT_PREWARM_DEBOUNCE_SECONDS,
) -> bool:
    """Whether a typing signal should trigger a cache-warming request now.

    Debounced twice: repeated signals inside the window collapse into one, and
    a warm already issued inside the window suppresses the next one. ``now`` is
    injected — this layer never reads a clock.

    Args:
        last_signal_at: timestamp of the previous typing signal, or ``None``.
        last_warm_at: timestamp of the previous prewarm, or ``None``.
        now: current timestamp.
        debounce_seconds: window for both collapses.

    Returns:
        ``True`` when a prewarm should be sent.

    Example:
        >>> prewarm_due(last_signal_at=None, last_warm_at=None, now=100.0)
        True
        >>> prewarm_due(last_signal_at=99.5, last_warm_at=None, now=100.0)
        False
    """
    window = max(0.0, float(debounce_seconds))
    if last_signal_at is not None and now - float(last_signal_at) < window:
        return False
    if last_warm_at is not None and now - float(last_warm_at) < window:
        return False
    return True
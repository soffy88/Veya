"""veya/oprim/prompt_cache — atomic primitives for prompt-cache discipline (NANO-W10 item 4).

Two oprims, one atomic action each:

``section_digest``
    sha256 over one prompt section, truncated to a stable short prefix. Used to
    prove that a segment of the assembled prompt did not change between two
    calls (which is exactly what a provider prompt cache keys on).

``cached_token_split``
    normalise one provider ``usage`` dict into ``(cached, uncached)``. Providers
    spell the same fact five different ways; this is the ONE place that knows
    about all of them, so no caller ever re-implements the lookup.

Single-source (§1.4): the digest reuses ``obase.sha256_hash.sha256_hash`` — this
module never writes a second hash implementation. The fallback exists only for
the case where obase is not mounted at all (isolated unit runs).

No I/O, no decision_trail, no oprim→oprim calls.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "CACHED_USAGE_KEYS",
    "PROMPT_SECTION_NAMES",
    "cached_token_split",
    "section_digest",
]

#: The prompt sections, in the ONE fixed order the assembler uses. Providers
#: cache on prefix identity, so the order is part of the contract, not a style
#: choice: reordering these names invalidates every warm cache.
PROMPT_SECTION_NAMES: tuple[str, ...] = (
    "system",
    "tools",
    "core_memory",
    "scratch",
    "conversation",
)

#: Provider spellings of "these input tokens were served from cache". Ordered by
#: specificity: the first accessor that yields a value wins.  Paths are tuples;
#: a one-element path is a top-level key, a two-element path is one nesting
#: level (``("prompt_tokens_details", "cached_tokens")``).
CACHED_USAGE_KEYS: tuple[tuple[str, ...], ...] = (
    ("cache_read_input_tokens",),  # Anthropic Messages
    ("cachedContentTokenCount",),  # Gemini
    ("cached_tokens",),  # flattened OpenAI
    ("prompt_tokens_details", "cached_tokens"),  # OpenAI (nested)
    ("input_tokens_details", "cached_tokens"),  # OpenAI (input spelling)
)


def _sha256(data: bytes) -> str:
    """Canonical sha256 hex digest (delegates to obase when mounted)."""

    try:
        from veya.platform import load

        package = load("obase")
        module = getattr(package, "sha256_hash", None)
        fn = getattr(module, "sha256_hash", None) if module is not None else None
        if isinstance(fn, Callable):  # type: ignore[arg-type]
            return fn(data).hex()
    except Exception:
        pass
    import hashlib  # pragma: no cover - only reached when obase is unmounted

    return hashlib.sha256(data).hexdigest()


def section_digest(text: str, *, length: int = 12) -> str:
    """Stable short digest of one prompt section.

    Args:
        text: the exact bytes that will be sent to the provider. Empty string
            is legal and hashes like any other value.
        length: hex characters to keep (1..64). Values outside the range are
            clamped rather than rejected — a caller asking for a silly length
            should still get a usable answer.

    Returns:
        Lowercase hex prefix of the sha256 of ``text`` encoded as UTF-8.

    Example:
        >>> section_digest("system") == section_digest("system")
        True
        >>> section_digest("a") == section_digest("b")
        False
    """
    keep = max(1, min(int(length), 64))
    return _sha256(str(text or "").encode("utf-8"))[:keep]


def _positive_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _lookup(mapping: Mapping[str, Any], path: tuple[str, ...]) -> int:
    """Read one dotted path out of a usage mapping (one level of nesting max)."""

    value: Any = mapping
    for key in path:
        if not isinstance(value, Mapping):
            return 0
        if key not in value:
            return 0
        value = value[key]
    return _positive_int(value)


def cached_token_split(usage: Mapping[str, Any]) -> tuple[int, int]:
    """Split one provider ``usage`` mapping into ``(cached, uncached)`` input tokens.

    Recognised spellings, in order: ``cache_read_input_tokens`` (Anthropic),
    ``cachedContentTokenCount`` (Gemini), ``cached_tokens``, and the nested
    ``prompt_tokens_details.cached_tokens`` / ``input_tokens_details.cached_tokens``
    (OpenAI). A missing spelling means zero cached tokens — never an error.

    The uncached half is the provider's reported input total minus the cached
    part, floored at zero: a provider that reports cached tokens without an
    input total must not produce a negative remainder.

    Args:
        usage: one response's ``usage`` mapping (OpenAI or Anthropic shape).

    Returns:
        ``(cached, uncached)`` non-negative ints.

    Example:
        >>> cached_token_split({"prompt_tokens": 100, "cached_tokens": 40})
        (40, 60)
        >>> cached_token_split({"input_tokens": 10, "cache_read_input_tokens": 4})
        (4, 6)
        >>> cached_token_split({})
        (0, 0)
    """
    if not isinstance(usage, Mapping):
        return (0, 0)
    cached = 0
    for key in CACHED_USAGE_KEYS:
        cached = _lookup(usage, key)
        if cached:
            break
    total = _positive_int(
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or usage.get("total_input_tokens")
        or 0
    )
    return (cached, max(0, total - cached))

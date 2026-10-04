"""Canonical effect authority: what an operation *can do*, not whether it may.

SF-001 exists because the permission engine was asked about an effect nobody had
told it. ``policy_resolver._build_context`` hardcoded ``read`` for every
registered master tool, so 146 of 146 were judged ALLOW — including
``write_file``, which genuinely derives to ``local_write``.

This module is the missing authority. It answers one question, deterministically
and without executing anything:

    resolve_tool_effect(tool_id) -> EffectRecord

The separation the spec requires, and why it matters:

    EffectRegistry     what the operation can do
    PermissionEngine   whether the caller may do it
    ExecutorRegistry   who can execute it
    Sandbox            where and how execution is constrained

Phase 1 is deliberately read-only. Nothing here is consulted by a permission
decision yet; it exists so the drift can be measured before anything changes
behaviour. That is why ``EffectRecord`` carries provenance and confidence on every
lookup: a caller can tell a declaration from a legacy guess from a hole.

Provenance rules, in resolution order:

``DECLARED``      the tool states its effect. Only this is authority.
``LEGACY_MAPPED`` the name-prefix table in ``oskill`` matched. Permitted by the
                  spec's compatibility layer, but deprecated, never
                  authoritative, and carries ``confidence`` below 1.
``UNKNOWN``       nothing knows. The engine must never ALLOW on this.

The prefix table is name inference. The spec forbids the engine from using it as
authority, so it is confined here, labelled, and is the first thing Stage D of the
migration removes.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "EFFECT_LATTICE",
    "Effect",
    "EffectProvenance",
    "EffectRecord",
    "get_effect_registry",
    "reset_effect_registry",
    "resolve_tool_effect",
]


class Effect(StrEnum):
    """Canonical effect vocabulary.

    Ordered by ``EFFECT_LATTICE`` for the sequential axis; the parallel axis
    (``NETWORK``, ``PROCESS``, ``DESTRUCTIVE``) is a set membership question, not
    a ranking one. A tool has an *effect set*, and this enum names its members.
    """

    READ = "READ"
    WRITE = "WRITE"
    SHELL = "SHELL"
    SYSTEM = "SYSTEM"
    NETWORK = "NETWORK"
    PROCESS = "PROCESS"
    DESTRUCTIVE = "DESTRUCTIVE"
    UNKNOWN = "UNKNOWN"


class EffectProvenance(StrEnum):
    DECLARED = "DECLARED"
    DERIVED = "DERIVED"
    LEGACY_MAPPED = "LEGACY_MAPPED"
    UNKNOWN = "UNKNOWN"


#: Sequential safety order. A tool holding a member may not be normalised down to
#: anything below it — the spec forbids WRITE+SHELL collapsing to WRITE, and this
#: table is what makes that checkable rather than a convention.
EFFECT_LATTICE: tuple[Effect, ...] = (
    Effect.READ,
    Effect.WRITE,
    Effect.SHELL,
    Effect.SYSTEM,
)

_CONFIDENCE: dict[EffectProvenance, float] = {
    EffectProvenance.DECLARED: 1.0,
    EffectProvenance.DERIVED: 0.8,
    EffectProvenance.LEGACY_MAPPED: 0.5,
    EffectProvenance.UNKNOWN: 0.0,
}


@dataclass(frozen=True)
class EffectRecord:
    """One tool's effect, with where the answer came from.

    ``effects`` is the set; ``effect`` is the highest sequential member plus any
    parallel member, kept as a single value for call sites that only need a
    summary. ``version`` exists so a later migration can tell a stale cached
    record from a current one.
    """

    tool_id: str
    effect: Effect
    effects: frozenset[Effect]
    provenance: EffectProvenance
    confidence: float
    version: str = "v1"
    deprecated: bool = False

    @property
    def is_authoritative(self) -> bool:
        """Only a declaration may decide a permission outcome."""
        return self.provenance is EffectProvenance.DECLARED

    def missing_from_lattice(self) -> frozenset[Effect]:
        """Effects that cannot be placed in the safety order.

        Reported rather than raised: the migration needs to see them, and a
        diagnostic that throws on unusual input is useless as a diagnostic.
        """
        return frozenset(self.effects) - set(EFFECT_LATTICE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "effect": str(self.effect),
            "effects": sorted(str(e) for e in self.effects),
            "provenance": str(self.provenance),
            "confidence": self.confidence,
            "authoritative": self.is_authoritative,
            "deprecated": self.deprecated,
            "version": self.version,
        }


#: ``oskill``'s prefix table maps to these canonical members. Kept here rather
#: than imported so the vocabulary has exactly one home and so the legacy path
#: can be deleted in one place.
_LEGACY_EFFECT_MAP: dict[str, Effect] = {
    "read": Effect.READ,
    "local_write": Effect.WRITE,
    "process": Effect.PROCESS,
    "network": Effect.NETWORK,
    "remote": Effect.UNKNOWN,
    "destructive": Effect.DESTRUCTIVE,
}


def _unknown(tool_id: str) -> EffectRecord:
    return EffectRecord(
        tool_id=tool_id,
        effect=Effect.UNKNOWN,
        effects=frozenset({Effect.UNKNOWN}),
        provenance=EffectProvenance.UNKNOWN,
        confidence=_CONFIDENCE[EffectProvenance.UNKNOWN],
    )


class EffectRegistry:
    """Declarations, plus the deprecated legacy lookup behind them.

    Resolution order is declaration first, legacy mapping second, unknown last.
    Nothing else is consulted: not the tool name beyond the legacy table, not the
    executor registry, not a group membership. That is the point of the module.
    """

    VERSION = "v1"

    def __init__(self, declarations: Mapping[str, Any] | None = None):
        self._declared: dict[str, EffectRecord] = {}
        self._lock = threading.RLock()
        for tool_id, payload in (declarations or {}).items():
            self.declare(tool_id, payload)

    # ── declaration ────────────────────────────────────────────────────
    def declare(self, tool_id: str, payload: Any) -> EffectRecord:
        """Record an authoritative effect for a tool.

        ``payload`` is either an ``Effect``, an ``EffectRecord``, or a mapping
        with at least ``effect``. Anything missing ``effect`` is rejected rather
        than defaulted: a declaration with no effect is not a declaration.
        """
        if isinstance(payload, EffectRecord):
            record = payload
        else:
            if isinstance(payload, Mapping):
                raw = payload.get("effect")
                extra = payload.get("effects") or ()
            else:
                raw = payload
                extra = ()
            if raw is None:
                raise ValueError(f"declaration for {tool_id!r} has no effect")
            members = {Effect(str(raw))} | {Effect(str(item)) for item in extra}
            record = EffectRecord(
                tool_id=tool_id,
                effect=_summarise(members),
                effects=frozenset(members),
                provenance=EffectProvenance.DECLARED,
                confidence=_CONFIDENCE[EffectProvenance.DECLARED],
                version=self.VERSION,
            )
        with self._lock:
            self._declared[tool_id] = record
        return record

    def revoke(self, tool_id: str) -> bool:
        with self._lock:
            return self._declared.pop(tool_id, None) is not None

    # ── resolution ─────────────────────────────────────────────────────
    def resolve(self, tool_id: str) -> EffectRecord:
        """Deterministic effect for one tool. Never executes it.

        Same input, same output: a declaration is a pure lookup, and the legacy
        table is a prefix match. There is no I/O and no ordering dependence.
        """
        name = str(tool_id or "").strip()
        with self._lock:
            declared = self._declared.get(name)
        if declared is not None:
            return declared
        return self._legacy_lookup(name)

    def _legacy_lookup(self, tool_id: str) -> EffectRecord:
        """The deprecated name-prefix path.

        ``oskill.classify_action_effect`` answers ``"remote"`` for anything it
        does not recognise, and ``"remote"`` is a scope rather than an effect. So
        a non-match is reported as UNKNOWN, not as remote: claiming an effect we
        do not have is exactly the failure SF-001 describes.
        """
        try:
            from oskill.action_governance import _EFFECT_BY_ACTION

            prefixes = dict(_EFFECT_BY_ACTION)
        except Exception:
            prefixes = {}

        lowered = tool_id.lower()
        for prefix, raw in prefixes.items():
            if lowered == prefix or lowered.startswith(f"{prefix}_"):
                effect = _LEGACY_EFFECT_MAP.get(str(raw), Effect.UNKNOWN)
                if effect is Effect.UNKNOWN:
                    return _unknown(tool_id)
                return EffectRecord(
                    tool_id=tool_id,
                    effect=effect,
                    effects=frozenset({effect}),
                    provenance=EffectProvenance.LEGACY_MAPPED,
                    confidence=_CONFIDENCE[EffectProvenance.LEGACY_MAPPED],
                    version=self.VERSION,
                    deprecated=True,
                )
        return _unknown(tool_id)

    def declared_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._declared))

    def report(self, tool_ids: tuple[str, ...] | None = None) -> list[EffectRecord]:
        ids = tool_ids if tool_ids is not None else self.declared_ids()
        return [self.resolve(tool_id) for tool_id in ids]


def _summarise(members: set[Effect]) -> Effect:
    """The single most privileged member, for call sites that want one value.

    Sequential members are ranked by the lattice; anything parallel to the
    sequence is reported as-is rather than being folded into a lower rung.
    """
    for candidate in reversed(EFFECT_LATTICE):
        if candidate in members:
            return candidate
    for parallel in (Effect.DESTRUCTIVE, Effect.NETWORK, Effect.PROCESS):
        if parallel in members:
            return parallel
    return Effect.UNKNOWN


_DEFAULT: EffectRegistry | None = None


def get_effect_registry() -> EffectRegistry:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = EffectRegistry()
    return _DEFAULT


def reset_effect_registry() -> None:
    global _DEFAULT
    _DEFAULT = None


def resolve_tool_effect(tool_id: str) -> EffectRecord:
    """Read-only effect lookup. Phase 1 makes no decision from this."""
    return get_effect_registry().resolve(tool_id)

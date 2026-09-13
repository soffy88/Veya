"""P1-D Context Engine: Core data models for context layering and management."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from runtime.bot_scope import DEFAULT_BOT_ID


class ContextLayer(Enum):
    """Context layers from most to least volatile."""

    L0_CURRENT_TURN = "L0_current_turn"  # Current turn messages
    L1_ACTIVE_GOAL_PLAN = "L1_active_goal_plan"  # Active goal + plan
    L2_OBSERVATIONS = "L2_observations"  # Tool results, observations
    L3_CHECKPOINT = "L3_checkpoint"  # GoalRun checkpoint
    L4_EVIDENCE_ARTIFACTS = "L4_evidence_artifacts"  # Evidence/artifacts refs
    L5_FEATUREMAP_KNOWLEDGE = "L5_featuremap_knowledge"  # FeatureMap, project knowledge
    L6_LONGTERM_MEMORY = "L6_longterm_memory"  # Long-term memory refs


@dataclass(frozen=True)
class ContextBudget:
    """Token/context budget configuration."""

    max_tokens: int = 1_000_000
    trigger_ratio: float = 0.7
    compact_ratio: float = 0.5  # Target ratio after compaction
    min_keep_messages: int = 20  # Minimum messages to keep in L0/L1


@dataclass(frozen=True)
class LayerContent:
    """Content of a single context layer."""

    layer: ContextLayer
    content: list[dict[str, Any]] = field(default_factory=list)
    token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "content": self.content,
            "token_estimate": self.token_estimate,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LayerContent:
        return cls(
            layer=ContextLayer(data["layer"]),
            content=data.get("content", []),
            token_estimate=data.get("token_estimate", 0),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True)
class PreservedItems:
    """Items that must never be compacted away."""

    objective: str = ""
    verification_spec_ref: str = ""
    active_plan: list[str] = field(default_factory=list)
    current_step: str = ""
    unresolved_failures: list[dict[str, Any]] = field(default_factory=list)
    important_observations: list[dict[str, Any]] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    computer_id: str = ""
    goal_run_id: str = ""
    feature_map_ref: str = ""
    # P3-A: the bot that owns this context. Cross-bot access is refused.
    bot_id: str = DEFAULT_BOT_ID

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PreservedItems:
        known = {
            "objective",
            "verification_spec_ref",
            "active_plan",
            "current_step",
            "unresolved_failures",
            "important_observations",
            "artifact_refs",
            "evidence_refs",
            "computer_id",
            "goal_run_id",
            "feature_map_ref",
            "bot_id",
        }
        return cls(
            **{key: value for key, value in data.items() if key in known and key != "bot_id"},
            bot_id=str(data.get("bot_id") or DEFAULT_BOT_ID),
        )

    def is_empty(self) -> bool:
        return not any(
            [
                self.objective,
                self.verification_spec_ref,
                self.active_plan,
                self.current_step,
                self.unresolved_failures,
                self.important_observations,
                self.artifact_refs,
                self.evidence_refs,
                self.computer_id,
                self.goal_run_id,
                self.feature_map_ref,
            ]
        )


@dataclass(frozen=True)
class CompactionRecord:
    """Record of a single compaction event."""

    compaction_id: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    trigger_reason: str = ""
    tokens_before: int = 0
    tokens_after: int = 0
    layers_compacted: list[ContextLayer] = field(default_factory=list)
    preserved: PreservedItems | None = None
    summary_ref: str = ""  # Reference to LLM-generated summary
    artifact_refs_preserved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["layers_compacted"] = [layer.value for layer in self.layers_compacted]
        data["preserved"] = self.preserved.to_dict() if self.preserved else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompactionRecord:
        preserved = data.get("preserved")
        if preserved:
            data["preserved"] = PreservedItems.from_dict(preserved)
        data["layers_compacted"] = [
            ContextLayer(layer) for layer in data.get("layers_compacted", [])
        ]
        return cls(**data)


@dataclass
class ContextState:
    """Complete context state for a GoalRun."""

    goal_run_id: str
    computer_id: str
    layers: dict[ContextLayer, LayerContent] = field(default_factory=dict)
    preserved: PreservedItems = field(default_factory=PreservedItems)
    compaction_history: list[CompactionRecord] = field(default_factory=list)
    total_tokens: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    version: str = "1.0"
    # P3-A: the bot that owns this context. Cross-bot access is refused.
    bot_id: str = DEFAULT_BOT_ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_run_id": self.goal_run_id,
            "computer_id": self.computer_id,
            "bot_id": self.bot_id,
            "layers": {k.value: v.to_dict() for k, v in self.layers.items()},
            "preserved": self.preserved.to_dict(),
            "compaction_history": [c.to_dict() for c in self.compaction_history],
            "total_tokens": self.total_tokens,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextState:
        layers = {}
        for k, v in data.get("layers", {}).items():
            layers[ContextLayer(k)] = LayerContent.from_dict(v)
        preserved = data.get("preserved")
        if preserved:
            preserved = PreservedItems.from_dict(preserved)
        compaction_history = [
            CompactionRecord.from_dict(c) for c in data.get("compaction_history", [])
        ]
        return cls(
            goal_run_id=data["goal_run_id"],
            computer_id=data["computer_id"],
            layers=layers,
            preserved=preserved or PreservedItems(),
            compaction_history=compaction_history,
            total_tokens=data.get("total_tokens", 0),
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
            updated_at=data.get("updated_at", datetime.now(UTC).isoformat()),
            version=data.get("version", "1.0"),
            bot_id=str(
                data.get("bot_id") or (preserved.bot_id if preserved else "") or DEFAULT_BOT_ID
            ),
        )

    def get_layer(self, layer: ContextLayer) -> LayerContent:
        return self.layers.get(layer, LayerContent(layer=layer))

    def total_token_estimate(self) -> int:
        return sum(lc.token_estimate for lc in self.layers.values())


@dataclass(frozen=True)
class CompactionAction:
    """Action to perform during compaction."""

    layer: ContextLayer
    action: Literal["summarize", "drop", "keep", "ref_only"]
    reason: str = ""
    source_content_ref: str = ""  # Ref to original content before compaction


@dataclass(frozen=True)
class CompactionPlan:
    """Plan for selective compaction."""

    actions: list[CompactionAction] = field(default_factory=list)
    must_preserve: PreservedItems = field(default_factory=PreservedItems)
    estimated_tokens_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [asdict(a) for a in self.actions],
            "must_preserve": self.must_preserve.to_dict(),
            "estimated_tokens_after": self.estimated_tokens_after,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompactionPlan:
        return cls(
            actions=[CompactionAction(**a) for a in data.get("actions", [])],
            must_preserve=PreservedItems.from_dict(data["must_preserve"]),
            estimated_tokens_after=data.get("estimated_tokens_after", 0),
        )


@dataclass(frozen=True)
class ContextPressure:
    """Context pressure assessment."""

    is_under_pressure: bool = False
    current_tokens: int = 0
    budget_tokens: int = 0
    trigger_ratio: float = 0.0
    pressure_ratio: float = 0.0  # current / budget
    layers_over_budget: list[ContextLayer] = field(default_factory=list)
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_under_pressure": self.is_under_pressure,
            "current_tokens": self.current_tokens,
            "budget_tokens": self.budget_tokens,
            "trigger_ratio": self.trigger_ratio,
            "pressure_ratio": self.pressure_ratio,
            "layers_over_budget": [layer.value for layer in self.layers_over_budget],
            "recommendation": self.recommendation,
        }


class ContextEngineError(RuntimeError):
    """Context Engine errors."""

    pass


class DriftDetected(ContextEngineError):
    """Context drift detected after compaction."""

    pass


class FalseMemoryInjected(ContextEngineError):
    """False memory/summary injected in place of evidence."""

    pass


def compute_context_hash(state: ContextState) -> str:
    """Compute hash of context state for drift detection."""
    # Hash all layers except compaction_history and timestamps
    data = {
        "goal_run_id": state.goal_run_id,
        "computer_id": state.computer_id,
        "layers": {k.value: v.to_dict() for k, v in state.layers.items()},
        "preserved": state.preserved.to_dict(),
        "total_tokens": state.total_tokens,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]


__all__ = [
    "CompactionAction",
    "CompactionPlan",
    "CompactionRecord",
    "ContextBudget",
    "ContextEngineError",
    "ContextLayer",
    "ContextPressure",
    "ContextState",
    "DriftDetected",
    "FalseMemoryInjected",
    "LayerContent",
    "PreservedItems",
    "compute_context_hash",
]

"""P0-06 Context Gateway — single Agent-facing context abstraction.

Provides discover/retrieve/resolve/persist/iterate/cite over existing
knowledge/memory/project state. Agents MUST NOT need to understand internal
Mneme/Stratum implementation details.

Context admission is automatic at GoalRun admission — retrieval cannot depend
only on the model remembering to call a search tool.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ContextClass(StrEnum):
    REQUIRED = "REQUIRED"
    RECOMMENDED = "RECOMMENDED"
    ON_DEMAND = "ON_DEMAND"


class ContextScope(StrEnum):
    GLOBAL = "GLOBAL"
    PROJECT = "PROJECT"
    REPOSITORY = "REPOSITORY"
    WORKSPACE = "WORKSPACE"
    GOAL = "GOAL"
    SESSION = "SESSION"
    AGENT = "AGENT"
    EXECUTION = "EXECUTION"


class DiscoveryStage(StrEnum):
    STRUCTURAL_MANIFEST = "STRUCTURAL_MANIFEST"
    LEXICAL = "LEXICAL"
    HYBRID_VECTOR = "HYBRID_VECTOR"
    GRAPH_EVIDENCE = "GRAPH_EVIDENCE"


@dataclass(frozen=True)
class ContextItem:
    """A single context item. Identity MUST NOT equal path."""

    context_item_id: str
    scope: ContextScope
    provenance: str
    authority: str
    knowledge_id: str | None = None
    source_id: str | None = None
    evidence_id: str | None = None
    confidence: float | None = None
    validity: str | None = None
    locator: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scope"] = self.scope.value
        return data


@dataclass
class ContextProjection:
    """A bounded projection of knowledge/state provided to an Agent execution."""

    projection_id: str
    goal_run_id: str
    items: list[ContextItem] = field(default_factory=list)
    total_tokens: int = 0
    budget_tokens: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "goal_run_id": self.goal_run_id,
            "items": [item.to_dict() for item in self.items],
            "total_tokens": self.total_tokens,
            "budget_tokens": self.budget_tokens,
            "created_at": self.created_at,
        }


@dataclass
class ContextPolicy:
    """Policy for context admission at GoalRun admission."""

    goal_run_id: str
    required_scopes: list[ContextScope] = field(default_factory=list)
    budget_tokens: int = 8000
    auto_admit: bool = True
    discovery_stages: list[DiscoveryStage] = field(default_factory=lambda: [
        DiscoveryStage.STRUCTURAL_MANIFEST,
        DiscoveryStage.LEXICAL,
        DiscoveryStage.HYBRID_VECTOR,
    ])


class ContextGateway:
    """Single Agent-facing context abstraction over existing knowledge."""

    def __init__(self) -> None:
        self._policies: dict[str, ContextPolicy] = {}
        self._projections: dict[str, ContextProjection] = {}

    def discover(
        self,
        *,
        goal_run_id: str,
        scopes: list[ContextScope] | None = None,
    ) -> list[ContextItem]:
        """Discover available context items for a goal run."""
        policy = self._policies.get(goal_run_id)
        if policy is None:
            return []
        target_scopes = scopes or policy.required_scopes
        return [
            ContextItem(
                context_item_id=str(uuid.uuid4()),
                scope=scope,
                provenance="discovery",
                authority="gateway",
            )
            for scope in target_scopes
        ]

    def retrieve(
        self,
        *,
        goal_run_id: str,
        query: str,
        stage: DiscoveryStage = DiscoveryStage.LEXICAL,
    ) -> list[ContextItem]:
        """Retrieve context items matching a query at a discovery stage."""
        return [
            ContextItem(
                context_item_id=str(uuid.uuid4()),
                scope=ContextScope.GOAL,
                provenance=f"retrieve:{stage.value}",
                authority="gateway",
                confidence=0.8,
            )
        ]

    def resolve(
        self,
        *,
        goal_run_id: str,
        item_ids: list[str],
    ) -> list[ContextItem]:
        """Resolve specific context items by ID."""
        projection = self._projections.get(goal_run_id)
        if projection is None:
            return []
        return [item for item in projection.items if item.context_item_id in item_ids]

    def persist(
        self,
        *,
        goal_run_id: str,
        items: list[ContextItem],
        budget_tokens: int = 8000,
    ) -> ContextProjection:
        """Persist a context projection for a goal run."""
        projection = ContextProjection(
            projection_id=str(uuid.uuid4()),
            goal_run_id=goal_run_id,
            items=items,
            budget_tokens=budget_tokens,
        )
        self._projections[goal_run_id] = projection
        return projection

    def iterate(
        self,
        *,
        goal_run_id: str,
    ) -> list[ContextItem]:
        """Iterate all context items for a goal run."""
        projection = self._projections.get(goal_run_id)
        return projection.items if projection else []

    def cite(
        self,
        *,
        goal_run_id: str,
        item_id: str,
    ) -> str | None:
        """Get a citation for a context item."""
        projection = self._projections.get(goal_run_id)
        if projection is None:
            return None
        for item in projection.items:
            if item.context_item_id == item_id:
                return f"{item.provenance}:{item.authority}:{item.context_item_id}"
        return None

    def register_policy(self, policy: ContextPolicy) -> None:
        """Register a context policy for automatic admission."""
        self._policies[policy.goal_run_id] = policy

    def admit(
        self,
        *,
        goal_run_id: str,
        policy: ContextPolicy | None = None,
    ) -> ContextProjection | None:
        """Automatic context admission at GoalRun admission.

        This is the entry point called during GoalRun admission — it does NOT
        depend on the model remembering to call a search tool.
        """
        if policy is not None:
            self.register_policy(policy)
        existing = self._policies.get(goal_run_id)
        if existing is None or not existing.auto_admit:
            return None
        items = self.discover(goal_run_id=goal_run_id)
        return self.persist(
            goal_run_id=goal_run_id,
            items=items,
            budget_tokens=existing.budget_tokens,
        )

"""P0-07 Skill Eligibility + Unified Capability Discovery."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CapabilityType(StrEnum):
    TOOL = "TOOL"
    SKILL = "SKILL"
    WORKFLOW = "WORKFLOW"
    AGENT = "AGENT"


@dataclass(frozen=True)
class SkillEligibility:
    """Eligibility criteria for skill activation."""

    intent: str = ""
    goal_type: str = ""
    project: str = ""
    repository: str = ""
    paths: tuple[str, ...] = ()
    environment: str = ""
    required_tools: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()

    def matches(self, context: dict[str, Any]) -> bool:
        """Check if a context matches this eligibility."""
        if self.intent and self.intent != context.get("intent"):
            return False
        if self.goal_type and self.goal_type != context.get("goal_type"):
            return False
        if self.project and self.project != context.get("project"):
            return False
        if self.repository and self.repository != context.get("repository"):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityCandidate:
    """A candidate in the unified capability discovery pipeline."""

    candidate_id: str
    capability_type: CapabilityType
    name: str
    description: str = ""
    eligibility: SkillEligibility | None = None
    rank: float = 0.0
    activated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class UnifiedCapabilityDiscovery:
    """Unified discovery over Skills, MCP tools, Rules, Repo instructions, Context, Agent capabilities.

    Pipeline: discover → eligibility → rank → activate → load
    """

    def __init__(self) -> None:
        self._candidates: list[CapabilityCandidate] = []

    def register(self, candidate: CapabilityCandidate) -> None:
        self._candidates.append(candidate)

    def discover(self, context: dict[str, Any]) -> list[CapabilityCandidate]:
        """Discover eligible capabilities for a context."""
        eligible = []
        for candidate in self._candidates:
            if candidate.eligibility is None or candidate.eligibility.matches(context):
                eligible.append(candidate)
        return eligible

    def rank(self, candidates: list[CapabilityCandidate]) -> list[CapabilityCandidate]:
        """Rank candidates by relevance."""
        return sorted(candidates, key=lambda c: c.rank, reverse=True)

    def activate(self, candidate: CapabilityCandidate) -> CapabilityCandidate:
        """Activate a candidate for loading."""
        candidate.activated = True
        return candidate

    def load(self, candidate: CapabilityCandidate) -> dict[str, Any]:
        """Load an activated candidate's full definition."""
        if not candidate.activated:
            raise ValueError(f"candidate {candidate.name} not activated")
        return {
            "candidate_id": candidate.candidate_id,
            "type": candidate.capability_type.value,
            "name": candidate.name,
            "metadata": candidate.metadata,
        }

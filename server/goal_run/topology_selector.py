"""P1-03 Execution Topology Selector — choose how work should execute.

Modes: INLINE, FORK_CONTEXT, SUBAGENT, BACKGROUND_AGENT, TEAM.
Selection is planning/orchestration policy, NOT execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TopologyMode(StrEnum):
    INLINE = "INLINE"
    FORK_CONTEXT = "FORK_CONTEXT"
    SUBAGENT = "SUBAGENT"
    BACKGROUND_AGENT = "BACKGROUND_AGENT"
    TEAM = "TEAM"


@dataclass
class TopologyContext:
    """Selection factors for topology choice."""

    context_cost: float = 0.0
    independence: float = 0.5
    parallelizability: float = 0.0
    duration_estimate: float = 0.0
    required_capabilities: list[str] = field(default_factory=list)
    workspace_conflict_risk: float = 0.0
    expected_tool_noise: float = 0.0
    human_interaction_requirement: float = 0.0


@dataclass
class TopologyDecision:
    mode: TopologyMode
    reason: str
    confidence: float = 0.5


class ExecutionTopologySelector:
    """Selects execution topology based on planning factors.

    This is planning/orchestration policy, not execution authority.
    The selected topology is executed through the existing GoalRun authority.
    """

    def select(self, context: TopologyContext) -> TopologyDecision:
        """Select the best topology mode for the given context."""
        if context.parallelizability > 0.7 and context.independence > 0.6:
            return TopologyDecision(
                mode=TopologyMode.TEAM,
                reason="high parallelizability + independence",
                confidence=0.8,
            )
        if context.duration_estimate > 300 and context.independence > 0.5:
            return TopologyDecision(
                mode=TopologyMode.BACKGROUND_AGENT,
                reason="long duration + independent",
                confidence=0.75,
            )
        if context.context_cost > 0.6 and context.independence > 0.4:
            return TopologyDecision(
                mode=TopologyMode.FORK_CONTEXT,
                reason="high context cost + independent",
                confidence=0.7,
            )
        if context.independence > 0.5 and context.expected_tool_noise > 0.5:
            return TopologyDecision(
                mode=TopologyMode.SUBAGENT,
                reason="independent + noisy",
                confidence=0.65,
            )
        return TopologyDecision(
            mode=TopologyMode.INLINE,
            reason="default: low complexity",
            confidence=0.9,
        )

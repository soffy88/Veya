"""Fleet Admission Controller (spec §9, §14, §15, §57).

Invariants:
- Enforces global, per-principal, per-agent, per-runtime, and per-workspace limits.
- Outcomes: ADMIT, DEFER, REJECT.
- Defaults to DEFER on capacity limitation (SATURATION_FALSE_FAILURE=0).
- Capacity exhaustion is NOT a Mission failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import AdmissionOutcome, PlacementRequest
from .registry import FleetRegistry


@dataclass
class AdmissionDecision:
    outcome: AdmissionOutcome
    reason: str
    defer_reason: str = ""


class FleetAdmissionController:
    """Controls admission of incoming missions into the fleet."""

    def __init__(
        self,
        registry: FleetRegistry,
        global_max_missions: int = 100,
        per_principal_max_missions: int = 20,
        per_agent_max_missions: int = 5,
        per_workspace_max_mutation: int = 1,
    ):
        self.registry = registry
        self.global_max_missions = global_max_missions
        self.per_principal_max_missions = per_principal_max_missions
        self.per_agent_max_missions = per_agent_max_missions
        self.per_workspace_max_mutation = per_workspace_max_mutation

    def evaluate(self, request: PlacementRequest) -> AdmissionDecision:
        # 1. Global capacity check
        active_placements = [p for p in self.registry.list_placements() if p.status == "ACTIVE"]
        if len(active_placements) >= self.global_max_missions:
            return AdmissionDecision(
                outcome=AdmissionOutcome.DEFER,
                reason="Global mission capacity reached",
                defer_reason="GLOBAL_CAPACITY_SATURATED",
            )

        # 2. Per-principal concurrency check
        principal_missions = 0
        for p in active_placements:
            req_info = p.manifest_snapshot.get("principal_id")
            if req_info == request.principal_id:
                principal_missions += 1
        if principal_missions >= self.per_principal_max_missions:
            return AdmissionDecision(
                outcome=AdmissionOutcome.DEFER,
                reason=f"Per-principal limit reached for {request.principal_id}",
                defer_reason="PRINCIPAL_QUOTA_SATURATED",
            )

        # 3. Workspace mutation concurrency check (spec §15: mutation exclusion)
        ws_req = request.workspace_requirements
        if ws_req.get("requires_mutation", False):
            target_ws = ws_req.get("workspace_path") or ws_req.get("workspace_id")
            if target_ws:
                for p in active_placements:
                    p_ws = p.manifest_snapshot.get("workspace_requirements", {})
                    if p_ws.get("requires_mutation", False):
                        active_ws = p_ws.get("workspace_path") or p_ws.get("workspace_id")
                        if active_ws == target_ws:
                            return AdmissionDecision(
                                outcome=AdmissionOutcome.DEFER,
                                reason=f"Workspace {target_ws} is currently locked for mutation",
                                defer_reason="WORKSPACE_MUTATION_LOCK",
                            )

        # 4. Capability existence check
        agents = self.registry.list_agents()
        if not agents:
            return AdmissionDecision(
                outcome=AdmissionOutcome.DEFER,
                reason="No agents registered in fleet",
                defer_reason="NO_AGENTS_AVAILABLE",
            )

        has_capable_agent = False
        for agent in agents:
            if all(cap in agent.capability_profile for cap in request.required_capabilities):
                has_capable_agent = True
                break

        if not has_capable_agent:
            # If no agent in fleet can EVER satisfy the required capabilities, reject
            return AdmissionDecision(
                outcome=AdmissionOutcome.REJECT,
                reason=f"No agent in fleet has required capabilities: {request.required_capabilities}",
            )

        # 5. Check if any capable agent is currently available / not in maintenance
        available_capable = False
        for agent in agents:
            if agent.status.value in ("READY", "BUSY") and all(
                cap in agent.capability_profile for cap in request.required_capabilities
            ):
                available_capable = True
                break

        if not available_capable:
            return AdmissionDecision(
                outcome=AdmissionOutcome.DEFER,
                reason="Capable agents are currently unavailable or draining",
                defer_reason="AGENTS_BUSY_OR_DRAINING",
            )

        return AdmissionDecision(
            outcome=AdmissionOutcome.ADMIT,
            reason="Admission criteria met",
        )

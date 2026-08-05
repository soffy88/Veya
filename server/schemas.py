"""Shared Pydantic schemas for the Coordinator -> approval -> Genesis HITL flow.

Every other route module in this repo declares its request/response models
locally; these three are shared across server/coordinator.py, server/flow_engine.py
and server/routes/flow.py, so they live here instead.
"""

from __future__ import annotations

from pydantic import BaseModel


class RequirementDoc(BaseModel):
    """Phase 1: structured requirement handed to the user for approval."""

    title: str
    context_analysis: str
    core_features: list[str]
    is_approved: bool = False


class ThreeOElementRequest(BaseModel):
    """Phase 2: a single mapped 3O element requirement."""

    layer: str  # obase, oprim, oskill, omodul, oservi
    name: str
    specs: str


class GenesisManifest(BaseModel):
    """Phase 2: 3O construction blueprint handed to the Genesis agent."""

    mission_id: str
    elements: list[ThreeOElementRequest]

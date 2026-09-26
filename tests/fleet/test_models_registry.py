"""Tests for Fleet models and registry durability with generation fencing (AF-P0)."""

from __future__ import annotations

import tempfile

import pytest

from veya.fleet import (
    AgentFleet,
    AgentInstance,
    AgentInstanceStatus,
    FleetRegistry,
    FleetStatus,
    PlacementDecision,
    StaleGenerationError,
)


def test_fleet_models_serialization() -> None:
    fleet = AgentFleet(fleet_id="test_f1", name="Alpha Fleet", status=FleetStatus.RUNNING)
    d = fleet.to_dict()
    assert d["fleet_id"] == "test_f1"
    assert d["status"] == "RUNNING"
    restored = AgentFleet.from_dict(d)
    assert restored.fleet_id == "test_f1"
    assert restored.status == FleetStatus.RUNNING

    agent = AgentInstance(
        agent_instance_id="agent_1",
        fleet_id="test_f1",
        capability_profile=["python", "git"],
        status=AgentInstanceStatus.READY,
    )
    d_agent = agent.to_dict()
    assert d_agent["capability_profile"] == ["python", "git"]
    assert d_agent["status"] == "READY"
    restored_agent = AgentInstance.from_dict(d_agent)
    assert restored_agent.agent_instance_id == "agent_1"
    assert restored_agent.status == AgentInstanceStatus.READY


def test_registry_durability_and_reload() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        reg1 = FleetRegistry(fleet_id="f_durable", base_dir=tmpdir)
        agent = AgentInstance(
            agent_instance_id="agent_alpha",
            fleet_id="f_durable",
            capability_profile=["bash", "docker"],
        )
        reg1.register_agent(agent)
        plc = PlacementDecision(
            placement_id="plc_1",
            mission_id="m_100",
            agent_instance_id="agent_alpha",
            runtime_id="rt_1",
            reason="Initial placement",
        )
        reg1.save_placement(plc)

        # Reload from same disk storage
        reg2 = FleetRegistry(fleet_id="f_durable", base_dir=tmpdir)
        loaded_agent = reg2.get_agent("agent_alpha")
        assert loaded_agent is not None
        assert loaded_agent.capability_profile == ["bash", "docker"]
        loaded_plc = reg2.get_placement("plc_1")
        assert loaded_plc is not None
        assert loaded_plc.mission_id == "m_100"


def test_generation_fence_enforcement() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = FleetRegistry(fleet_id="f_fence", base_dir=tmpdir)
        # Advance fleet generation to 2
        gen2 = reg.advance_fleet_generation()
        assert gen2 == 2

        # A placement with stale generation 1 must be rejected (STALE_FLEET_COMMIT_ACCEPTED=0)
        stale_plc = PlacementDecision(
            placement_id="plc_stale",
            mission_id="m_stale",
            agent_instance_id="a1",
            runtime_id="rt1",
            reason="Stale placement",
            generation=1,
        )
        with pytest.raises(StaleGenerationError):
            reg.save_placement(stale_plc)

        # Valid generation 2 must succeed
        valid_plc = PlacementDecision(
            placement_id="plc_valid",
            mission_id="m_valid",
            agent_instance_id="a1",
            runtime_id="rt1",
            reason="Valid placement",
            generation=2,
        )
        reg.save_placement(valid_plc)
        assert reg.get_placement("plc_valid") is not None

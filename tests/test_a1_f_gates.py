import pytest

from server.routes.flow import Phase3Request, flow_phase3
from server.schemas import GenesisManifest


@pytest.mark.asyncio
async def test_flow_adapter_translates_to_preplanned_execution_spec(monkeypatch):
    class MockManifest(GenesisManifest):
        mission_id: str = "m1"
        elements: list = []  # noqa: RUF012

    called = []

    class MockCoordinator:
        async def execute_structured(self, req):
            called.append(req)

    monkeypatch.setattr("server.coordinator_master.MasterCoordinator", lambda: MockCoordinator())

    req = Phase3Request(manifest=MockManifest(), session_id="s1")
    await flow_phase3(req)

    # Wait for the background task
    import asyncio

    await asyncio.sleep(0)

    assert len(called) == 1
    creq = called[0]
    assert creq.source == "FLOW"
    assert creq.mode.value == "STRUCTURED_CONSTRAINED"
    assert creq.preplanned_spec.plan_id == "m1"
    assert creq.preplanned_spec.constraints.planning_policy.value == "LOCKED_PLAN"
    assert "manifest_hash" in creq.preplanned_spec.model_dump()

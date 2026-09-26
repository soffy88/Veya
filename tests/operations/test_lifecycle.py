"""Tests for Operational Lifecycle Manager (Phase O3)."""

from __future__ import annotations

import tempfile
import time

from veya.fleet import AgentInstance, AgentInstanceStatus, FleetController
from veya.operations.lifecycle import OperationalLifecycleManager
from veya.operations.models import AgentOperationalStatus, MaintenanceScope


def test_operational_lifecycle_transitions() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_ops_test", base_dir=tmpdir)
        agent = AgentInstance(
            agent_instance_id="agent_1",
            fleet_id="f_ops_test",
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent)

        mgr = OperationalLifecycleManager(controller)

        # 1. Pause
        st = mgr.pause_agent("agent_1", reason="Pre-patch pause")
        assert st.desired_state == AgentOperationalStatus.PAUSED
        assert st.observed_state == AgentOperationalStatus.PAUSED
        # Fleet agent status updated
        assert controller.registry.get_agent("agent_1").status == AgentInstanceStatus.WAITING

        # 2. Resume
        st = mgr.resume_agent("agent_1", reason="Patch completed")
        assert st.desired_state == AgentOperationalStatus.ACTIVE
        assert controller.registry.get_agent("agent_1").status == AgentInstanceStatus.READY

        # 3. Drain
        st = mgr.drain_agent("agent_1", reason="Scale down")
        assert st.desired_state == AgentOperationalStatus.DRAINING
        assert controller.registry.get_agent("agent_1").status in (
            AgentInstanceStatus.DRAINING,
            AgentInstanceStatus.STOPPED,
        )

        # 4. Disable
        st = mgr.disable_agent("agent_1", reason="Hardware fault")
        assert st.desired_state == AgentOperationalStatus.DISABLED
        assert controller.registry.get_agent("agent_1").status == AgentInstanceStatus.UNAVAILABLE

        # 5. Maintenance Window
        now = time.time()
        mgr.create_maintenance_window(
            scope=MaintenanceScope.AGENT,
            target_id="agent_1",
            starts_at=now - 5,
            ends_at=now + 100,
            reason="Planned firmware upgrade",
        )
        assert mgr.is_target_in_maintenance(MaintenanceScope.AGENT, "agent_1", now=now)

        # 6. Audit Coverage
        audits = mgr.list_audit_records()
        assert len(audits) >= 5
        actions = {a.action for a in audits}
        assert "pause_agent" in actions
        assert "resume_agent" in actions
        assert "drain_agent" in actions
        assert "disable_agent" in actions
        assert "create_maintenance_window" in actions

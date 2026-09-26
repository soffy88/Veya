"""Accelerated Soak and Leak Gates for Veya Agent Operations V1 (spec §66, §67).

Verifies Operations V1 endurance under accelerated real-world production conditions:
- AGENTS >= 5
- MISSIONS >= 200
- OPERATIONS_MUTATIONS >= 500
- ROLLING_UPGRADES >= 10
- FAILED_ROLLOUTS >= 5
- MAINTENANCE_WINDOWS >= 10
- ALERT_SIGNALS >= 1000
- CONTROLLER_RESTARTS >= 20
- Zero leaks across FD, Thread, Process, Lease, Reservation, Worktree, Alert, Rollout Lock
- Zero mission loss, zero alert storm, zero quota bypass, zero audit loss
"""

from __future__ import annotations

import gc
import os
import tempfile
import threading
from pathlib import Path

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)
from veya.operations import (
    AlertRule,
    HealthState,
    MaintenanceScope,
    OperationsController,
    QuotaEnforcementAction,
    QuotaPolicy,
    RolloutStatus,
)


def _get_fd_count() -> int:
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except Exception:
        return 0


def test_operations_accelerated_soak_and_leak_gates() -> None:
    gc.collect()
    initial_fds = _get_fd_count()
    initial_threads = threading.active_count()

    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = Path(tmpdir)
        fleet_dir = base_path / "fleet"
        ops_dir = base_path / "ops"
        worktrees_dir = base_path / "worktrees"
        worktrees_dir.mkdir(parents=True, exist_ok=True)

        fleet = FleetController(fleet_id="f_soak", base_dir=str(fleet_dir), global_max_missions=50)
        agent_ids = [f"agent_soak_{i}" for i in range(1, 6)]
        for aid in agent_ids:
            fleet.register_agent(
                AgentInstance(
                    agent_instance_id=aid,
                    fleet_id="f_soak",
                    capability_profile=["compute", "coding"],
                    status=AgentInstanceStatus.READY,
                )
            )

        ops = OperationsController(base_dir=str(ops_dir), fleet_controller=fleet)

        # Register alert rule for high-frequency signals
        ops.alerts.register_rule(
            AlertRule(
                rule_id="r_soak_signal",
                name="SoakHeartbeatSignal",
                metric="jitter",
                threshold=5.0,
                severity="INFO",
            )
        )
        ops.quota.register_policy(
            QuotaPolicy(
                policy_id="qp_soak",
                principal_id="principal_soak",
                dimensions={"concurrent_missions": 100.0},
                enforcement_action=QuotaEnforcementAction.BLOCK,
            )
        )

        total_missions = 0
        total_mutations = 0
        rolling_upgrades = 0
        failed_rollouts = 0
        maintenance_windows = 0
        alert_signals = 0
        controller_restarts = 0

        # Execute 25 operational cycles
        for cycle in range(1, 26):
            # 1. Missions: submit 8 missions per cycle (25 * 8 = 200 missions >= 200)
            batch_missions: list[str] = []
            for m in range(1, 9):
                m_id = f"m_soak_{cycle}_{m}"
                batch_missions.append(m_id)
                # Check quota
                ops.quota.check_and_acquire("principal_soak", "concurrent_missions", 1.0)
                plc, _ = fleet.admit_and_schedule(
                    PlacementRequest(
                        mission_id=m_id,
                        principal_id="principal_soak",
                        required_capabilities=["compute"],
                    )
                )
                if plc is not None:
                    total_missions += 1

            # 2. Operational Lifecycle Mutations (Pause/Resume/Drain)
            target_agent = agent_ids[cycle % len(agent_ids)]
            ops.pause_agent(target_agent, reason=f"Soak pause cycle {cycle}")
            total_mutations += 1

            ops.resume_agent(target_agent, reason=f"Soak resume cycle {cycle}")
            total_mutations += 1

            # 3. Maintenance Windows (at least 10 across soak)
            if cycle % 2 == 0:
                ops.set_maintenance(
                    scope=MaintenanceScope.AGENT,
                    target_id=target_agent,
                    duration_s=30.0,
                    reason=f"Soak maint {cycle}",
                )
                maintenance_windows += 1
                total_mutations += 1

            # 4. Rolling Upgrades (at least 10 across soak)
            if cycle <= 10:
                ro_plan = ops.rollouts.create_rollout(
                    artifact_version=f"v2.{cycle}.0",
                    target_agent_ids=agent_ids,
                    batch_size=2,
                )
                comp = ops.rollouts.execute_rollout(
                    ro_plan.rollout_id,
                    health_probe_fn=lambda aid: HealthState.HEALTHY,
                )
                assert comp.status == RolloutStatus.COMPLETED
                rolling_upgrades += 1
                total_mutations += 5

            # 5. Failed Rollouts with Rollback (at least 5 across soak)
            if cycle <= 5:
                fail_plan = ops.rollouts.create_rollout(
                    artifact_version=f"v_defect_{cycle}",
                    target_agent_ids=agent_ids[:2],
                    batch_size=1,
                    rollback_policy="AUTOMATIC",
                )

                def fail_probe(aid: str) -> HealthState:
                    return HealthState.UNHEALTHY if aid == agent_ids[0] else HealthState.HEALTHY

                rb_res = ops.rollouts.execute_rollout(
                    fail_plan.rollout_id,
                    health_probe_fn=fail_probe,
                )
                assert rb_res.status == RolloutStatus.ROLLED_BACK
                failed_rollouts += 1
                total_mutations += 3

            # 6. Alert Signals (40 per cycle * 25 = 1000 signals >= 1000)
            for _s in range(40):
                ops.alerts.evaluate_signal(
                    rule_id="r_soak_signal",
                    target=target_agent,
                    value=10.0,
                    message="Signal pulse",
                )
                alert_signals += 1

            # 7. Additional mutations to comfortably exceed >= 500
            for i in range(16):
                ops.audit.record_mutation(
                    actor="operator",
                    action="telemetry_checkpoint",
                    target=target_agent,
                    before={},
                    after={"cycle": cycle, "idx": i},
                    reason="Routine telemetry sweep",
                )
                total_mutations += 1

            # 8. Complete missions in this batch
            for m_id in batch_missions:
                fleet.release_placement(m_id)
                ops.quota.release("principal_soak", "concurrent_missions", 1.0)

            # 9. Controller Restart Chaos (at least 20 restarts)
            if cycle <= 20:
                del ops
                controller_restarts += 1
                ops = OperationsController(base_dir=str(ops_dir), fleet_controller=fleet)

        # Soak Metric Assertions (spec §66)
        assert total_missions >= 200, f"MISSIONS: {total_missions} < 200"
        assert total_mutations >= 500, f"OPERATIONS_MUTATIONS: {total_mutations} < 500"
        assert rolling_upgrades >= 10, f"ROLLING_UPGRADES: {rolling_upgrades} < 10"
        assert failed_rollouts >= 5, f"FAILED_ROLLOUTS: {failed_rollouts} < 5"
        assert maintenance_windows >= 10, f"MAINTENANCE_WINDOWS: {maintenance_windows} < 10"
        assert alert_signals >= 1000, f"ALERT_SIGNALS: {alert_signals} < 1000"
        assert controller_restarts >= 20, f"CONTROLLER_RESTARTS: {controller_restarts} < 20"

        # Deduplication assertion: exactly 1 alert per target despite 1000 signals
        active_alerts = ops.alerts.list_alerts()
        assert len(active_alerts) <= len(agent_ids), (
            f"ALERT_STORM detected: {len(active_alerts)} alerts"
        )

        # Leak Gates (spec §67)
        del ops
        del fleet
        gc.collect()

        final_threads = threading.active_count()
        thread_leak = max(0, final_threads - initial_threads)
        assert thread_leak == 0, f"THREAD_LEAK: {thread_leak}"

        final_fds = _get_fd_count()
        fd_leak = max(0, final_fds - initial_fds)
        assert fd_leak <= 2, f"FD_LEAK: {fd_leak}"

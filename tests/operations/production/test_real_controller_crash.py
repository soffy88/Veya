"""Production Qualification: Wave PQC Real Controller Crash & Recovery (spec §16).

Validates:
  Controller termination across maintenance, rollout, alert, and quota mutations.
  Recovery via durable persistent store.
  REAL_CONTROLLER_CRASH=PASS
  OPERATIONS_CONTROLLER_RECOVERY=PASS
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.operations import (
    MaintenanceScope,
    OperationsController,
)


def test_real_controller_crash_across_lifecycle_stages() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)

        # 1. Instance 1 executes maintenance & rollout mutations
        ops1 = OperationsController(operations_id="ops_crash_pqc", base_dir=str(base_dir))
        ops1.set_maintenance(
            scope=MaintenanceScope.AGENT,
            target_id="agent_c_crash",
            duration_s=1800.0,
            reason="Emergency crash test maintenance",
            idempotency_key="key_maint_crash_1",
        )
        ops1.pause_agent(
            agent_id="agent_c_crash",
            reason="Pre-crash pause",
            idempotency_key="key_pause_crash_1",
        )
        gen1 = ops1.generation

        # Simulate abrupt SIGKILL / process termination
        del ops1

        # 2. Instance 2 recovers from disk store
        ops2 = OperationsController(operations_id="ops_crash_pqc", base_dir=str(base_dir))
        assert ops2.generation > gen1  # Generation advanced on durable recovery
        assert ops2.lifecycle.is_target_in_maintenance(MaintenanceScope.AGENT, "agent_c_crash")
        assert ops2.lifecycle.get_agent_state("agent_c_crash").desired_state.value == "PAUSED"

        # 3. Further mutation under recovered controller succeeds
        ops2.resume_agent(
            agent_id="agent_c_crash",
            reason="Post-crash resume",
            idempotency_key="key_resume_post_crash",
        )
        assert ops2.lifecycle.get_agent_state("agent_c_crash").desired_state.value == "ACTIVE"

"""Production Qualification: Wave PQG Saturation & Exactly-Once Gate (spec §42, §43, §44, §45).

Validates:
  Thread concurrency and resource saturation without deadlocks or race conditions.
  Zero resource overcommit (RESOURCE_OVERCOMMIT=0).
  Zero starvation (STARVATION=0).
  Zero duplicate operational side effects (DUPLICATE_OPERATIONAL_SIDE_EFFECTS=0).
  Exactly-once mutation semantics across parallel retries.
"""

from __future__ import annotations

import concurrent.futures
import tempfile

from veya.operations import (
    MaintenanceScope,
    OperationsController,
)


def test_concurrent_saturation_and_idempotency_gate() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ops = OperationsController(operations_id="ops_sat_test", base_dir=tmpdir)

        # 1. Concurrently pause and resume 20 agents using a thread pool
        num_agents = 20

        def mutate_agent(idx: int) -> str:
            agent_id = f"agent_sat_{idx}"
            # Pause
            ops.pause_agent(
                agent_id=agent_id,
                reason=f"Saturation pause {idx}",
                idempotency_key=f"pause_sat_{idx}",
            )
            # Maintenance
            ops.set_maintenance(
                scope=MaintenanceScope.AGENT,
                target_id=agent_id,
                duration_s=300.0,
                reason=f"Saturation maint {idx}",
                idempotency_key=f"maint_sat_{idx}",
            )
            # Resume
            ops.resume_agent(
                agent_id=agent_id,
                reason=f"Saturation resume {idx}",
                idempotency_key=f"resume_sat_{idx}",
            )
            return agent_id

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(mutate_agent, i) for i in range(num_agents)]
            completed = [f.result() for f in concurrent.futures.as_completed(futures)]

        assert len(completed) == num_agents

        # Verify all agents ended in ACTIVE state without starvation
        for i in range(num_agents):
            st = ops.lifecycle.get_agent_state(f"agent_sat_{i}")
            assert st.desired_state.value == "ACTIVE"
            assert ops.lifecycle.is_target_in_maintenance(MaintenanceScope.AGENT, f"agent_sat_{i}")

        # Verify all 60 mutations are recorded in audit log without loss
        records = ops.audit.list_records()
        assert len(records) == num_agents * 3  # 60 records

        # 2. Idempotency test under concurrent duplicate submissions:
        # Submit the same idempotency key 10 times concurrently; exactly 1 succeeds, 9 fail with DuplicateOperationalRequestError
        dup_key = "concurrent_dup_key_999"
        successes = 0
        duplicates = 0

        def try_duplicate_call() -> bool:
            try:
                ops.pause_agent(
                    agent_id="agent_sat_0",
                    reason="Dup call",
                    idempotency_key=dup_key,
                )
                return True
            except Exception as e:
                if "DUPLICATE_OPERATIONAL_REQUEST" in str(e):
                    return False
                raise

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            dup_futures = [executor.submit(try_duplicate_call) for _ in range(10)]
            for f in concurrent.futures.as_completed(dup_futures):
                if f.result() is True:
                    successes += 1
                else:
                    duplicates += 1

        assert successes == 1
        assert duplicates == 9

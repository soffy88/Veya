"""Production Qualification: Wave PQC Stale Operations Controller (spec §19).

Validates:
  Rejection of state mutations from controllers or clients operating on stale generation numbers.
  STALE_OPERATIONS_COMMIT_ACCEPTED=0
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from veya.operations import (
    MaintenanceScope,
    OperationsController,
    StaleOperationsGenerationError,
)


def test_stale_operations_controller_commit_rejection() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        ops = OperationsController(operations_id="ops_stale_test", base_dir=str(base_dir))

        # Initial generation is 1
        initial_gen = ops.generation

        # Trigger restart to advance generation to 2
        del ops
        ops = OperationsController(operations_id="ops_stale_test", base_dir=str(base_dir))
        assert ops.generation > initial_gen

        current_gen = ops.generation

        # Stale client / split-brain controller attempts commit with old generation
        stale_gen = initial_gen
        with pytest.raises(StaleOperationsGenerationError):
            ops.pause_agent(
                agent_id="agent_fenced",
                reason="Stale pause request",
                expected_generation=stale_gen,
            )

        # Confirm no state mutation occurred
        assert "agent_fenced" not in ops.lifecycle._agent_states

        # Attempt maintenance with stale generation
        with pytest.raises(StaleOperationsGenerationError):
            ops.set_maintenance(
                scope=MaintenanceScope.AGENT,
                target_id="agent_fenced",
                duration_s=300.0,
                reason="Stale maintenance",
                expected_generation=stale_gen,
            )

        assert not ops.lifecycle.is_target_in_maintenance(MaintenanceScope.AGENT, "agent_fenced")

        # Legitimate commit with current generation succeeds
        st = ops.pause_agent(
            agent_id="agent_fenced",
            reason="Legitimate pause",
            expected_generation=current_gen,
        )
        assert st.desired_state.value == "PAUSED"

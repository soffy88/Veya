"""Production Qualification: Wave PQE Multi-Principal Isolation (spec §30).

Validates:
  Multi-principal isolation barriers:
    CROSS_PRINCIPAL_OPERATIONAL_READ=0
    CROSS_PRINCIPAL_OPERATIONAL_WRITE=0
  Unauthorized cross-principal actions raise CrossPrincipalAccessDeniedError.
"""

from __future__ import annotations

import tempfile

import pytest

from veya.operations import (
    CrossPrincipalAccessDeniedError,
    OperationsController,
)


def test_multi_principal_operational_isolation() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ops = OperationsController(operations_id="ops_isolation_test", base_dir=tmpdir)

        # 1. Principal Alpha mutates its own agent
        st_alpha = ops.pause_agent(
            agent_id="agent_alpha_1",
            reason="Alpha routine check",
            actor="engineer_alpha",
            actor_principal="principal_alpha",
            target_principal="principal_alpha",
        )
        assert st_alpha.desired_state.value == "PAUSED"

        # 2. Principal Beta attempts to unpause / mutate Alpha's agent -> Denied!
        with pytest.raises(CrossPrincipalAccessDeniedError) as exc_info:
            ops.resume_agent(
                agent_id="agent_alpha_1",
                reason="Unauthorized intrusion",
                actor="engineer_beta",
                actor_principal="principal_beta",
                target_principal="principal_alpha",
            )
        assert "CROSS_PRINCIPAL_ACCESS_DENIED" in str(exc_info.value)
        assert exc_info.value.args[0].find("principal_beta") != -1

        # 3. Read isolation in Audit trail
        # Record mutation for Beta
        ops.pause_agent(
            agent_id="agent_beta_1",
            reason="Beta routine pause",
            actor="engineer_beta",
            actor_principal="principal_beta",
            target_principal="principal_beta",
        )

        records_alpha = ops.audit.list_records(principal_id="principal_alpha")
        assert len(records_alpha) == 1
        assert records_alpha[0].target == "agent_alpha_1"

        records_beta = ops.audit.list_records(principal_id="principal_beta")
        assert len(records_beta) == 1
        assert records_beta[0].target == "agent_beta_1"

        # System/Admin can see all records
        all_records = ops.audit.list_records(principal_id="system")
        assert len(all_records) >= 2

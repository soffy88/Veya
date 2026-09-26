"""Production Qualification: Runtime Generation Fencing (spec §15).

Validates:
  STALE_RUNTIME_GENERATION_ACCEPTED=0
  Old runtime process/session attempting lease renewal, ownership, or transition is denied.
"""

from __future__ import annotations

import pytest

from veya.operations import RolloutCoordinator, StaleGenerationCommitError


def test_runtime_generation_fencing_denials() -> None:
    coordinator = RolloutCoordinator()

    # Set active runtime generation to 4
    coordinator._agent_runtime_generations["agent_fenced_1"] = 4

    # 1. Stale commit with gen 1 -> DENIED
    with pytest.raises(StaleGenerationCommitError):
        coordinator.verify_runtime_generation("agent_fenced_1", submitted_gen=1)

    # 2. Stale commit with gen 3 -> DENIED
    with pytest.raises(StaleGenerationCommitError):
        coordinator.verify_runtime_generation("agent_fenced_1", submitted_gen=3)

    # 3. Matching generation 4 -> ACCEPTED
    assert coordinator.verify_runtime_generation("agent_fenced_1", submitted_gen=4)

    # 4. Future generation >= 4 -> ACCEPTED
    assert coordinator.verify_runtime_generation("agent_fenced_1", submitted_gen=5)

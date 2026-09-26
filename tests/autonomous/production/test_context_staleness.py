"""AQ-P3 & AQ-P24: Real Stale Context and Observation Freshness Tests.

Invariants:
- STALE_CONTEXT_GUARD=PASS: Outdated or superseded observations excluded from known facts.
- STALE_FACT_ACTION=0: No actions taken based on stale facts.
- EXPIRED_OBSERVATION_USED_FOR_CRITICAL_ACTION=0: Expired time-sensitive observations ignored.
- CONFLICT_RECONCILIATION=PASS: Conflicting claims reconciled without arbitrary selection.
"""

from __future__ import annotations

import tempfile
import time

from veya.autonomous import (
    AutonomousCycle,
    Observation,
    ObservationSource,
    ObservationStatus,
    SituationAssessor,
    reconcile_context,
)


def test_stale_observation_excluded_from_known_facts() -> None:
    """AQ-P3: Superseded observation is marked STALE and filtered out by SituationAssessor."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_stale_context",
            objective="Deploy to staging target",
            base_dir=tmpdir,
        )

        # Observation 1: initial endpoint IP
        obs1 = cycle.journal.append(
            mission_id="prod_stale_context",
            source=ObservationSource.TOOL,
            source_ref="dns_lookup",
            kind="IP_MAPPING",
            summary="staging.example.com resolved to 10.0.0.1",
            status=ObservationStatus.CURRENT,
        )

        # Observation 2: DNS rotated, supersedes Observation 1
        obs2 = cycle.journal.append(
            mission_id="prod_stale_context",
            source=ObservationSource.TOOL,
            source_ref="dns_lookup",
            kind="IP_MAPPING",
            summary="staging.example.com resolved to 10.0.0.99",
            status=ObservationStatus.CURRENT,
            supersedes=[obs1.observation_id],
        )

        # Reconcile context
        reconciled = reconcile_context(cycle.journal.query("prod_stale_context"))
        stale_obs = [o for o in reconciled if o.status == ObservationStatus.STALE]
        current_obs = [o for o in reconciled if o.status == ObservationStatus.CURRENT]

        assert any(o.observation_id == obs1.observation_id for o in stale_obs)
        assert any(o.observation_id == obs2.observation_id for o in current_obs)

        # Situation assessment must ONLY include current fact
        assessor = SituationAssessor()
        assessment = assessor.assess(
            mission_id="prod_stale_context",
            cycle_id="cycle_1",
            reconciled_context=reconciled,
        )

        assert "staging.example.com resolved to 10.0.0.99" in assessment.known_facts
        assert (
            "staging.example.com resolved to 10.0.0.1" not in assessment.known_facts
        )  # STALE_FACT_ACTION=0


def test_observation_freshness_expiration() -> None:
    """AQ-P24: Time-sensitive observation expiring past max_age is classified as STALE."""
    obs_old = Observation(
        observation_id="obs_token_old",
        mission_id="prod_freshness",
        source=ObservationSource.SYSTEM,
        summary="Auth token valid for 5 seconds",
        freshness=time.time() - 300.0,  # 300 seconds ago
        status=ObservationStatus.CURRENT,
    )

    obs_fresh = Observation(
        observation_id="obs_token_new",
        mission_id="prod_freshness",
        source=ObservationSource.SYSTEM,
        summary="Auth token refreshed recently",
        freshness=time.time() - 2.0,  # 2 seconds ago
        status=ObservationStatus.CURRENT,
    )

    reconciled = reconcile_context([obs_old, obs_fresh], max_age_s=60.0)
    assert len(reconciled.stale) == 1
    assert reconciled.stale[0].observation_id == "obs_token_old"
    assert len(reconciled.current) == 1
    assert reconciled.current[0].observation_id == "obs_token_new"


def test_conflicting_observations_tracked() -> None:
    """AQ-P23: Two contradictory sources flagged as CONFLICTING uncertainties."""
    obs_a = Observation(
        observation_id="obs_a",
        mission_id="prod_conflict",
        source=ObservationSource.TOOL,
        summary="Service reports port 80 open",
        status=ObservationStatus.CURRENT,
    )
    obs_b = Observation(
        observation_id="obs_b",
        mission_id="prod_conflict",
        source=ObservationSource.SYSTEM,
        summary="Security scanner reports port 80 blocked",
        status=ObservationStatus.CURRENT,
        contradicts=["obs_a"],
    )

    reconciled = reconcile_context([obs_a, obs_b])
    assert len(reconciled.conflicting) == 2

    assessor = SituationAssessor()
    assessment = assessor.assess(
        mission_id="prod_conflict",
        cycle_id="c1",
        reconciled_context=reconciled,
    )

    # Neither should be accepted into known_facts blindly
    assert not any("port 80 open" in fact for fact in assessment.known_facts)
    assert any("CONFLICT:" in u for u in assessment.uncertainties)

"""PQ §3: Real Concurrent Mission Test with Mixed Workloads.

Invariants:
- >= 10 concurrent missions mixed across read-heavy, write-heavy, test/build, multi-step, long-running, multi-repo.
- ALL_MISSIONS_ADMITTED_CORRECTLY=PASS
- DUPLICATE_PLACEMENT=0
- MISSION_LOSS=0
- DUPLICATE_SIDE_EFFECTS=0
- RESOURCE_OVERCOMMIT=0
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
    ResourceType,
)


def test_mixed_concurrent_missions_qualification() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(
            fleet_id="f_mixed_concurrent",
            base_dir=tmpdir,
            global_max_missions=50,
        )

        # Register resource pools
        controller.resources.register_pool("cpu_slots", ResourceType.CPU, capacity=20.0)
        controller.resources.register_pool("exec_slots", ResourceType.EXECUTOR_SLOT, capacity=20.0)

        # Register 5 agents with diverse capability profiles
        repo1 = Path(tmpdir) / "repo_a"
        repo2 = Path(tmpdir) / "repo_b"
        repo3 = Path(tmpdir) / "repo_c"

        for r in (repo1, repo2, repo3):
            r.mkdir(parents=True, exist_ok=True)

        profiles = [
            ("agent_read", [str(repo1), str(repo2)], ["read", "search"]),
            ("agent_write", [str(repo2), str(repo3)], ["write", "codegen"]),
            ("agent_build", [str(repo1), str(repo3)], ["build", "test"]),
            ("agent_multi", [str(repo1), str(repo2), str(repo3)], ["multistep", "coordination"]),
            (
                "agent_general",
                [str(repo1), str(repo2), str(repo3)],
                ["read", "write", "build", "test", "longrun"],
            ),
        ]

        for aid, scopes, caps in profiles:
            agent = AgentInstance(
                agent_instance_id=aid,
                fleet_id="f_mixed_concurrent",
                workspace_scope=scopes,
                capability_profile=caps,
                status=AgentInstanceStatus.READY,
            )
            controller.register_agent(agent)

        # 12 diverse concurrent missions
        mission_specs = [
            ("m_read_1", "read", str(repo1), False, 1.0),
            ("m_read_2", "search", str(repo2), False, 1.0),
            ("m_write_1", "write", str(repo2), True, 2.0),
            ("m_write_2", "codegen", str(repo3), True, 2.0),
            ("m_build_1", "build", str(repo1), False, 1.5),
            ("m_build_2", "test", str(repo3), False, 1.5),
            ("m_multi_1", "multistep", str(repo1), False, 1.0),
            ("m_multi_2", "coordination", str(repo2), False, 1.0),
            ("m_long_1", "longrun", str(repo3), False, 2.0),
            ("m_long_2", "longrun", str(repo1), False, 2.0),
            ("m_repo_cross_1", "read", str(repo3), False, 1.0),
            ("m_repo_cross_2", "build", str(repo2), False, 1.0),
        ]

        placements = []
        for m_id, cap, ws, is_mut, cpu_req in mission_specs:
            # Reserve resource with overcommit protection
            res = controller.resources.reserve(
                mission_id=m_id,
                pool_id="cpu_slots",
                quantity=cpu_req,
            )
            assert res is not None

            req = PlacementRequest(
                mission_id=m_id,
                principal_id="principal_mix",
                required_capabilities=[cap],
                workspace_requirements={"workspace_path": ws, "requires_mutation": is_mut},
            )
            plc, status = controller.admit_and_schedule(req)
            assert plc is not None, f"Mission '{m_id}' placement failed: {status}"
            placements.append(plc)

        # Invariant Assertions
        assert len(placements) == 12, "All 12 missions must be admitted and scheduled"

        # Unique placements (DUPLICATE_PLACEMENT=0)
        placement_ids = [p.placement_id for p in placements]
        assert len(placement_ids) == len(set(placement_ids)), "DUPLICATE_PLACEMENT must be 0"

        # Mission loss check (MISSION_LOSS=0)
        active_plc_missions = {
            p.mission_id for p in controller.registry.list_placements() if p.status == "ACTIVE"
        }
        for m_id, _, _, _, _ in mission_specs:
            assert m_id in active_plc_missions, f"Mission {m_id} lost from active registry"

        # Resource overcommit check (RESOURCE_OVERCOMMIT=0)
        cpu_pool = controller.registry.get_pool("cpu_slots")
        assert cpu_pool is not None
        assert cpu_pool.allocated + cpu_pool.reserved <= cpu_pool.capacity, (
            "RESOURCE_OVERCOMMIT must be 0"
        )

        # Idempotent re-submission check (DUPLICATE_SIDE_EFFECTS=0)
        for m_id, cap, ws, is_mut, _ in mission_specs[:3]:
            dup_req = PlacementRequest(
                mission_id=m_id,
                principal_id="principal_mix",
                required_capabilities=[cap],
                workspace_requirements={"workspace_path": ws, "requires_mutation": is_mut},
            )
            dup_plc, dup_status = controller.admit_and_schedule(dup_req)
            assert dup_plc is not None
            assert dup_status == "IDEMPOTENT_EXISTING_PLACEMENT"
            assert dup_plc.placement_id in placement_ids

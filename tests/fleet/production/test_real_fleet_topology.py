"""PQ §2: Real Fleet Topology Qualification.

Invariants:
- AGENT_COUNT >= 5
- CONCURRENT_MISSIONS >= 10
- DISTINCT_REPOS >= 3 (Veya, HEVI, STRATUM)
- Each AgentInstance has independent agent_instance_id, generation, lease, runtime identity.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)


def _init_git_repo(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", f"{name}Bot"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", f"{name.lower()}@veya.local"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text(f"# {name} Repository\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"], cwd=path, check=True, capture_output=True
    )


def test_real_fleet_topology_qualification() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = Path(tmpdir)
        repo_veya = base_path / "repos" / "veya"
        repo_hevi = base_path / "repos" / "hevi"
        repo_stratum = base_path / "repos" / "stratum"

        _init_git_repo(repo_veya, "Veya")
        _init_git_repo(repo_hevi, "HEVI")
        _init_git_repo(repo_stratum, "STRATUM")

        controller = FleetController(
            fleet_id="f_topology",
            base_dir=tmpdir,
            global_max_missions=50,
        )

        # Register 5 distinct AgentInstances across 3 failure domains and runtimes
        agents_data = [
            ("agent_veya_1", "rt_veya_primary", [str(repo_veya)], ["python", "build"], "zone-a"),
            ("agent_veya_2", "rt_veya_worker", [str(repo_veya)], ["python", "test"], "zone-a"),
            ("agent_hevi_1", "rt_hevi_primary", [str(repo_hevi)], ["rust", "cuda"], "zone-b"),
            ("agent_hevi_2", "rt_hevi_worker", [str(repo_hevi)], ["rust", "test"], "zone-b"),
            (
                "agent_stratum_1",
                "rt_stratum_all",
                [str(repo_stratum)],
                ["golang", "deploy"],
                "zone-c",
            ),
        ]

        for aid, rtid, ws, caps, zone in agents_data:
            agent = AgentInstance(
                agent_instance_id=aid,
                fleet_id="f_topology",
                runtime_id=rtid,
                workspace_scope=ws,
                capability_profile=caps,
                failure_domain=zone,
                status=AgentInstanceStatus.READY,
            )
            controller.register_agent(agent)

        all_agents = controller.registry.list_agents()
        assert len(all_agents) >= 5, f"Expected AGENT_COUNT >= 5, got {len(all_agents)}"

        # Check distinct repo scopes
        all_scopes = {scope for a in all_agents for scope in a.workspace_scope}
        assert len(all_scopes) >= 3, f"Expected DISTINCT_REPOS >= 3, got {len(all_scopes)}"

        # Submit 10 concurrent missions targeted across the 3 repositories
        placements = []
        for i in range(1, 11):
            if i % 3 == 1:
                target_repo = str(repo_veya)
                req_caps = ["python"]
            elif i % 3 == 2:
                target_repo = str(repo_hevi)
                req_caps = ["rust"]
            else:
                target_repo = str(repo_stratum)
                req_caps = ["golang"]

            req = PlacementRequest(
                mission_id=f"m_topo_{i}",
                principal_id=f"user_{i % 3}",
                required_capabilities=req_caps,
                workspace_requirements={"workspace_path": target_repo},
            )
            plc, status = controller.admit_and_schedule(req)
            assert plc is not None, f"Mission {i} failed to place: {status}"
            assert plc.status == "ACTIVE"
            placements.append(plc)

        assert len(placements) == 10, f"Expected CONCURRENT_MISSIONS >= 10, got {len(placements)}"

        # Verify each agent instance has independent generation, lease, and runtime identity
        leases = controller.registry.list_leases()
        active_leases = [ls for ls in leases if ls.status.value == "ACTIVE"]
        assert len(active_leases) == 10

        lease_holders = {ls.agent_instance_id for ls in active_leases}
        assert len(lease_holders) >= 3, "Placements must span across distinct agents"

        for p in placements:
            assert p.generation >= 1
            agent = controller.registry.get_agent(p.agent_instance_id)
            assert agent is not None
            assert agent.runtime_id == p.runtime_id

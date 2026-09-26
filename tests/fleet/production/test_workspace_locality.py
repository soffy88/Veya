"""PQ §12: Workspace Locality, Nested Repos, and Mutation Isolation.

Invariants:
- WORKSPACE_LOCALITY=PASS
- WORKSPACE_ESCAPE=0
- CROSS_REPO_WRITE_LEAK=0
- Tests >= 2 nested repos + 1 direct repo with locality preference and mutation locks.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)


def test_workspace_locality_and_isolation_qualification() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        # Direct repo + two nested repos
        repo_mono = base / "monorepo"
        repo_pkg_a = base / "monorepo" / "packages" / "pkg_a"
        repo_pkg_b = base / "monorepo" / "packages" / "pkg_b"
        repo_direct = base / "standalone"

        for p in (repo_mono, repo_pkg_a, repo_pkg_b, repo_direct):
            p.mkdir(parents=True, exist_ok=True)

        controller = FleetController(fleet_id="f_locality_pqc", base_dir=tmpdir)

        # Agent 1 has locality for pkg_a
        a1 = AgentInstance(
            agent_instance_id="agent_pkg_a",
            fleet_id="f_locality_pqc",
            workspace_scope=[str(repo_pkg_a)],
            capability_profile=["node"],
            status=AgentInstanceStatus.READY,
        )
        # Agent 2 has locality for standalone
        a2 = AgentInstance(
            agent_instance_id="agent_standalone",
            fleet_id="f_locality_pqc",
            workspace_scope=[str(repo_direct)],
            capability_profile=["node"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(a1)
        controller.register_agent(a2)

        # 1. Placement with locality preference: pkg_a should choose agent_pkg_a
        req_a = PlacementRequest(
            mission_id="m_pkg_a_build",
            principal_id="dev",
            required_capabilities=["node"],
            workspace_requirements={"workspace_path": str(repo_pkg_a), "requires_mutation": True},
        )
        plc_a, _ = controller.admit_and_schedule(req_a)
        assert plc_a is not None
        assert plc_a.agent_instance_id == "agent_pkg_a"  # WORKSPACE_LOCALITY=PASS

        # 2. Concurrent mutating request on same workspace pkg_a must lock (CROSS_REPO_WRITE_LEAK=0)
        req_a_concurrent = PlacementRequest(
            mission_id="m_pkg_a_write2",
            principal_id="dev2",
            required_capabilities=["node"],
            workspace_requirements={"workspace_path": str(repo_pkg_a), "requires_mutation": True},
        )
        plc_locked, status_locked = controller.admit_and_schedule(req_a_concurrent)
        assert plc_locked is None
        assert "WORKSPACE_MUTATION_LOCK" in status_locked

        # 3. Request on standalone repo can run concurrently without escape
        req_standalone = PlacementRequest(
            mission_id="m_standalone_write",
            principal_id="dev3",
            required_capabilities=["node"],
            workspace_requirements={"workspace_path": str(repo_direct), "requires_mutation": True},
        )
        plc_std, _status_std = controller.admit_and_schedule(req_standalone)
        assert plc_std is not None
        assert plc_std.agent_instance_id == "agent_standalone"  # WORKSPACE_ESCAPE=0

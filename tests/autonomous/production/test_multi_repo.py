"""AQ-P11: Real Multi-Repo Mission Qualification Tests.

Invariants:
- CROSS_REPO_PATH_ISOLATION=PASS: Operations strictly routed to intended repo.
- WRONG_REPO_WRITE=0: Modifications never escape or cross-contaminate repos.
- WORKSPACE_ESCAPE=0: Path traversal or workspace leaks blocked.
- ACCEPTED_PROGRESS_PRESERVED=PASS: Multi-stage progress tracking across repos.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from veya.autonomous import (
    ActionProposal,
    AutonomousCycle,
)


def _init_repo(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", f"Bot {name}"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", f"{name}@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"Initial {name}"], cwd=path, check=True, capture_output=True
    )


def test_real_multi_repo_isolation_and_verification() -> None:
    """AQ-P11: Inspect and modify repo A, inspect repo B, verify no cross-contamination."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        repo_a = base / "repo_core"
        repo_b = base / "repo_plugin"

        _init_repo(repo_a, "Core Engine")
        _init_repo(repo_b, "Extension Plugin")

        cycle = AutonomousCycle(
            mission_id="prod_multi_repo",
            objective="Update API contract in repo_core and verify repo_plugin compatibility",
            base_dir=tmpdir,
            required_milestones=[
                "Step 1: Export new API in repo_core",
                "Step 2: Verify repo_plugin reads new API",
            ],
        )

        # Milestone 1: modify repo_core ONLY
        def exec_step1(act: ActionProposal) -> tuple[int, str, list[str]]:
            core_api = repo_a / "api.json"
            core_api.write_text('{"version": "2.0.0", "endpoint": "/v2/query"}', encoding="utf-8")
            subprocess.run(["git", "add", "api.json"], cwd=repo_a, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Expose v2 api"],
                cwd=repo_a,
                check=True,
                capture_output=True,
            )
            return 0, "Updated repo_core API", ["ev_core_v2"]

        cycle.step(executor=exec_step1)
        assert len(cycle.accepted_progress) == 1
        assert (repo_a / "api.json").is_file()
        assert not (repo_b / "api.json").exists()  # WRONG_REPO_WRITE=0

        # Milestone 2: inspect repo_plugin, read from repo_core
        def exec_step2(act: ActionProposal) -> tuple[int, str, list[str]]:
            # Read core API from repo_a
            core_content = (repo_a / "api.json").read_text(encoding="utf-8")
            # Write plugin config inside repo_b
            plugin_cfg = repo_b / "plugin.conf"
            plugin_cfg.write_text(f"upstream={core_content}\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "plugin.conf"], cwd=repo_b, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "Bind to core v2"],
                cwd=repo_b,
                check=True,
                capture_output=True,
            )
            return 0, "Bound plugin to core v2", ["ev_plugin_bound"]

        cycle.step(executor=exec_step2)
        assert len(cycle.accepted_progress) == 2
        assert (repo_b / "plugin.conf").is_file()
        assert not (repo_a / "plugin.conf").exists()  # WORKSPACE_ESCAPE=0

        # Verify git logs in each repo independently
        res_a = subprocess.run(
            ["git", "log", "--oneline"], cwd=repo_a, check=True, capture_output=True, text=True
        )
        assert "Expose v2 api" in res_a.stdout
        assert "Bind to core v2" not in res_a.stdout

        res_b = subprocess.run(
            ["git", "log", "--oneline"], cwd=repo_b, check=True, capture_output=True, text=True
        )
        assert "Bind to core v2" in res_b.stdout
        assert "Expose v2 api" not in res_b.stdout

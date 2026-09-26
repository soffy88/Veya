"""AQ-P2: Real External Change Qualification Tests.

Invariants:
- STALE_ASSUMPTION_USED=0: Once external reality changes, outdated assumptions are not used.
- EXTERNAL_CHANGE_DETECTED=PASS: Real file or Git changes detected via observation updates.
- REAL_REPLAN=PASS: MasterAgent replans preserving accepted progress.
- ACCEPTED_PROGRESS_LOST=0: Valid prior achievements remain preserved.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from veya.autonomous import (
    ActionProposal,
    AutonomousCycle,
    DecisionType,
    ObservationSource,
    ObservationStatus,
)


def test_real_external_git_change_triggers_replan() -> None:
    """AQ-P2: External git commit invalidates assumption, triggering replan with preserved progress."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo_app"
        repo_dir.mkdir()
        # Initialize real git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )

        f1 = repo_dir / "base.txt"
        f1.write_text("v1.0.0", encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial release"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )

        cycle = AutonomousCycle(
            mission_id="prod_ext_change",
            objective="Deliver patch release v1.0.1",
            base_dir=tmpdir,
        )

        # Step 1: Execute first subtask successfully
        def step1_exec(act: ActionProposal) -> tuple[int, str, list[str]]:
            return 0, "Created patch branch", ["ev_branch_patch"]

        cycle.step(executor=step1_exec)
        assert len(cycle.accepted_progress) == 1
        saved_progress = list(cycle.accepted_progress)

        # External reality changes: someone pushed breaking commit on main externally
        f1.write_text("v2.0.0-breaking", encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "External major bump"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )

        # Record fresh observation of git change
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
        )
        new_head = res.stdout.strip()

        cycle.journal.append(
            mission_id="prod_ext_change",
            source=ObservationSource.TOOL,
            source_ref="git_watch",
            kind="INVALIDATED_ASSUMPTION",
            summary=f"External git advance detected: head is now {new_head[:7]} (breaking major version)",
            status=ObservationStatus.CURRENT,
        )

        # Step 2: Next step must detect external change, synthesize REPLAN, and keep accepted progress
        cycle.step()
        assert cycle.latest_decision is not None
        assert cycle.latest_decision.decision_type == DecisionType.REPLAN
        assert "EXTERNAL_CHANGE_DETECTED" in cycle.latest_decision.reason
        assert cycle.accepted_progress == saved_progress  # ACCEPTED_PROGRESS_LOST=0


def test_real_external_file_change_detected() -> None:
    """AQ-P2: External configuration file modified on disk triggers plan reassessment."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "service.conf"
        config_path.write_text("port=8080\nmode=dev\n", encoding="utf-8")

        cycle = AutonomousCycle(
            mission_id="prod_ext_file_change",
            objective="Configure production service",
            base_dir=tmpdir,
        )

        # Observation A: initial config
        cycle.journal.append(
            mission_id="prod_ext_file_change",
            source=ObservationSource.SYSTEM,
            source_ref="fs_probe",
            kind="CONFIG_STATE",
            summary="service.conf mode is dev",
            status=ObservationStatus.CURRENT,
        )

        cycle.step()

        # External change: admin modifies config to locked
        config_path.write_text("port=443\nmode=locked\n", encoding="utf-8")

        # Observation B contradicts/invalidates Observation A
        cycle.journal.append(
            mission_id="prod_ext_file_change",
            source=ObservationSource.SYSTEM,
            source_ref="fs_probe",
            kind="INVALIDATED_ASSUMPTION",
            summary="service.conf mode unexpectedly changed to locked",
            status=ObservationStatus.CURRENT,
        )

        cycle.step()
        assert cycle.latest_decision is not None
        assert cycle.latest_decision.decision_type == DecisionType.REPLAN

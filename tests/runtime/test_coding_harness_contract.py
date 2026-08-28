from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.coding.tools import coding_worktree_create
from runtime.coding.workspace_detect import detect_workspace
from runtime.harness.contract import build_coding_harness_contract
from runtime.harness.guides import load_guides
from runtime.harness.sensors import sensors_for_workspace


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "harness@example.invalid")
    _git(root, "config", "user.name", "Harness Tests")
    (root / ".gitignore").write_text(".veya/\n", encoding="utf-8")
    (root / "README.md").write_text("harness\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def test_contract_binds_guides_required_sensors_permissions_and_artifacts(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text(
        "## Commands\n- test: pytest\n- lint: ruff check .\n",
        encoding="utf-8",
    )
    workspace = detect_workspace(root)
    guides = load_guides(workspace)
    sensors = sensors_for_workspace(workspace, guides)

    contract = build_coding_harness_contract(
        workspace,
        "task-contract",
        guides=guides,
        sensors=sensors,
    )

    assert contract.workspace_id == workspace.id
    assert contract.guide_refs == [str(root / "AGENTS.md")]
    assert contract.required_sensors
    assert contract.permission_profile == "DEVELOPMENT"
    assert contract.artifact_policy == ".veya/runs/task-contract/outputs"
    assert contract.answers()["allowed_writes"] == "task worktree only"


def test_coding_worktree_creation_persists_contract_before_task_execution(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("## Rules\n- Keep the worktree isolated.\n", encoding="utf-8")

    result = coding_worktree_create(str(root), "task-contract", "contract test")

    assert result["status"] == "ok"
    contract = result["data"]["harness_contract"]
    assert contract["workspace_id"] == detect_workspace(root).id
    contract_path = Path(result["data"]["harness_contract_path"])
    assert contract_path.is_file()
    assert contract["permission_profile"] == "DEVELOPMENT"
    assert contract["artifact_policy"].endswith("outputs")

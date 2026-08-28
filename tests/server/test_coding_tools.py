from __future__ import annotations

import json
import subprocess
from pathlib import Path

from runtime.coding.tools import (
    coding_apply_patch,
    coding_finalize_patch,
    coding_run_command,
    coding_run_tests,
    coding_workspace_detect,
    coding_worktree_create,
    register_tools,
)
from server.tool_registry import MasterToolRegistry, master_tools


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
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Coding Tests")
    (root / ".gitignore").write_text(".veya/\n", encoding="utf-8")
    (root / "app.py").write_text("print('base')\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\ndependencies = ['pytest']\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def test_coding_tools_register_additively_and_idempotently():
    registry = MasterToolRegistry()
    assert register_tools(registry) == 12
    assert register_tools(registry) == 0
    names = set(registry.list_tools())
    assert {
        "coding_workspace_detect",
        "coding_worktree_create",
        "coding_worktree_status",
        "coding_diff",
        "coding_apply_patch",
        "coding_discard",
        "coding_run_command",
        "coding_run_tests",
        "coding_run_lint",
        "coding_run_typecheck",
        "coding_build",
        "coding_finalize_patch",
    } <= names
    assert {name for name in master_tools.list_tools() if name.startswith("coding_")} >= names


def test_coding_tool_flow_emits_patch_verification_and_artifacts(tmp_path: Path):
    root = _repo(tmp_path)
    detected = coding_workspace_detect(str(root))
    assert detected["status"] == "ok"
    created = coding_worktree_create(str(root), "task-42", "fix test")
    assert created["status"] == "ok"
    worktree = created["data"]["worktree"]["path"]

    patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-print('base')
+print('fixed')
"""
    applied = coding_apply_patch(worktree, patch)
    assert applied["status"] == "ok"

    command = coding_run_command(worktree, "python3 -c \"print('command ok')\"")
    tests = coding_run_tests(worktree, "python3 -c \"print('tests ok')\"")
    assert command["status"] == "ok"
    assert tests["status"] == "ok"
    assert tests["command_results"][0]["status"] == "passed"

    finalized = coding_finalize_patch(
        worktree,
        verification={
            "tests_passed": True,
            "acceptance_passed": True,
            "profile": "local_trusted",
            "sensor_results": [tests["data"]["sensor_result"]],
        },
        run_id="run-42",
    )
    assert finalized["status"] == "ok"
    assert finalized["data"]["patch"]["verified"] is True
    artifact_paths = [Path(item["path"]) for item in finalized["artifacts"]]
    assert {path.name for path in artifact_paths} == {
        "harness_contract.json",
        "patch.diff",
        "sensor_report.json",
        "verification_report.json",
        "summary.md",
    }
    output_root = root / ".veya" / "runs" / "task-42"
    assert all((output_root / path).is_file() for path in artifact_paths)
    manifest = Path(finalized["data"]["manifest_path"])
    assert manifest.is_file()
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert any(item["kind"] == "sensor_report" for item in manifest_data["artifacts"])
    assert finalized["data"]["verification_report"]["sensor_results"]
    assert "No commit or remote operation was performed." in (
        root / ".veya" / "runs" / "task-42" / "outputs" / "summary.md"
    ).read_text(encoding="utf-8")
    assert _git(root, "branch", "--show-current") == "main"


def test_coding_command_cannot_use_main_worktree(tmp_path: Path):
    root = _repo(tmp_path)
    result = coding_run_command(str(root), "python3 -c 'print(1)'")
    assert result["status"] == "failed"
    assert "Veya worktree" in result["evidence"][0]["message"]

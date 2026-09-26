from pathlib import Path

from runtime.coding.command_runner import CommandRunner
from runtime.coding.sandbox_profiles import get_sandbox_profile


def test_workspace_full_is_networked_and_default(tmp_path: Path) -> None:
    runner = CommandRunner(tmp_path)
    assert runner.profile.id == "l0_workspace_full"
    assert runner.profile.network == "allowed"
    assert get_sandbox_profile("l0_isolated").network == "denied"


def test_workspace_full_allows_project_write_without_runner_approval(tmp_path: Path) -> None:
    result = CommandRunner(tmp_path).run(["touch", "created.txt"])
    assert result.status == "passed"
    assert (tmp_path / "created.txt").is_file()

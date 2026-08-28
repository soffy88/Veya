from __future__ import annotations

import json
from pathlib import Path

from runtime.coding.command_runner import CommandPolicyError, CommandRunner, parse_command
from runtime.coding.sandbox_profiles import get_sandbox_profile, list_sandbox_profiles


def test_profiles_are_explicit_and_do_not_mount_secrets():
    profiles = {profile.id: profile for profile in list_sandbox_profiles()}

    assert set(profiles) == {"local_trusted", "local_restricted", "docker_python", "docker_node"}
    assert profiles["local_trusted"].network == "allowed"
    assert profiles["local_restricted"].network == "denied"
    assert profiles["local_restricted"].approvals == "required_for_write"
    for profile in profiles.values():
        assert all(mount.target not in {"/home", "/root"} for mount in profile.mounts)
        assert all("secret" not in mount.target.lower() for mount in profile.mounts)


def test_command_parser_rejects_shell_escape_and_runner_captures_redacted_artifact(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    with_error = ["echo ok && touch outside", "bash -lc 'echo bypass'", "echo hi > file"]
    for command in with_error:
        try:
            parse_command(command)
        except CommandPolicyError:
            pass
        else:
            raise AssertionError(f"unsafe command accepted: {command}")

    runner = CommandRunner(root, profile="local_restricted", artifact_root=root / ".veya" / "outputs")
    result = runner.run(
        ["python3", "-c", "import os; print(os.getenv('API_KEY', 'missing')); print('ok')"],
        env={"API_KEY": "do-not-leak"},
    )

    assert result.status == "passed"
    assert "do-not-leak" not in result.stdout
    assert "missing" in result.stdout
    assert "do-not-leak" not in json.dumps(result.to_dict())
    assert result.artifact_path
    artifact = json.loads(Path(result.artifact_path).read_text(encoding="utf-8"))
    assert artifact["status"] == "passed"
    assert "do-not-leak" not in json.dumps(artifact)

    home_probe = runner.run(["python3", "-c", "import os; print(os.listdir('/home'))"])
    assert home_probe.status == "passed"
    assert home_probe.stdout.strip() == "[]"


def test_restricted_runner_denies_network_and_outside_cwd(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    runner = CommandRunner(root, profile=get_sandbox_profile("local_restricted"))

    network = runner.run("curl https://example.invalid")
    outside = runner.run("python3 -c 'print(1)'", cwd=tmp_path)

    assert network.status == "denied"
    assert "network access is denied" in network.stderr
    assert outside.status == "denied"
    assert "inside the task workspace" in outside.stderr

    outside_file = tmp_path / "outside-worktree.txt"
    write_outside = runner.run(
        [
            "python3",
            "-c",
            "import sys; open(sys.argv[1], 'w').write('must fail')",
            str(outside_file),
        ]
    )
    assert write_outside.status == "failed"
    assert not outside_file.exists()


def test_timeout_and_docker_command_boundary(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    runner = CommandRunner(root, profile="local_restricted")
    timeout = runner.run("python3 -c 'import time; time.sleep(1)'", timeout_s=0.01)
    assert timeout.status == "timeout"
    assert timeout.timed_out is True

    docker_runner = CommandRunner(root, profile="docker_python")
    wrapped = docker_runner._docker_argv(["pytest"], root, None)
    assert wrapped[:6] == ["docker", "run", "--rm", "--network", "none", "--workdir"]
    assert "--mount" in wrapped
    assert "/home" not in " ".join(wrapped)

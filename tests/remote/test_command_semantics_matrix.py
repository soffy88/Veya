"""Adversarial command-semantics matrix for the canonical PermissionEngine.

Every row is checked through four callers. The invariant under test is that
identical operation semantics produce an identical permission decision, and that
no caller independently downgrades ``APPROVAL_REQUIRED`` to ``ALLOW``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runtime.coding.command_runner import command_requires_approval
from runtime.harness.sensors import sensor_command_is_safe
from veya.remote.permission_engine import (
    CommandEffect,
    Decision,
    OperationContext,
    PermissionEngine,
    parse_command_context,
)


class _Sensor:
    """Minimal stand-in for the harness sensor record."""

    def __init__(self, command: str) -> None:
        self.command = command
        self.id = "probe"
        self.deterministic = True
        self.kind = "command"
        self.cost_level = "free"
        self.required = True
        self.timeout_s = 30


# (label, command, expected effect, expected decision)
MATRIX: list[tuple[str, str, CommandEffect, Decision]] = [
    # ── safe shell ───────────────────────────────────────────────────────
    ("safe shell: ls", "ls -la", CommandEffect.READ_ONLY, Decision.ALLOW),
    ("safe shell: cat", "cat notes.txt", CommandEffect.READ_ONLY, Decision.ALLOW),
    ("safe shell: grep", "rg todo src", CommandEffect.READ_ONLY, Decision.ALLOW),
    ("safe shell: find (read)", "find . -name '*.py'", CommandEffect.READ_ONLY, Decision.ALLOW),
    (
        "safe shell: network fetch",
        "curl https://example.com",
        CommandEffect.READ_ONLY,
        Decision.ALLOW,
    ),
    # ── project mutation ─────────────────────────────────────────────────
    (
        "project mutation: mkdir",
        "mkdir -p src/pkg",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
    ),
    ("project mutation: touch", "touch new.txt", CommandEffect.REVERSIBLE_MUTATION, Decision.ALLOW),
    ("project mutation: cp", "cp a.txt b.txt", CommandEffect.REVERSIBLE_MUTATION, Decision.ALLOW),
    # ── destructive mutation ─────────────────────────────────────────────
    (
        "destructive: rm file",
        "rm build.txt",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "destructive: rm -rf dir",
        "rm -rf build",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "destructive: dd to device",
        "dd if=/dev/zero of=/dev/sda",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "destructive: chmod",
        "chmod 777 .",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "destructive: chown",
        "chown -R root:root .",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "destructive: shred",
        "shred -u secret.txt",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "destructive: truncate",
        "truncate -s 0 data.db",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "destructive: find -delete",
        "find . -name '*.log' -delete",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "destructive: find -exec",
        "find . -name x -exec rm {}",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "destructive: signal",
        "kill -9 1",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    # ── unknown destructive target (no resolvable target) ───────────────
    (
        "unknown target: signal",
        "kill -9 1",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "unknown target: git clean",
        "git clean -fd",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    # ── sudo ─────────────────────────────────────────────────────────────
    (
        "sudo: gated",
        "sudo apt install x",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "sudo: destructive",
        "sudo rm -rf /",
        CommandEffect.PRIVILEGED_HOST_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    # ── systemd ──────────────────────────────────────────────────────────
    (
        "systemd: user",
        "systemctl --user restart app",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
    ),
    (
        "systemd: system",
        "systemctl restart nginx",
        CommandEffect.PRIVILEGED_HOST_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    # ── safe git ─────────────────────────────────────────────────────────
    ("git safe: status", "git status", CommandEffect.READ_ONLY, Decision.ALLOW),
    ("git safe: diff", "git diff", CommandEffect.READ_ONLY, Decision.ALLOW),
    ("git safe: log", "git log --oneline", CommandEffect.READ_ONLY, Decision.ALLOW),
    ("git safe: show", "git show HEAD", CommandEffect.READ_ONLY, Decision.ALLOW),
    ("git safe: rev-parse", "git rev-parse HEAD", CommandEffect.READ_ONLY, Decision.ALLOW),
    # ── normal git mutation ──────────────────────────────────────────────
    ("git normal: add", "git add .", CommandEffect.REVERSIBLE_MUTATION, Decision.ALLOW),
    (
        "git normal: commit",
        "git commit -m change",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
    ),
    ("git normal: merge", "git merge feature", CommandEffect.REVERSIBLE_MUTATION, Decision.ALLOW),
    ("git normal: rebase", "git rebase main", CommandEffect.REVERSIBLE_MUTATION, Decision.ALLOW),
    (
        "git normal: worktree",
        "git worktree add ../probe feature",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
    ),
    # ── destructive git ──────────────────────────────────────────────────
    (
        "git destructive: clean",
        "git clean -fdx",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "git destructive: reset --hard",
        "git reset --hard",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "git destructive: restore",
        "git restore file.txt",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "git destructive: branch -D",
        "git branch -D feature",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "git destructive: branch --force",
        "git branch --force other",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "git destructive: checkout --orphan",
        "git checkout --orphan x",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "git destructive: gc",
        "git gc --prune",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    # ── remote git ───────────────────────────────────────────────────────
    (
        "git remote: push",
        "git push origin main",
        CommandEffect.REMOTE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "git remote: push --force",
        "git push --force",
        CommandEffect.REMOTE_IRREVERSIBLE_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    # ── package managers ─────────────────────────────────────────────────
    (
        "pkg: pip install (project)",
        "pip install requests",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
    ),
    (
        "pkg: npm install (project)",
        "npm install left-pad",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
    ),
    (
        "pkg: yarn remove (project)",
        "yarn remove left-pad",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
    ),
    (
        "pkg: go get (project)",
        "go get example.com/pkg",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
    ),
    (
        "pkg: pip -g (global)",
        "pip install -g x",
        CommandEffect.PRIVILEGED_HOST_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "pkg: npm -g (global)",
        "npm install -g x",
        CommandEffect.PRIVILEGED_HOST_MUTATION,
        Decision.APPROVAL_REQUIRED,
    ),
    (
        "pkg: cargo update (project)",
        "cargo update",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
    ),
]


def _labels() -> list[str]:
    return [row[0] for row in MATRIX]


def _engine(command: str, root: Path) -> tuple[CommandEffect, Decision, OperationContext]:
    context = parse_command_context(command, cwd=root, workspace_root=root)
    decision = PermissionEngine().evaluate(context)
    return CommandEffect(context.command_effect), decision.decision, context


@pytest.mark.parametrize(("label", "command", "effect", "decision"), MATRIX, ids=_labels())
def test_permission_engine_classifies_command_semantics(
    tmp_path: Path, label: str, command: str, effect: CommandEffect, decision: Decision
) -> None:
    """The engine reports the documented effect and decision for each row."""
    got_effect, got_decision, _ = _engine(command, tmp_path)
    assert got_effect is effect, f"{label}: effect {got_effect} != {effect}"
    assert got_decision is decision, f"{label}: decision {got_decision} != {decision}"


@pytest.mark.parametrize(("label", "command", "effect", "decision"), MATRIX, ids=_labels())
def test_command_requires_approval_agrees_with_engine(
    tmp_path: Path, label: str, command: str, effect: CommandEffect, decision: Decision
) -> None:
    """The compatibility adapter must never disagree with the engine."""
    _got_effect, got_decision, _ctx = _engine(command, tmp_path)
    approved = command_requires_approval(command.split())
    assert approved is (got_decision is not Decision.ALLOW), (
        f"{label}: command_requires_approval disagrees with the engine "
        f"(engine={got_decision}, adapter={approved})"
    )


@pytest.mark.parametrize(("label", "command", "effect", "decision"), MATRIX, ids=_labels())
def test_harness_sensor_agrees_with_engine(
    label: str, command: str, effect: CommandEffect, decision: Decision
) -> None:
    """The harness may be stricter, never laxer, than the engine.

    ``sensor_command_is_safe`` also enforces the doctor's offline-only rule, so a
    network command can be unsafe for a second, legitimate reason. The invariant
    that matters is one-directional: anything the engine gates must also be
    refused by the harness.
    """
    safe, detail = sensor_command_is_safe(_Sensor(command))  # type: ignore[arg-type]
    root = Path("/tmp")
    _effect, got_decision, _ctx = _engine(command, root)
    if got_decision is not Decision.ALLOW:
        assert safe is False, f"{label}: engine gated it but the harness allowed it ({detail})"


def test_no_caller_downgrades_approval_required(tmp_path: Path) -> None:
    """APPROVAL_REQUIRED must stay APPROVAL_REQUIRED at every boundary."""
    gated = [row for row in MATRIX if row[3] is not Decision.ALLOW]
    assert gated, "matrix must contain gated commands"
    for label, command, _effect, decision in gated:
        _got_effect, got_decision, _ctx = _engine(command, tmp_path)
        assert got_decision is decision, label
        assert command_requires_approval(command.split()) is True, label
        safe, _detail = sensor_command_is_safe(_Sensor(command))  # type: ignore[arg-type]
        assert safe is False, label


def test_scope_and_danger_are_orthogonal(tmp_path: Path) -> None:
    """A destructive command is gated even when its target is inside the project."""
    context = parse_command_context("rm -rf build", cwd=tmp_path, workspace_root=tmp_path)
    decision = PermissionEngine().evaluate(context)
    assert context.target_paths, "target must be resolved"
    assert all(tmp_path == path or tmp_path in path.parents for path in context.target_paths), (
        "target is project-scoped"
    )
    assert decision.decision is Decision.APPROVAL_REQUIRED
    assert decision.scope.value == "PROJECT"


def test_unknown_destructive_target_fails_closed(tmp_path: Path) -> None:
    """Destructive semantics with no resolvable target must not soften to ALLOW."""
    context = parse_command_context("kill -9 1", cwd=tmp_path, workspace_root=tmp_path)
    assert not context.target_paths
    decision = PermissionEngine().evaluate(context)
    assert decision.decision is Decision.APPROVAL_REQUIRED
    assert decision.reason.value == "APPROVAL_UNKNOWN_HIGH_IMPACT"


def test_arguments_not_only_executable_name_change_semantics(tmp_path: Path) -> None:
    """Same executable, materially different arguments, different verdicts."""
    pairs = [
        # (benign form, dangerous form, dangerous decision)
        ("mkdir -p x", "rm -rf build", Decision.APPROVAL_REQUIRED),
        ("git status", "git push --force", Decision.APPROVAL_REQUIRED),
        ("pip install x", "pip install -g x", Decision.APPROVAL_REQUIRED),
        ("systemctl --user restart app", "systemctl restart nginx", Decision.APPROVAL_REQUIRED),
    ]
    for _benign, dangerous, expected in pairs:
        _effect, decision, _ctx = _engine(dangerous, tmp_path)
        assert decision is expected, dangerous
    for benign, _dangerous, _expected in pairs:
        effect, decision, _ctx = _engine(benign, tmp_path)
        assert decision is Decision.ALLOW, benign
        assert effect is not CommandEffect.DESTRUCTIVE_MUTATION, benign


def test_harness_chmod_reproduction_is_gated() -> None:
    """The exact harmless reproduction from the closure report must be blocked."""
    assert command_requires_approval(["chmod", "600", "marker.txt"]) is True
    safe, detail = sensor_command_is_safe(_Sensor("chmod 600 marker.txt"))  # type: ignore[arg-type]
    assert safe is False
    assert "requires approval" in detail


def test_permission_engine_is_the_only_approval_authority() -> None:
    """No caller may re-derive command meaning from argv on its own."""
    root = Path("/tmp")
    context = parse_command_context("rm -rf build", cwd=root, workspace_root=root)
    # The context carries the classification; a caller reading argv alone has
    # nothing canonical to work from.
    assert context.command_effect == str(CommandEffect.DESTRUCTIVE_MUTATION)
    assert PermissionEngine().evaluate(context).decision is Decision.APPROVAL_REQUIRED


async def test_mcp_shell_exec_preserves_policy_precedence(tmp_path: Path) -> None:
    """Workspace binding must not mask a policy denial (reason-code drift P0-9)."""
    from veya.remote import (
        RemoteAudit,
        RemoteAuth,
        RemotePermissions,
        RemoteSessionManager,
        RemoteToolAdapter,
    )
    from veya.remote.mcp_server import create_gateway

    # the remote shell leg binds a nested Git repository; use a real one so the
    # safe-command control can actually execute.
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "notes.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "init"], check=True, capture_output=True
    )

    auth = RemoteAuth()
    _record, secret = auth.issue(
        "tester",
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        workspaces=[str(repo)],
    )
    audit = RemoteAudit()
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=4),
        audit=audit,
        adapter=RemoteToolAdapter(None, redact=audit.redact),
    )
    init = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = init["result"]["sessionId"]

    async def call(arguments: dict) -> dict:
        response = await gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "shell.exec", "arguments": arguments},
            },
            authorization=f"Bearer {secret}",
            session_header=session_id,
        )
        return response["result"]["structuredContent"]

    # Destructive shell without the destructive capability: policy blocks it
    # before any repository discovery can report a workspace error.
    plain = await call({"command": "rm -rf build"})
    assert plain["ok"] is False
    assert plain["error_code"] == "POLICY_BLOCKED"

    # A boolean approval flag is never a bypass.
    bypass = await call({"command": "rm -rf build", "approved": True})
    assert bypass["ok"] is False
    assert bypass["error_code"] in {"POLICY_BLOCKED", "INVALID_APPROVAL"}

    # A safe command is not blocked by the destructive policy.
    safe = await call({"command": "ls -la"})
    assert safe["ok"] is True


def test_harness_doctor_blocks_on_destructive_sensor(tmp_path: Path) -> None:
    """HARNESS_BLOCKED for a main-guarded sensor command, with no harness change."""
    from runtime.harness.doctor import run_harness_doctor

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "marker.txt").write_text("marker\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text(
        "## Commands\n"
        f"- test: chmod 600 {repo / 'marker.txt'}\n"
        "## Permissions\n- Permission and approval policy is explicit.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "init"], check=True, capture_output=True
    )
    (repo / ".veya" / "runs").mkdir(parents=True)

    report = run_harness_doctor(repo, sensor_mode="full", run_sensors=False)
    assert report.status == "HARNESS_BLOCKED"
    assert any("requires approval" in blocker for blocker in report.blockers)


def test_exactly_one_authority_module_defines_command_policy() -> None:
    """A second destructive-executable list would be a second authority."""
    source = Path(__file__).resolve().parents[2] / "veya" / "remote" / "permission_engine.py"
    text = source.read_text(encoding="utf-8")
    assert text.count("_DESTRUCTIVE_EXECUTABLES = frozenset(") == 1
    runner = (
        Path(__file__).resolve().parents[2] / "runtime" / "coding" / "command_runner.py"
    ).read_text(encoding="utf-8")
    # the runner must delegate, never own a destructive-executable policy
    assert "_DESTRUCTIVE_EXECUTABLES" not in runner
    assert "PermissionEngine" in runner

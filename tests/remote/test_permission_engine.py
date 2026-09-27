from pathlib import Path

from veya.remote.permission_engine import (
    Decision,
    OperationContext,
    PermissionEngine,
    ReasonCode,
    Scope,
    parse_command_context,
)


def test_project_read_and_mutation_are_allowed(tmp_path: Path) -> None:
    engine = PermissionEngine()
    read = engine.evaluate(
        OperationContext(
            operation="file.read",
            workspace_root=tmp_path,
            cwd=tmp_path,
            target_paths=(tmp_path / "README.md",),
            filesystem_effect="read",
        )
    )
    write = engine.evaluate(
        OperationContext(
            operation="file.write",
            workspace_root=tmp_path,
            cwd=tmp_path,
            target_paths=(tmp_path / "generated.txt",),
            filesystem_effect="write",
        )
    )
    assert (read.decision, read.reason) == (Decision.ALLOW, ReasonCode.ALLOW_READ_ONLY)
    assert (write.decision, write.reason) == (
        Decision.ALLOW,
        ReasonCode.ALLOW_PROJECT_MUTATION,
    )


def test_normal_git_is_allowed_but_force_push_requires_approval(tmp_path: Path) -> None:
    engine = PermissionEngine()
    normal = engine.evaluate(
        parse_command_context("git status", cwd=tmp_path, workspace_root=tmp_path)
    )
    force = engine.evaluate(
        parse_command_context(
            "git push --force-with-lease origin main",
            cwd=tmp_path,
            workspace_root=tmp_path,
        )
    )
    assert normal.decision == Decision.ALLOW
    assert normal.reason == ReasonCode.ALLOW_READ_ONLY
    assert force.decision == Decision.APPROVAL_REQUIRED
    assert force.reason == ReasonCode.APPROVAL_IRREVERSIBLE_REMOTE


def test_user_runtime_allowed_and_system_runtime_requires_approval() -> None:
    engine = PermissionEngine()
    user = engine.evaluate(OperationContext(operation="systemctl restart", service_effect="user"))
    system = engine.evaluate(
        OperationContext(operation="systemctl restart", service_effect="system")
    )
    assert user.decision == Decision.ALLOW
    assert user.reason == ReasonCode.ALLOW_USER_RUNTIME
    assert system.decision == Decision.APPROVAL_REQUIRED


def test_scope_escape_is_denied(tmp_path: Path) -> None:
    decision = PermissionEngine().evaluate(
        OperationContext(
            operation="file.write",
            workspace_root=tmp_path,
            cwd=tmp_path,
            target_paths=(tmp_path.parent / "outside.txt",),
            filesystem_effect="write",
        )
    )
    assert decision.scope == Scope.OUTSIDE_ALLOWED_SCOPE
    assert decision.decision == Decision.DENY
    assert decision.reason == ReasonCode.DENY_SCOPE_ESCAPE


def test_privileged_host_intent_requires_approval(tmp_path: Path) -> None:
    decision = PermissionEngine().evaluate(
        parse_command_context("sudo apt install curl", cwd=tmp_path, workspace_root=tmp_path)
    )
    assert decision.decision == Decision.APPROVAL_REQUIRED
    assert decision.reason == ReasonCode.APPROVAL_HOST_PRIVILEGE


def test_shell_wrapper_is_recursive_and_does_not_hide_host_mutation(tmp_path: Path) -> None:
    engine = PermissionEngine()
    safe = engine.evaluate(
        parse_command_context(
            "bash -lc 'pytest && ruff check .'", cwd=tmp_path, workspace_root=tmp_path
        )
    )
    gated = engine.evaluate(
        parse_command_context(
            "bash -lc 'pytest && sudo apt-get install curl'",
            cwd=tmp_path,
            workspace_root=tmp_path,
        )
    )
    assert safe.decision == Decision.ALLOW
    assert gated.decision == Decision.APPROVAL_REQUIRED


def test_read_only_shell_never_privileged(tmp_path: Path) -> None:
    engine = PermissionEngine()
    for command in (
        "pwd",
        "git rev-parse --show-toplevel",
        "git status",
        "git diff",
        "rg pattern .",
        "grep pattern file.txt",
        "find . -maxdepth 1",
        "cat README.md",
    ):
        decision = engine.evaluate(
            parse_command_context(command, cwd=tmp_path, workspace_root=tmp_path)
        )
        assert decision.decision == Decision.ALLOW, command
        assert decision.reason in {ReasonCode.ALLOW_READ_ONLY, ReasonCode.ALLOW_PROJECT_GIT}


def test_project_mutation_and_test_build_are_allowed(tmp_path: Path) -> None:
    engine = PermissionEngine()
    for operation, command, target in (
        ("file.write", None, tmp_path / "new.txt"),
        ("file.patch", None, tmp_path / "new.txt"),
        ("file.delete", None, tmp_path / "new.txt"),
        ("test.run", ("pytest", "-q"), None),
        ("build.run", ("python", "-m", "compileall", "."), None),
    ):
        decision = engine.evaluate(
            OperationContext(
                operation=operation,
                workspace_root=tmp_path,
                cwd=tmp_path,
                target_paths=(target,) if target else (),
                command=command,
                filesystem_effect="write" if target else "none",
                process_effect="execute" if command else "none",
            )
        )
        assert decision.decision == Decision.ALLOW, operation


def test_project_git_operations_and_worktree_metadata_are_allowed(tmp_path: Path) -> None:
    engine = PermissionEngine()
    for operation in (
        "add",
        "commit",
        "branch",
        "switch",
        "checkout",
        "merge",
        "rebase",
        "cherry-pick",
        "stash",
        "worktree",
    ):
        decision = engine.evaluate(
            parse_command_context(f"git {operation}", cwd=tmp_path, workspace_root=tmp_path)
        )
        assert decision.decision == Decision.ALLOW, operation


def test_symlink_escape_denied_by_workspace_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    from veya.remote.models import RemotePermissions
    from veya.remote.workspace_policy import WorkspacePolicy, WorkspacePolicyError

    policy = WorkspacePolicy(workspace, RemotePermissions(read=True, write=True))
    try:
        policy.resolve("link/secret.txt", must_exist=True)
    except WorkspacePolicyError as exc:
        assert exc.code == "WORKSPACE_DENIED"
    else:  # pragma: no cover - assertion makes the security negative explicit
        raise AssertionError("symlink escape was accepted")


def test_host_mutations_require_approval(tmp_path: Path) -> None:
    engine = PermissionEngine()
    for command in ("sudo apt install curl", "systemctl restart ssh.service"):
        decision = engine.evaluate(
            parse_command_context(command, cwd=tmp_path, workspace_root=tmp_path)
        )
        assert decision.decision == Decision.APPROVAL_REQUIRED, command

    etc = engine.evaluate(
        OperationContext(
            operation="file.write",
            workspace_root=tmp_path,
            cwd=tmp_path,
            target_paths=(Path("/etc/veya.conf"),),
            filesystem_effect="write",
        )
    )
    assert etc.decision == Decision.APPROVAL_REQUIRED

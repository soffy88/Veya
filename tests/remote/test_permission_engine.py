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

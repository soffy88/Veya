"""Security-core tests for the remote MCP interface (auth / policy / session / audit)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from veya.remote import (
    EffectClass,
    RemoteAudit,
    RemoteAuth,
    RemoteAuthError,
    RemotePermissions,
    RemoteSessionError,
    RemoteSessionManager,
    WorkspacePolicy,
    WorkspacePolicyError,
)
from veya.remote.auth import RemoteToken, _digest
from veya.remote.workspace_policy import classify_destructive


# ── authentication ─────────────────────────────────────────────────────
def test_no_tokens_configured_fails_closed() -> None:
    auth = RemoteAuth()
    assert auth.configured is False
    with pytest.raises(RemoteAuthError):
        auth.verify("Bearer anything")


def test_valid_token_verifies_and_invalid_is_denied() -> None:
    auth = RemoteAuth()
    _, secret = auth.issue("alice", workspaces=["/tmp"])
    token = auth.verify(f"Bearer {secret}")
    assert token.principal == "alice"
    with pytest.raises(RemoteAuthError):
        auth.verify("Bearer not-the-secret")
    with pytest.raises(RemoteAuthError):
        auth.verify(secret)  # missing "Bearer "
    with pytest.raises(RemoteAuthError):
        auth.verify(None)


def test_token_revocation_and_rotation() -> None:
    auth = RemoteAuth()
    record, secret = auth.issue("bob", workspaces=["/tmp"])
    auth.verify(f"Bearer {secret}")
    assert auth.revoke(record.token_id) is True
    with pytest.raises(RemoteAuthError):
        auth.verify(f"Bearer {secret}")
    new_secret = auth.rotate(record.token_id)
    assert new_secret and new_secret != secret
    assert auth.verify(f"Bearer {new_secret}").token_id == record.token_id
    with pytest.raises(RemoteAuthError):
        auth.verify(f"Bearer {secret}")
    assert auth.revoke("missing") is False


def test_raw_secret_never_persisted(tmp_path: Path) -> None:
    store = tmp_path / "tokens.json"
    auth = RemoteAuth(store_path=store)
    _, secret = auth.issue("carol", workspaces=["/tmp"])
    raw = store.read_text(encoding="utf-8")
    assert secret not in raw
    data = json.loads(raw)
    assert data[0]["secret_sha256"] == _digest(secret)


def test_from_env_and_expiry(tmp_path: Path) -> None:
    record = RemoteToken(
        token_id="t1",
        principal="dave",
        secret_sha256=_digest("s3cret"),
        workspaces=("/tmp",),
        expires_at=time.time() - 1,
    )
    auth = RemoteAuth.from_env({"VEYA_REMOTE_TOKENS": json.dumps([record.to_dict()])})
    assert auth.token_count == 1
    with pytest.raises(RemoteAuthError):
        auth.verify("Bearer s3cret")


# ── workspace policy ───────────────────────────────────────────────────
def test_path_traversal_is_blocked(tmp_path: Path) -> None:
    policy = WorkspacePolicy(tmp_path, RemotePermissions())
    with pytest.raises(WorkspacePolicyError) as exc:
        policy.resolve("../outside.txt", must_exist=None)
    assert exc.value.code == "WORKSPACE_DENIED"


def test_absolute_path_escape_is_blocked(tmp_path: Path) -> None:
    policy = WorkspacePolicy(tmp_path, RemotePermissions())
    with pytest.raises(WorkspacePolicyError):
        policy.resolve("/etc/passwd", must_exist=False)


def test_symlink_escape_is_blocked(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "link"
    link.symlink_to(outside)
    policy = WorkspacePolicy(workspace, RemotePermissions())
    with pytest.raises(WorkspacePolicyError) as exc:
        policy.resolve("link/secret.txt", must_exist=True)
    assert exc.value.code == "WORKSPACE_DENIED"


def test_sensitive_subpath_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    (workspace / "secrets").mkdir(parents=True)
    (workspace / "secrets" / "id_rsa").write_text("key", encoding="utf-8")
    import veya.remote.workspace_policy as wp

    monkeypatch.setattr(wp, "sensitive_subpaths", lambda: (workspace / "secrets",))
    policy = WorkspacePolicy(workspace, RemotePermissions())
    with pytest.raises(WorkspacePolicyError) as exc:
        policy.resolve("secrets/id_rsa", must_exist=True)
    assert exc.value.code == "WORKSPACE_DENIED"


def test_forbidden_root_cannot_be_bound() -> None:
    with pytest.raises(WorkspacePolicyError):
        WorkspacePolicy(Path("/"), RemotePermissions())


def test_git_internals_need_destructive(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".git").mkdir(parents=True)
    read_only = WorkspacePolicy(workspace, RemotePermissions(write=True))
    with pytest.raises(WorkspacePolicyError) as exc:
        read_only.resolve(".git/config", must_exist=None, for_write=True)
    assert exc.value.code == "POLICY_BLOCKED"
    destructive = WorkspacePolicy(workspace, RemotePermissions(write=True, destructive=True))
    assert destructive.resolve(".git/config", must_exist=None, for_write=True)


def test_permission_enforcement(tmp_path: Path) -> None:
    policy = WorkspacePolicy(tmp_path, RemotePermissions())
    policy.require(EffectClass.READ)
    with pytest.raises(WorkspacePolicyError):
        policy.require(EffectClass.WRITE)
    with pytest.raises(WorkspacePolicyError):
        policy.require(EffectClass.READ, needs_shell=True)
    with pytest.raises(WorkspacePolicyError):
        policy.require(EffectClass.READ, needs_git=True)
    shell_only = WorkspacePolicy(tmp_path, RemotePermissions(read=True, write=True, shell=True))
    shell_only.require(EffectClass.WRITE, needs_shell=True)
    with pytest.raises(WorkspacePolicyError):
        shell_only.require(EffectClass.DESTRUCTIVE)


def test_destructive_classification(tmp_path: Path) -> None:
    assert classify_destructive("ls -la") is None
    assert classify_destructive("rm -rf build") == "rm"
    assert classify_destructive("git reset --hard HEAD~1") == "git-reset-hard"
    assert classify_destructive("git clean -fd") == "git-clean"
    assert classify_destructive("pip install requests") == "pip-install"
    policy = WorkspacePolicy(tmp_path, RemotePermissions(write=True, shell=True))
    with pytest.raises(WorkspacePolicyError) as exc:
        policy.require_not_destructive("rm -rf /tmp/x")
    assert exc.value.code == "POLICY_BLOCKED"


# ── session manager ────────────────────────────────────────────────────
def _token(workspaces: tuple[str, ...] = ("/tmp",), token_id: str = "tok") -> RemoteToken:
    return RemoteToken(
        token_id=token_id,
        principal="p",
        secret_sha256=_digest("s"),
        permissions=RemotePermissions(read=True, write=True),
        workspaces=workspaces,
    )


def test_session_workspace_binding_and_switch(tmp_path: Path) -> None:
    ws1 = tmp_path / "a"
    ws2 = tmp_path / "b"
    ws1.mkdir()
    ws2.mkdir()
    manager = RemoteSessionManager()
    token = _token((str(ws1),))
    session = manager.create(token)
    assert session.active_workspace == str(ws1.resolve())
    with pytest.raises(RemoteSessionError):
        manager.bind_workspace(session, str(ws2))
    token2 = _token((str(ws1), str(ws2)), token_id="tok2")
    session2 = manager.create(token2)
    assert manager.bind_workspace(session2, str(ws2)) == str(ws2.resolve())


def test_session_fails_closed_without_workspaces() -> None:
    manager = RemoteSessionManager()
    with pytest.raises(RemoteSessionError):
        manager.create(_token(()))


def test_session_ttl_and_reconnect(tmp_path: Path) -> None:
    now = [1000.0]
    manager = RemoteSessionManager(ttl_s=60, clock=lambda: now[0])
    session = manager.create(_token((str(tmp_path),)))
    assert manager.reconnect(session.session_id).session_id == session.session_id
    now[0] += 61
    with pytest.raises(RemoteSessionError):
        manager.require(session.session_id)


def test_concurrent_session_limit(tmp_path: Path) -> None:
    manager = RemoteSessionManager(max_sessions=1)
    token_a = _token((str(tmp_path),))
    token_b = RemoteToken(
        token_id="tok_b",
        principal="q",
        secret_sha256=_digest("s2"),
        permissions=RemotePermissions(read=True),
        workspaces=(str(tmp_path),),
    )
    manager.create(token_a)
    with pytest.raises(RemoteSessionError) as exc:
        manager.create(token_b)
    assert exc.value.code == "LIMIT_EXCEEDED"


def test_same_token_requires_explicit_session_id_to_reconnect(tmp_path: Path) -> None:
    """A credential identifies a principal, not a conversation.

    Re-initializing without the explicit MCP session id must not silently
    share another conversation's session/worktree; reconnect by id does, and
    refreshes the TTL so a long-lived client is not churned.
    """

    now = [1000.0]
    manager = RemoteSessionManager(ttl_s=60, clock=lambda: now[0])
    token = _token((str(tmp_path),))
    first = manager.create(token)
    first.worktrees[str(tmp_path)] = "/tmp/wt"
    now[0] += 30
    second = manager.create(token)
    assert second.session_id != first.session_id

    resumed = manager.reconnect(first.session_id)
    assert resumed.session_id == first.session_id
    assert resumed.worktrees[str(tmp_path)] == "/tmp/wt"
    assert resumed.expires_at > now[0]  # TTL refreshed on explicit reconnect


# ── audit ──────────────────────────────────────────────────────────────
def test_audit_does_not_redact_identifiers() -> None:
    audit = RemoteAudit()
    text = (
        "path=/data/soffy/projects/veya/.veya/worktrees/task-remote-cabc1bd7f4310f305764 "
        "branch=veya/remote-mcp-session-remote-cabc1 "
        "sha=0123456789abcdef0123456789abcdef01234567"
    )
    redacted = audit.redact(text)
    assert "task-remote-cabc1bd7f4310f305764" in redacted
    assert "0123456789abcdef0123456789abcdef01234567" in redacted


def test_audit_still_redacts_credential_shapes() -> None:
    audit = RemoteAudit()
    for secret in (
        "sk-abcdefghijklmnopqrstuvwxyz012345",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAIOSFODNN7EXAMPLE",
    ):
        assert secret not in audit.redact(f"value={secret}")


def test_audit_redacts_secrets_and_appends(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    audit = RemoteAudit(log, secret_values=("super-secret-value",))
    audit.record(
        tool="file.write",
        status="completed",
        args={
            "path": "a.py",
            "content": "token=super-secret-value",
            "api_key": "abcd",
            "nested": {"password": "hunter2", "safe": "ok"},
        },
    )
    record = audit.records()[0]
    assert record["args"]["api_key"] == "***REDACTED***"
    assert record["args"]["nested"]["password"] == "***REDACTED***"
    assert record["args"]["nested"]["safe"] == "ok"
    assert "super-secret-value" not in json.dumps(record)
    assert "hunter2" not in json.dumps(record)
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["tool"] == "file.write"

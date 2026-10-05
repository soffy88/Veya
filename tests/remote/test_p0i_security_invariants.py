"""P0-I: security invariants — permission, effect, sandbox, refusal truthfulness.

These tests try to break containment rather than confirm it works. Each escape is
attempted through the real gateway, and the assertion is that no process ran and
no handle was issued — a refusal that still produces an execution_id has not
refused anything.

Nothing here relaxes an existing rule to make a test pass. In particular
shell.exec grading an inline interpreter as P2_ROOT_MUTATION is treated as the
security baseline it is and asserted as such.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
)
from veya.remote.effect_registry import (
    EFFECT_LATTICE,
    Effect,
    EffectProvenance,
    EffectRegistry,
    get_effect_registry,
    resolve_tool_effect,
)
from veya.remote.execution import ExecutionStore
from veya.remote.mcp_server import create_gateway
from veya.remote.permission_engine import (
    Decision,
    OperationContext,
    PermissionEngine,
    ReasonCode,
    Scope,
)
from veya.remote.tool_adapter import BINDINGS

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
PY = sys.executable


class Executor:
    """Records every tool it is asked to run so a refusal can be shown to run nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        self.calls.append(name)
        if name == "write_file":
            from server.tool_registry import _tool_write_file

            result = _tool_write_file(
                str(kwargs.get("filepath")),
                str(kwargs.get("content", "")),
                bool(kwargs.get("overwrite", True)),
            )
            return json.dumps({"status": "ok", "data": {"result": str(result)}})
        return json.dumps({"status": "ok", "data": {"stdout": "unused"}})


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


def make_gateway(workspaces: list[Path]):
    auth = RemoteAuth()
    _record, secret = auth.issue(
        "tester", permissions=PERMS, workspaces=[str(w) for w in workspaces]
    )
    audit = RemoteAudit(None)
    adapter = RemoteToolAdapter(
        Executor(), redact=audit.redact, execution_store=ExecutionStore(None)
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=8),
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret


async def open_session(gateway, secret, workspace: str) -> str:
    response = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"workspace": workspace}},
        authorization=f"Bearer {secret}",
    )
    return response["result"]["sessionId"]


async def call(gateway, secret, session, name, arguments):
    response = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        authorization=f"Bearer {secret}",
        session_header=session,
    )
    return response["result"]["structuredContent"]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_git_repo(tmp_path / "proj")


# ── §9.2 the effect vocabulary and its fail-closed member ─────────────
def test_every_required_effect_exists() -> None:
    required = {
        "READ",
        "WRITE",
        "SHELL",
        "NETWORK",
        "PROCESS",
        "SYSTEM",
        "DESTRUCTIVE",
        "UNKNOWN",
    }
    assert required <= {member.value for member in Effect}


def test_unknown_is_outside_the_safety_order() -> None:
    """UNKNOWN must be unorderable, or "least effect wins" could normalise it.

    NETWORK/PROCESS/DESTRUCTIVE are deliberately outside the sequential lattice:
    they are set membership, not ranks. UNKNOWN is outside it for a different and
    stronger reason — it is not a level of access, it is an absence of knowledge,
    and ranking it would let it be treated as the bottom of the ladder.
    """
    assert Effect.UNKNOWN not in EFFECT_LATTICE
    assert Effect.READ in EFFECT_LATTICE
    assert EFFECT_LATTICE.index(Effect.SYSTEM) > EFFECT_LATTICE.index(Effect.WRITE)


def test_an_undeclared_tool_stays_unknown_and_is_never_a_read() -> None:
    """§9.2: UNKNOWN fails closed. It must not fall back to READ."""
    resolution = resolve_tool_effect("veya.definitely.not.declared")

    assert resolution.effect is Effect.UNKNOWN
    assert resolution.effect is not Effect.READ
    assert resolution.provenance is EffectProvenance.UNKNOWN
    assert resolution.confidence < 1.0


async def test_an_undeclared_tool_is_refused_before_it_can_be_classified(
    repo: Path,
) -> None:
    """§9.2: UNKNOWN fails closed at the boundary that can act on it.

    The effect registry answers UNKNOWN for a tool nobody declared, and an
    undeclared name is not in the binding table either, so the call is refused at
    the tool boundary with no execution. The fail-closed guarantee is the
    conjunction of those two facts, not a fallback inside the permission engine:
    the engine is handed an already-resolved effect and is not re-deriving one.
    """
    assert resolve_tool_effect("veya.undeclared.tool").effect is Effect.UNKNOWN
    assert "veya.undeclared.tool" not in {binding.name for binding in BINDINGS}

    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))
    envelope = await call(
        gateway, secret, session, "veya.undeclared.tool", {"workspace": str(repo)}
    )

    assert envelope["ok"] is False, envelope
    assert envelope["error_code"] == "TOOL_DENIED", envelope
    assert envelope.get("execution_id") is None, envelope


# ── §9.3 the four authorities do not substitute for each other ────────
def test_effect_permission_executor_and_sandbox_answer_different_questions() -> None:
    """§9.3: no authority can be substituted for another, because none holds
    another's verdict.

    Checked against the modules' own namespaces rather than their prose: the
    effect registry must expose no authorisation verdict at all, and the
    permission engine must not construct an effect registry of its own, or
    effect declaration would stop being an independent input to the decision.
    """
    import importlib

    effect = importlib.import_module("veya.remote.effect_registry")
    permission = importlib.import_module("veya.remote.permission_engine")
    executor = importlib.import_module("veya.remote.executor_registry")
    sandbox = importlib.import_module("runtime.coding.sandbox_profiles")

    # Capability declaration holds no authorisation vocabulary.
    assert not hasattr(effect, "Decision")
    assert not hasattr(effect, "PermissionDecision")

    # Authorisation does not resolve effects for itself.
    assert not hasattr(permission, "resolve_tool_effect")
    assert not hasattr(permission, "get_effect_registry")

    # Executor identity and containment are separate capabilities again.
    assert not hasattr(executor, "Decision")
    assert not hasattr(sandbox, "Decision")


# ── §9.4 credential semantics ─────────────────────────────────────────
def test_credential_fields_are_present_and_truthful() -> None:
    """SF-CRED cutover: credential_valid, not candidate.authenticated."""
    from veya.remote.executor_registry import ExecutorRuntimeState

    fields = set(ExecutorRuntimeState.__dataclass_fields__)
    assert {"credential_present", "credential_valid", "credential_evidence"} <= fields
    # ``authenticated`` still exists on the record but is explicitly not the
    # selection or admission authority; the comment in the source says so.
    assert "credential_valid" in fields and "candidate" not in fields
    state = ExecutorRuntimeState()
    assert state.credential_valid is None
    assert state.credential_evidence == "UNPROBED"


def test_candidate_authenticated_is_not_used_as_authority() -> None:
    """The pre-cutover selection key must not have crept back in."""
    proc = subprocess.run(
        ["git", "grep", "-n", "candidate", "--", "veya/"],
        capture_output=True,
        text=True,
        check=False,
    )
    offenders = [
        line for line in proc.stdout.splitlines() if "authenticated" in line and "candidate" in line
    ]
    assert offenders == [], offenders


def test_an_unproven_credential_makes_no_claim() -> None:
    """credential_valid is tri-state: unknown must not read as valid or invalid."""
    from veya.remote.executor_registry import ExecutorRuntimeState

    unknown = ExecutorRuntimeState(credential_present=True, credential_valid=None)
    assert unknown.credential_valid is None
    assert unknown.credential_evidence == "UNPROBED"


# ── §9.5 sandbox boundary ─────────────────────────────────────────────
def _policy(repo: Path, **extra) -> Any:
    from veya.remote.workspace_policy import WorkspacePolicy

    return WorkspacePolicy(repo, PERMS, **extra)


def test_a_target_inside_the_workspace_resolves(repo: Path) -> None:
    resolved = _policy(repo).resolve("README.md", must_exist=True)
    assert resolved == (repo / "README.md").resolve()


@pytest.mark.parametrize(
    "label,target",
    [
        ("parent_traversal", "../../etc/passwd"),
        ("absolute_etc", "/etc/passwd"),
        ("absolute_proc", "/proc/self/environ"),
        ("sibling_directory", "../outside/secret.txt"),
    ],
)
def test_out_of_scope_paths_are_refused_by_the_containment_authority(
    tmp_path: Path, label: str, target: str
) -> None:
    """Containment is WorkspacePolicy's question, and it answers it before
    anything is classified.

    The permission engine is not the right place to assert this: it authorises a
    resolved operation, and a HOST-scope read is legitimately ALLOW there. What
    must never happen is a path outside the bound workspace being resolved at
    all, so that is what is asserted.
    """
    repo = make_git_repo(tmp_path / "proj")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified\n", encoding="utf-8")

    with pytest.raises(Exception) as caught:
        _policy(repo).resolve(target, must_exist=False)

    assert getattr(caught.value, "code", "") in {
        "WORKSPACE_DENIED",
        "NOT_FOUND",
        "INVALID_ARGUMENT",
    }, (label, caught.value)


def test_a_symlink_pointing_outside_is_refused(tmp_path: Path) -> None:
    """Containment is checked after symlink resolution, not before.

    A link that lives inside the workspace and points outside is the escape a
    lexical prefix check waves through: the path *is* under the root, and the
    content is not. ``resolve`` resolves first, so the resolved path is what gets
    tested.
    """
    repo = make_git_repo(tmp_path / "proj")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified\n", encoding="utf-8")
    link = repo / "innocent.txt"
    link.symlink_to(outside / "secret.txt")

    # Sanity: the link really does escape once resolved.
    assert link.resolve().is_relative_to(outside)

    with pytest.raises(Exception) as caught:
        _policy(repo).resolve("innocent.txt", must_exist=True)

    assert getattr(caught.value, "code", "") == "WORKSPACE_DENIED", caught.value


def test_sensitive_subtrees_are_refused_even_when_readable(repo: Path) -> None:
    """/etc, /proc, ~/.ssh and friends are never accessible remotely."""
    from veya.remote.workspace_policy import sensitive_subpaths

    assert sensitive_subpaths(), "the sensitive list must not be empty"

    policy = _policy(repo, extra_roots=(Path("/etc"),))
    with pytest.raises(Exception) as caught:
        policy.resolve("/etc/hostname", must_exist=False)

    assert getattr(caught.value, "code", "") == "WORKSPACE_DENIED", caught.value


def test_the_permission_engine_authorises_a_host_scope_read(tmp_path: Path) -> None:
    """Documents the split the tests above rely on.

    A HOST-scope read-only operation is ALLOW at the permission layer by design;
    containment is WorkspacePolicy's job and happens earlier. Pinning this keeps
    the layering honest: if someone later makes the engine deny host reads, this
    test says whether that was intended.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    decision = PermissionEngine().evaluate(
        OperationContext(
            tool="file.read",
            operation="file.read",
            workspace_root=workspace,
            cwd=workspace,
            target_paths=(Path("/etc/hostname"),),
            filesystem_effect="read",
        )
    )
    assert decision.decision is Decision.ALLOW
    assert decision.scope is Scope.HOST


def test_the_permission_engine_still_denies_an_out_of_scope_relative_target(
    repo: Path,
) -> None:
    """The escape verdict is the engine's too, for the relative form it sees."""
    decision = PermissionEngine().evaluate(
        OperationContext(
            tool="file.read",
            operation="file.read",
            workspace_root=repo,
            cwd=repo,
            target_paths=((repo / "../../etc/passwd").resolve(),),
            filesystem_effect="read",
        )
    )
    assert decision.decision is Decision.DENY
    assert decision.reason in {
        ReasonCode.DENY_SCOPE_ESCAPE,
        ReasonCode.DENY_PATH_TRAVERSAL,
    }


async def test_escape_attempts_issue_no_execution_and_run_nothing(tmp_path: Path) -> None:
    """End to end: the refusal must hold at the transport, not only in a unit."""
    repo = make_git_repo(tmp_path / "proj")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified\n", encoding="utf-8")
    (repo / "link.txt").symlink_to(outside / "secret.txt")

    executor = Executor()
    auth = RemoteAuth()
    _record, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(repo)])
    audit = RemoteAudit(None)
    adapter = RemoteToolAdapter(executor, redact=audit.redact, execution_store=ExecutionStore(None))
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=60, max_sessions=4),
        audit=audit,
        adapter=adapter,
    )
    session = await open_session(gateway, secret, str(repo))

    attempts = [
        "link.txt",
        "../../etc/passwd",
        "/etc/passwd",
        "/proc/self/environ",
        str(outside / "secret.txt"),
    ]
    for path in attempts:
        envelope = await call(
            gateway, secret, session, "file.read", {"path": path, "workspace": str(repo)}
        )
        assert envelope["ok"] is False, (path, envelope)
        assert envelope.get("execution_id") is None, (path, envelope)

    # No mutation tool was reached either.
    assert executor.calls == [], executor.calls


# ── §9.6 approval is not denial ──────────────────────────────────────
def test_require_approval_and_deny_are_distinct_verdicts() -> None:
    assert Decision.APPROVAL_REQUIRED is not Decision.DENY
    assert {d.value for d in Decision} == {"ALLOW", "APPROVAL_REQUIRED", "DENY"}


async def test_an_unmodellable_trailing_command_fails_safe(repo: Path) -> None:
    """§9.6, and the rule P0-H leaned on: what the parser cannot model is graded
    at the top class rather than read as benign.

    ``python -c "import sys; sys.exit(0)"`` alone is permitted — an inline
    interpreter is not privileged. The ``;`` is what matters: the parser splits
    it and the trailing segment is not a command it can model, so the whole
    string is escalated and approval is requested instead of the string being
    read as a harmless first command. Relaxing this to give a test a convenient
    carrier would trade a security property for ergonomics, so it is pinned as it
    stands rather than worked around.
    """
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))

    envelope = await call(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": f'{PY} -c "import sys; sys.exit(0)"', "workspace": str(repo), "wait": True},
    )

    assert envelope["ok"] is False, envelope
    assert envelope["error_code"] == "APPROVAL_REQUIRED", envelope
    assert envelope.get("execution_id") is None, envelope
    assert "P2_ROOT_MUTATION" in envelope["message"], envelope


async def test_a_benign_shell_command_is_not_escalated(repo: Path) -> None:
    """The fail-safe above must not be bought by refusing ordinary commands."""
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))

    envelope = await call(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": f'{PY} -c "print(1)"', "workspace": str(repo), "wait": True},
    )

    assert envelope["ok"] is True, envelope
    assert envelope["execution_id"], envelope


def test_trusted_admin_never_downgrades_the_security_reasons() -> None:
    """An operator's blanket trust must not soften the boundary reasons."""
    from veya.remote.permission_engine import _TRUSTED_ADMIN_NEVER_DOWNGRADES

    assert ReasonCode.DENY_SCOPE_ESCAPE in _TRUSTED_ADMIN_NEVER_DOWNGRADES
    assert ReasonCode.DENY_PATH_TRAVERSAL in _TRUSTED_ADMIN_NEVER_DOWNGRADES
    assert ReasonCode.APPROVAL_HOST_DESTRUCTIVE in _TRUSTED_ADMIN_NEVER_DOWNGRADES


def test_a_compound_effect_is_never_folded_downward() -> None:
    """§9.2: declaring several effects resolves to all of them, not the first.

    There is no narrowing helper to call, and that is the point: the property is
    structural. A compound declaration comes back as the full set, so a caller
    cannot read a WRITE off a WRITE+SHELL tool and proceed.
    """
    registry = EffectRegistry()
    registry.declare("compound", {"effect": Effect.WRITE, "effects": [Effect.SHELL]})

    record = registry.resolve("compound")

    assert set(record.effects) == {Effect.WRITE, Effect.SHELL}
    assert record.effect is not Effect.WRITE or Effect.SHELL in record.effects


def test_parallel_effects_stay_unfolded() -> None:
    """NETWORK/PROCESS/DESTRUCTIVE are membership, not a rank to be collapsed."""
    registry = EffectRegistry()
    registry.declare("remote_only", Effect.NETWORK)
    registry.declare("multi", {"effect": Effect.WRITE, "effects": [Effect.NETWORK, Effect.PROCESS]})

    assert registry.resolve("remote_only").effect is Effect.NETWORK
    assert set(registry.resolve("multi").effects) == {
        Effect.WRITE,
        Effect.NETWORK,
        Effect.PROCESS,
    }


def test_the_registry_is_the_authority_not_a_name_prefix() -> None:
    get_effect_registry()
    from veya.remote.effect_registry import declared_effect

    # The three write tools carry a real declaration rather than a name match.
    for tool in ("file.write", "file.patch", "artifact.write"):
        assert declared_effect(tool) is Effect.WRITE, tool
    assert declared_effect("veya.not.declared") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

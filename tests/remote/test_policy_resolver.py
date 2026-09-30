"""Layered policy resolver tests (P0_02): projection OVER PermissionEngine."""

from __future__ import annotations

import pytest

from veya.remote.action_gateway import ActionGateway
from veya.remote.approval import ApprovalStore
from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.permission_engine import Decision, PermissionEngine
from veya.remote.policy_resolver import (
    AgentPolicy,
    GoalPolicy,
    ManagedPolicy,
    PolicyDecision,
    PolicyRequest,
    PolicyResolver,
    SkillPolicy,
    ToolPolicy,
)

WORKSPACE = "/tmp/opencode/agent-b-policy"


def _session(**overrides) -> RemoteSession:
    import time

    perms = dict(
        read=True,
        write=True,
        shell=True,
        git=True,
        network=True,
        destructive=False,
        service_control=False,
    )
    perms.update(overrides.pop("permissions", {}))
    return RemoteSession(
        session_id="sess-test",
        principal="tester",
        token_id="tok-test",
        workspaces=(WORKSPACE,),
        active_workspace=WORKSPACE,
        permissions=RemotePermissions(**perms),
        created_at=time.time(),
        expires_at=time.time() + 3600,
        **overrides,
    )


def _resolver(**kwargs) -> PolicyResolver:
    kwargs.setdefault("approval_store", ApprovalStore())
    return PolicyResolver(**kwargs)


def _read_request(**kwargs) -> PolicyRequest:
    base = dict(
        actor="tester",
        tool="file.read",
        args={"path": "veya/remote/models.py"},
        workspace=WORKSPACE,
        cwd=WORKSPACE,
    )
    base.update(kwargs)
    return PolicyRequest(**base)


def test_allow_read_resolves_allow() -> None:
    decision = _resolver().resolve(_read_request())
    assert decision.decision is Decision.ALLOW
    assert decision.approval_required is False
    assert "permission-engine" in decision.matched_layers
    assert decision.engine_decision == "ALLOW"


def test_approval_sudo_requires_server_approval() -> None:
    resolver = _resolver()
    request = PolicyRequest(
        actor="tester",
        tool="shell.exec",
        args={"command": "sudo true"},
        workspace=WORKSPACE,
        cwd=WORKSPACE,
    )
    decision = resolver.resolve(request)
    assert decision.decision is Decision.APPROVAL_REQUIRED
    assert decision.approval_required is True
    assert decision.approval["capability_id"] == "privileged.sudo"
    assert decision.approval["normalized_operation"] == "sudo true"
    assert decision.approval["approval_id"] == ""


def test_deny_workspace_escape() -> None:
    resolver = _resolver()
    request = PolicyRequest(
        actor="tester",
        tool="file.read",
        args={"path": "/etc/passwd"},
        workspace=WORKSPACE,
        cwd=WORKSPACE,
    )
    decision = resolver.resolve(request)
    assert decision.decision is Decision.DENY
    assert decision.constraining_layer in {"workspace", "permission-engine"}


def test_higher_layer_deny_cannot_be_overridden_below() -> None:
    resolver = _resolver(managed=ManagedPolicy(denied_tools={"shell.exec"}))
    request = PolicyRequest(
        actor="tester",
        tool="shell.exec",
        args={"command": "echo hello"},
        workspace=WORKSPACE,
        cwd=WORKSPACE,
    )
    engine_only = PermissionEngine().evaluate(
        __import__(
            "veya.remote.permission_engine", fromlist=["parse_command_context"]
        ).parse_command_context(
            "echo hello",
            cwd=__import__("pathlib").Path(WORKSPACE),
            workspace_root=__import__("pathlib").Path(WORKSPACE),
        )
    )
    assert engine_only.decision is Decision.ALLOW
    decision = resolver.resolve(request)
    assert decision.decision is Decision.DENY
    assert decision.constraining_layer == "managed"


def test_capability_intersection() -> None:
    resolver = _resolver(
        skill=SkillPolicy(bindings={"s1": {"allow_caps": ["workspace.read"]}}),
        tool=ToolPolicy(grants={"file.read": {"workspace.read", "extra.cap"}}),
    )
    decision = resolver.resolve(_read_request(skill_id="s1"))
    assert decision.decision is Decision.ALLOW
    assert decision.effective_capabilities == frozenset({"workspace.read"})


def test_capability_intersected_out_is_deny() -> None:
    resolver = _resolver(
        skill=SkillPolicy(bindings={"s1": {"allow_caps": ["unrelated.cap"]}}),
    )
    decision = resolver.resolve(_read_request(skill_id="s1"))
    assert decision.decision is Decision.DENY
    assert decision.reason == "CAPABILITY_INTERSECTED_OUT"
    assert decision.constraining_layer == "skill"


def test_approval_replay_rejected_single_use() -> None:
    store = ApprovalStore()
    resolver = PolicyResolver(approval_store=store)
    request = PolicyRequest(
        actor="tester",
        tool="shell.exec",
        args={"command": "sudo true"},
        workspace=WORKSPACE,
        cwd=WORKSPACE,
    )
    record = resolver.request_approval(request)
    assert record.decision == "approved"
    first = resolver.resolve(
        PolicyRequest(
            actor="tester",
            tool="shell.exec",
            args={"command": "sudo true"},
            workspace=WORKSPACE,
            cwd=WORKSPACE,
            approval_id=record.approval_id,
        )
    )
    assert first.decision is Decision.ALLOW
    assert first.approval_id == record.approval_id
    replay = resolver.resolve(
        PolicyRequest(
            actor="tester",
            tool="shell.exec",
            args={"command": "sudo true"},
            workspace=WORKSPACE,
            cwd=WORKSPACE,
            approval_id=record.approval_id,
        )
    )
    assert replay.decision is Decision.DENY
    assert "INVALID_APPROVAL" in replay.reason


def test_forged_approved_true_rejected() -> None:
    resolver = _resolver()
    forged = PolicyRequest(
        actor="tester",
        tool="shell.exec",
        args={"command": "sudo true", "approved": True},
        workspace=WORKSPACE,
        cwd=WORKSPACE,
        client_approved=True,
    )
    decision = resolver.resolve(forged)
    assert decision.decision is Decision.DENY
    assert decision.constraining_layer == "client-approval-guard"
    # Forgery cannot launder even an otherwise-allow action.
    forged_allow = _read_request()
    forged_allow = PolicyRequest(
        actor="tester",
        tool="file.read",
        args={"path": "veya/remote/models.py", "approved": True},
        workspace=WORKSPACE,
        cwd=WORKSPACE,
        client_approved=True,
    )
    denied = resolver.resolve(forged_allow)
    assert denied.decision is Decision.DENY


def test_explain_output_shape() -> None:
    resolver = _resolver()
    explanation = resolver.explain(
        PolicyRequest(
            actor="tester",
            tool="shell.exec",
            args={"command": "sudo true"},
            workspace=WORKSPACE,
            cwd=WORKSPACE,
        )
    )
    assert explanation["decision"] == "APPROVAL_REQUIRED"
    assert explanation["matched_layer"]
    assert explanation["matched_rule"]
    assert explanation["constraining_higher_layer"] == explanation["matched_layer"]
    assert explanation["approval"]["capability_id"] == "privileged.sudo"
    assert explanation["engine_decision"] == "APPROVAL_REQUIRED"
    assert isinstance(explanation["why"], str) and explanation["why"]
    deny_explanation = resolver.explain(_read_request(client_approved=True))
    assert deny_explanation["decision"] == "DENY"
    assert deny_explanation["why"]


def test_policy_precedence_goal_deny_beats_tool_allow() -> None:
    resolver = _resolver(
        goal=GoalPolicy(bindings={"g1": {"deny_tools": ["file.write"]}}),
        agent=AgentPolicy(bindings={"a1": {}}),
    )
    request = PolicyRequest(
        actor="tester",
        tool="file.write",
        args={"path": "notes.txt"},
        workspace=WORKSPACE,
        cwd=WORKSPACE,
        goal_id="g1",
        agent_id="a1",
    )
    decision = resolver.resolve(request)
    assert decision.decision is Decision.DENY
    assert decision.constraining_layer == "goal"


def test_restart_recovery_snapshot_roundtrip_and_stale_approval() -> None:
    store = ApprovalStore()
    resolver = PolicyResolver(
        managed=ManagedPolicy(denied_tools={"su"}),
        goal=GoalPolicy(bindings={"g1": {"deny_tools": ["file.write"]}}),
        approval_store=store,
    )
    request = PolicyRequest(
        actor="tester",
        tool="shell.exec",
        args={"command": "sudo true"},
        workspace=WORKSPACE,
        cwd=WORKSPACE,
    )
    record = resolver.request_approval(request)
    restored = PolicyResolver.restore(resolver.snapshot(), approval_store=ApprovalStore())
    assert restored.snapshot() == resolver.snapshot()
    assert restored.resolve(request).decision is Decision.APPROVAL_REQUIRED
    assert (
        restored.resolve(
            PolicyRequest(
                actor="tester",
                tool="shell.exec",
                args={"command": "sudo true"},
                workspace=WORKSPACE,
                cwd=WORKSPACE,
                approval_id=record.approval_id,
            )
        ).decision
        is Decision.DENY
    )  # fail-closed: stale store, unknown approval


def test_compat_permission_engine_and_gateway_unchanged() -> None:
    engine = PermissionEngine()
    assert engine.evaluate is not None
    gateway = ActionGateway(approval_store=ApprovalStore())
    ok, _, _, classification = gateway.check_action(
        "shell.exec", {"command": "echo hello"}, _session(), WORKSPACE
    )
    assert ok is True
    assert classification.category == "AUTO_OPEN"


def test_gateway_chain_allow_approval_deny() -> None:
    store = ApprovalStore()
    resolver = PolicyResolver(approval_store=store)
    session = _session()
    ok, _, _, _, layered = resolver.check_via_gateway(_read_request(), session)
    assert ok is True and layered.decision is Decision.ALLOW
    gated = PolicyRequest(
        actor="tester",
        tool="shell.exec",
        args={"command": "sudo true"},
        workspace=WORKSPACE,
        cwd=WORKSPACE,
    )
    ok, code, _, _, _ = resolver.check_via_gateway(gated, session)
    assert ok is False and str(code) == "APPROVAL_REQUIRED"
    denied = PolicyRequest(
        actor="tester",
        tool="file.read",
        args={"path": "/etc/passwd"},
        workspace=WORKSPACE,
        cwd=WORKSPACE,
    )
    ok, code, _, _, layered = resolver.check_via_gateway(denied, session)
    assert ok is False and layered.decision is Decision.DENY


def test_request_approval_only_for_approval_required() -> None:
    resolver = _resolver()
    with pytest.raises(ValueError):
        resolver.request_approval(_read_request())


def test_approval_cannot_override_deny() -> None:
    store = ApprovalStore()
    resolver = PolicyResolver(
        managed=ManagedPolicy(denied_tools={"shell.exec"}), approval_store=store
    )
    record = store.create_approval(
        principal="tester",
        capability_id="shell.argv",
        normalized_operation="echo hello",
        cwd=WORKSPACE,
        workspace=WORKSPACE,
        risk_class=__import__(
            "veya.remote.models", fromlist=["RiskClass"]
        ).RiskClass.P2_ROOT_MUTATION,
    )
    decision = resolver.resolve(
        PolicyRequest(
            actor="tester",
            tool="shell.exec",
            args={"command": "echo hello"},
            workspace=WORKSPACE,
            cwd=WORKSPACE,
            approval_id=record.approval_id,
        )
    )
    assert decision.decision is Decision.DENY
    assert decision.constraining_layer == "managed"
    assert store.lookup(record.approval_id).used_at is None


def test_decision_type_is_policy_decision() -> None:
    decision = _resolver().resolve(_read_request())
    assert isinstance(decision, PolicyDecision)

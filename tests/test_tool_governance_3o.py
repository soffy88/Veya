"""PR-13 Tool/MCP governance and credential isolation contract tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from veya.platform import load

obase = load("obase")
oprim = load("oprim")
oskill = load("oskill")
omodul = load("omodul")
oservi = load("oservi")


def _contract(
    *,
    name: str = "read_document",
    kind: str = "native",
    server: str | None = None,
    effect: str = "read",
    credential: Any = None,
    enabled: bool = True,
) -> Any:
    return obase.ToolSpec(
        name=name,
        kind=kind,
        server=server,
        effect=effect,
        credential_ref=credential,
        enabled=enabled,
    )


def _grant(
    spec: Any, *, actor: str = "agent", revoked: bool = False, expires_at: str | None = None
) -> Any:
    return obase.Grant(
        tool=spec.identity,
        subject=actor,
        allowed_effects=frozenset({spec.effect}),
        tool_version=spec.version,
        revoked=revoked,
        expires_at=expires_at,
    )


async def _run(
    tmp_path: Path,
    spec: Any,
    grant: Any,
    *,
    policy: Any,
    arguments: dict[str, Any] | None = None,
    approval: Any = None,
    executor: Any = None,
    mcp_client: Any = None,
    credential_resolver: Any = None,
    operation_key: str = "",
    side_effect_recorder: Any = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    audits: list[dict[str, Any]] = []
    request = obase.ToolCallRequest(
        tool=spec.name,
        kind=spec.kind,
        server=spec.server,
        version=spec.version,
        arguments=arguments or {},
        actor="agent",
        source="pr13-test",
        grant=grant,
        credential_ref=spec.credential_ref,
    )

    async def writer(record: Any) -> None:
        audits.append(record.to_dict())

    result = await omodul.governed_tool_transaction(
        omodul.GovernedToolConfig(),
        omodul.GovernedToolInput(
            request=request,
            spec=spec,
            prepare_tool_execution=oskill.prepare_tool_execution,
            govern_action=omodul.govern_action,
            execute_governed_action=omodul.execute_governed_action,
            policy_evaluator=policy,
            approval_resolver=approval,
            audit_append=oprim.audit_append,
            audit_writer=writer,
            tool_call=oprim.tool_call,
            mcp_call=oprim.mcp_call,
            executor=executor,
            mcp_client=mcp_client,
            credential_resolve=oprim.credential_resolve,
            secret_read=oprim.secret_read,
            credential_resolver=credential_resolver,
            side_effect_record=oprim.side_effect_record,
            side_effect_recorder=side_effect_recorder,
            operation_key=operation_key,
            target_ref=spec.identity,
            capability="idempotency_key" if side_effect_recorder else "manual_only",
        ),
        tmp_path,
    )
    return result, audits


def test_tool_identity_and_refs_are_unified_but_secret_free() -> None:
    ref = obase.CredentialRef(id="github-prod", provider="github", version=3)
    native = _contract(name="read_document", credential=ref)
    mcp = _contract(name="search", kind="mcp", server="knowledge", credential=ref)
    assert native.identity == "native/read_document@1"
    assert mcp.identity == "mcp/knowledge/search@1"
    assert "github-prod" in json.dumps(ref.to_dict())
    assert "super-secret" not in repr(ref)
    assert "value" not in ref.to_dict()


def test_layer4_action_policy_does_not_allow_unknown_non_read_effect() -> None:
    from server.action_gateway_adapter import ActionGatewayAdapter

    adapter = ActionGatewayAdapter(policy_profile="DEVELOPMENT")
    request = obase.ActionRequest(action="unclassified_remote", effect="remote")
    decision = adapter._evaluate_policy(request)
    assert decision.verdict == "REQUIRE_APPROVAL"


def test_grant_check_fails_closed_for_missing_revoked_stale_and_wrong_identity() -> None:
    spec = _contract(name="publish", effect="remote")
    assert oprim.grant_check(
        _grant(spec),
        tool_identity=spec.identity,
        actor="agent",
        effect="remote",
        version="1",
    )
    assert not oprim.grant_check(
        None, tool_identity=spec.identity, actor="agent", effect="remote", version="1"
    )
    assert not oprim.grant_check(
        _grant(spec, revoked=True),
        tool_identity=spec.identity,
        actor="agent",
        effect="remote",
        version="1",
    )
    assert not oprim.grant_check(
        _grant(spec, expires_at="2000-01-01T00:00:00+00:00"),
        tool_identity=spec.identity,
        actor="agent",
        effect="remote",
        version="1",
    )
    assert not oprim.grant_check(
        _grant(spec),
        tool_identity="native/other@1",
        actor="agent",
        effect="remote",
        version="1",
    )


@pytest.mark.asyncio
async def test_native_allow_and_raw_credential_argument_is_denied(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    spec = _contract()

    def policy(request: Any) -> Any:
        return obase.ActionDecision(verdict="ALLOW", request_id=request.request_id)

    async def executor(request: Any) -> dict[str, Any]:
        calls.append(dict(request.arguments))
        return {"ok": True}

    allowed, _ = await _run(
        tmp_path,
        spec,
        _grant(spec),
        policy=policy,
        executor=executor,
        arguments={"path": "README.md"},
    )
    assert allowed["status"] == "completed"
    assert allowed["executed"] is True

    denied, audits = await _run(
        tmp_path,
        spec,
        _grant(spec),
        policy=policy,
        executor=executor,
        arguments={"api_token": "raw-token"},
    )
    assert denied["status"] == "failed"
    assert denied["executed"] is False
    assert calls == [{"path": "README.md"}]
    assert "raw-token" not in json.dumps(audits)


@pytest.mark.asyncio
async def test_unapproved_remote_action_has_zero_side_effects(tmp_path: Path) -> None:
    calls: list[bool] = []
    spec = _contract(name="publish", effect="remote")

    def require_approval(request: Any) -> Any:
        return obase.ActionDecision(
            verdict="REQUIRE_APPROVAL", reason="remote effect", request_id=request.request_id
        )

    result, _ = await _run(
        tmp_path,
        spec,
        _grant(spec),
        policy=require_approval,
        executor=lambda _request: calls.append(True),
        operation_key="publish-1",
    )
    assert result["decision"]["verdict"] == "REQUIRE_APPROVAL"
    assert result["executed"] is False
    assert calls == []


@pytest.mark.asyncio
async def test_approved_remote_action_uses_one_injected_ledger_boundary(tmp_path: Path) -> None:
    calls: list[str] = []
    records: dict[str, Any] = {}
    spec = _contract(name="publish", effect="remote")

    def require_approval(request: Any) -> Any:
        return obase.ActionDecision(
            verdict="REQUIRE_APPROVAL", reason="remote effect", request_id=request.request_id
        )

    async def approve(_request: Any) -> bool:
        return True

    async def recorder(**kwargs: Any) -> Any:
        key = str(kwargs["operation_key"])
        if key not in records:
            records[key] = await kwargs["provider"]()
        return records[key]

    async def executor(_request: Any) -> str:
        calls.append("physical")
        return "published"

    first, _ = await _run(
        tmp_path,
        spec,
        _grant(spec),
        policy=require_approval,
        approval=approve,
        executor=executor,
        operation_key="publish-once",
        side_effect_recorder=recorder,
    )
    second, _ = await _run(
        tmp_path,
        spec,
        _grant(spec),
        policy=require_approval,
        approval=approve,
        executor=executor,
        operation_key="publish-once",
        side_effect_recorder=recorder,
    )
    assert first["status"] == second["status"] == "completed"
    assert calls == ["physical"]
    assert len(records) == 1


@pytest.mark.asyncio
async def test_policy_exception_is_deny_and_never_reaches_executor(tmp_path: Path) -> None:
    calls: list[bool] = []
    spec = _contract(name="write_document", effect="local_write")

    def broken(_request: Any) -> Any:
        raise RuntimeError("policy should not expose this text")

    result, audits = await _run(
        tmp_path,
        spec,
        _grant(spec),
        policy=broken,
        executor=lambda _request: calls.append(True),
    )
    assert result["decision"]["verdict"] == "DENY"
    assert calls == []
    assert "policy should not expose" not in json.dumps(result)
    assert "policy should not expose" not in json.dumps(audits)


@pytest.mark.asyncio
async def test_credential_is_late_bound_and_redacted_from_result_and_audit(tmp_path: Path) -> None:
    secret = "ghs_super_secret_123"
    ref = obase.CredentialRef(id="github-prod")
    spec = _contract(name="read_private", credential=ref)
    resolved: list[str] = []
    received: list[str | None] = []

    def resolver(value: Any) -> str:
        resolved.append(value.id)
        return secret

    async def executor(_request: Any, _injected_secret: str | None = None) -> dict[str, str | None]:
        received.append(_injected_secret)
        return {"echo": _injected_secret}

    result, audits = await _run(
        tmp_path,
        spec,
        _grant(spec),
        policy=lambda request: obase.ActionDecision(verdict="ALLOW", request_id=request.request_id),
        executor=executor,
        credential_resolver=resolver,
    )
    serialized = json.dumps({"result": result, "audits": audits}, ensure_ascii=False)
    assert result["status"] == "completed"
    assert resolved == ["github-prod"]
    assert received == [secret]
    assert secret not in serialized
    assert "github-prod" in serialized


@pytest.mark.asyncio
async def test_secret_ref_uses_the_separate_late_bound_secret_atomic() -> None:
    secret = "secret-ref-value"
    ref = obase.SecretRef(id="vault-entry")

    class Reader:
        def get_secret(self, identifier: str) -> str:
            assert identifier == "vault-entry"
            return secret

    assert await oprim.secret_read(ref, reader=Reader()) == secret

    with pytest.raises(oprim.SecretReadError, match="missing or empty"):
        await oprim.secret_read(ref, reader=lambda _ref: None)


@pytest.mark.asyncio
async def test_missing_credential_fails_closed_before_physical_call(tmp_path: Path) -> None:
    calls: list[bool] = []
    spec = _contract(name="read_private", credential=obase.CredentialRef(id="missing"))
    result, _ = await _run(
        tmp_path,
        spec,
        _grant(spec),
        policy=lambda request: obase.ActionDecision(verdict="ALLOW", request_id=request.request_id),
        executor=lambda _request: calls.append(True),
        credential_resolver=lambda _ref: None,
    )
    assert result["status"] == "failed"
    assert result["executed"] is False
    assert calls == []


class _McpFixture:
    def __init__(self, *, failure: bool = False) -> None:
        self.failure = failure
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if self.failure:
            raise RuntimeError("remote echoed mcp-secret")
        return {"ok": True, "tool": name}


@pytest.mark.asyncio
async def test_mcp_allow_and_transport_failure_are_governed_and_safe(tmp_path: Path) -> None:
    spec = _contract(name="search", kind="mcp", server="knowledge")
    client = _McpFixture()

    def policy(request: Any) -> Any:
        return obase.ActionDecision(verdict="ALLOW", request_id=request.request_id)

    allowed, _ = await _run(
        tmp_path,
        spec,
        _grant(spec),
        policy=policy,
        mcp_client=client,
        arguments={"query": "state authority"},
    )
    assert allowed["status"] == "completed"
    assert client.calls == [("search", {"query": "state authority"})]

    failed, audits = await _run(
        tmp_path,
        spec,
        _grant(spec),
        policy=policy,
        mcp_client=_McpFixture(failure=True),
    )
    serialized = json.dumps({"result": failed, "audits": audits})
    assert failed["status"] == "failed"
    assert "mcp-secret" not in serialized


@pytest.mark.asyncio
async def test_mcp_approval_path_reuses_action_gateway(tmp_path: Path) -> None:
    spec = _contract(name="publish", kind="mcp", server="github", effect="remote")
    client = _McpFixture()
    approved, _ = await _run(
        tmp_path,
        spec,
        _grant(spec),
        policy=lambda request: obase.ActionDecision(
            verdict="REQUIRE_APPROVAL", request_id=request.request_id
        ),
        approval=lambda _request: True,
        mcp_client=client,
        operation_key="mcp-publish-once",
        side_effect_recorder=lambda **kwargs: kwargs["provider"](),
    )
    assert approved["status"] == "completed"
    assert client.calls == [("publish", {})]


@pytest.mark.asyncio
async def test_veya_adapter_binds_existing_callable_to_canonical_engine(tmp_path: Path) -> None:
    from server.tool_governance_adapter import ToolGovernanceAdapter

    audits: list[dict[str, Any]] = []
    adapter = ToolGovernanceAdapter(
        audit_writer=lambda record: audits.append(record.to_dict()),
        output_dir=tmp_path,
    )
    spec = _contract()
    adapter.register_native(spec, lambda path: {"path": path})
    result = await adapter.execute("read_document", {"path": "README.md"})
    assert result["status"] == "completed"
    assert result["executed"] is True
    assert result["result"] == {"path": "README.md"}
    assert len(audits) == 3


def test_mcp_registry_versions_and_invalidation_fail_closed() -> None:
    registry = obase.McpClientRegistry
    registry.clear()
    try:
        spec = obase.MCPServerSpec(
            name="knowledge",
            endpoint="https://user:password@example.test/mcp",
            version="2",
            tools=(_contract(name="search", kind="mcp", server="knowledge"),),
        )
        engine = oservi.MCPRegistryEngine(
            registry=registry,
            trigger={"on_demand": True},
            config={},
            name="test-mcp-registry",
        )
        engine.run()
        client = _McpFixture()
        assert engine.register(spec, client=client)["status"] == "registered"
        assert registry.spec("knowledge").enabled is True
        assert "password" not in json.dumps(registry.spec("knowledge").to_dict())
        assert engine.invalidate("knowledge")["status"] == "invalidated"
        assert registry.spec("knowledge").enabled is False
    finally:
        registry.clear()


@pytest.mark.asyncio
async def test_zero_trust_vault_redacts_hitl_event_and_physical_exception(tmp_path: Path) -> None:
    from obase.event_bus import EventBus
    from obase.secrets_store import SecretsStore
    from oskill.zero_trust_vault import ZeroTrustVault

    secret = "vault-secret-123"
    bus = EventBus()
    vault = ZeroTrustVault(
        store=SecretsStore(tmp_path / "vault"),
        event_bus=bus,
        approval_timeout=1,
    )
    vault.set_secret("github", secret)

    async def physical(**_kwargs: Any) -> str:
        raise RuntimeError(f"remote response echoed {secret}")

    task = asyncio.create_task(
        vault.execute_secure_tool(
            "publish",
            {"password": secret, "nested": {"token": secret}},
            "github",
            physical,
        )
    )
    await asyncio.sleep(0)
    pending = vault.get_pending()[0]["task_id"]
    assert secret not in json.dumps(bus.history("vault_hitl")[0].payload)
    assert vault.resolve_approval(pending, True) is True
    result = await task
    assert secret not in result


def test_tool_governance_engine_declares_one_injected_source_per_layer() -> None:
    points = oservi.ToolGovernanceEngine.injection_points
    assert points["registry"].kind == "obase"
    assert points["tool_resolve"].kind == "oprim"
    assert points["prepare_tool_execution"].kind == "oskill"
    assert points["governed_tool_transaction"].kind == "omodul"
    assert points["secret_read"].kind == "oprim"
    assert points["audit_writer"].kind == "layer4"
    assert points["side_effect_recorder"].kind == "layer4"
    assert "tool_governance" in oservi.list_skeletons()
    assert "mcp_registry" in oservi.list_skeletons()
    assert omodul.GovernedToolConfig._enabled_pillars >= {"fingerprint", "decision_trail"}


def test_new_3o_elements_do_not_reverse_import_oservi() -> None:
    root = Path(__file__).resolve().parents[1] / "platform" / "3O"
    for package, names in {
        "obase": ("tool_governance.py",),
        "oprim": (
            "credential_resolve.py",
            "grant_check.py",
            "mcp_call.py",
            "secret_read.py",
            "tool_call.py",
            "tool_resolve.py",
        ),
        "oskill": (
            "classify_tool_effect.py",
            "prepare_tool_execution.py",
            "resolve_tool_grant.py",
        ),
        "omodul": ("governed_tool_transaction.py", "governed_mcp_transaction.py"),
    }.items():
        for name in names:
            source = (root / package / package / name).read_text(encoding="utf-8")
            assert "import oservi" not in source
            assert "from oservi" not in source

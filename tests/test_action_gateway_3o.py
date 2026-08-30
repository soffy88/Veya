"""PR-09 Action Gateway contract tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.action_gateway_adapter import ActionGatewayAdapter
from server.tool_registry import SideEffect
from veya.platform import load

obase = load("obase")
oprim = load("oprim")
oskill = load("oskill")
omodul = load("omodul")
oservi = load("oservi")


def _request(*, action: str = "read_file", effect: str = "read"):
    return obase.ActionRequest(
        action=action,
        effect=effect,
        resource="workspace:file",
        arguments={"path": "README.md", "api_token": "must-not-leak"},
        actor="test",
    )


async def _govern(request, audits, *, policy, approval=None, writer=None, tmp_path=None):
    return await omodul.govern_action(
        omodul.GovernActionConfig(),
        omodul.GovernActionInput(
            request=request,
            policy_evaluator=policy,
            approval_resolver=approval,
            audit_append=oprim.audit_append,
            audit_writer=writer or audits.append,
        ),
        tmp_path or Path(".veya") / "test-action-gateway",
    )


@pytest.mark.asyncio
async def test_allow_read_and_deny_policy(tmp_path):
    audits = []
    allowed = await _govern(
        _request(),
        audits,
        policy=lambda request: oskill.evaluate_action_policy(request),
        tmp_path=tmp_path,
    )
    assert allowed["status"] == "completed"
    assert allowed["decision"]["verdict"] == "ALLOW"

    denied = await _govern(
        _request(action="delete_file", effect="destructive"),
        audits,
        policy=lambda request: obase.ActionDecision(
            verdict="DENY", reason="explicit deny", request_id=request.request_id
        ),
        tmp_path=tmp_path,
    )
    assert denied["status"] == "failed"
    assert denied["decision"]["verdict"] == "DENY"


@pytest.mark.asyncio
async def test_require_approval_is_resolved_once(tmp_path):
    audits = []
    approvals = []

    async def approve(request):
        approvals.append(request.request_id)
        return True

    result = await _govern(
        _request(action="publish", effect="remote"),
        audits,
        policy=lambda request: obase.ActionDecision(
            verdict="REQUIRE_APPROVAL", reason="remote", request_id=request.request_id
        ),
        approval=approve,
        tmp_path=tmp_path,
    )
    assert result["decision"]["verdict"] == "ALLOW"
    assert result["decision"]["approved"] is True
    assert len(approvals) == 1


@pytest.mark.asyncio
async def test_policy_exception_denies_and_unapproved_has_no_side_effect(tmp_path):
    audits = []

    def broken(_request):
        raise RuntimeError("broken policy")

    result = await _govern(
        _request(action="write_file", effect="local_write"),
        audits,
        policy=broken,
        tmp_path=tmp_path,
    )
    assert result["decision"]["verdict"] == "DENY"

    calls = []
    request = _request(action="publish", effect="remote")
    governed = await _govern(
        request,
        audits,
        policy=lambda value: obase.ActionDecision(
            verdict="REQUIRE_APPROVAL", reason="approval required", request_id=value.request_id
        ),
        tmp_path=tmp_path,
    )
    execution = await omodul.execute_governed_action(
        omodul.ExecuteGovernedActionConfig(),
        omodul.ExecuteGovernedActionInput(
            request=request,
            decision=obase.ActionDecision(**governed["decision"]),
            executor=lambda _request: calls.append(True),
            audit_append=oprim.audit_append,
            audit_writer=audits.append,
            side_effect_record=oprim.side_effect_record,
            side_effect_recorder=lambda **_kwargs: pytest.fail("unapproved action reached ledger"),
        ),
        tmp_path,
    )
    assert execution["status"] == "failed"
    assert calls == []


@pytest.mark.asyncio
async def test_approved_action_is_recorded_and_duplicate_is_idempotent(tmp_path):
    audits = []
    calls = []
    records = {}

    async def recorder(**kwargs):
        key = kwargs["operation_key"]
        if key in records:
            return records[key]
        result = await kwargs["provider"]()
        records[key] = result
        return result

    request = _request(action="write_file", effect="local_write")
    decision = obase.ActionDecision(
        verdict="ALLOW", reason="approved", request_id=request.request_id
    )

    async def physical(_request):
        calls.append(1)
        return {"written": True}

    def audit(record):
        audits.append(record.to_dict())

    inputs = dict(
        request=request,
        decision=decision,
        executor=physical,
        audit_append=oprim.audit_append,
        audit_writer=audit,
        side_effect_record=oprim.side_effect_record,
        side_effect_recorder=recorder,
        operation_key="stable-write-key",
        target_ref="workspace:file",
    )
    first = await omodul.execute_governed_action(
        omodul.ExecuteGovernedActionConfig(),
        omodul.ExecuteGovernedActionInput(**inputs),
        tmp_path,
    )
    second = await omodul.execute_governed_action(
        omodul.ExecuteGovernedActionConfig(),
        omodul.ExecuteGovernedActionInput(**inputs),
        tmp_path,
    )
    assert first["status"] == second["status"] == "completed"
    assert calls == [1]
    assert len(records) == 1
    assert all("must-not-leak" not in str(record) for record in audits)


@pytest.mark.asyncio
async def test_audit_failure_is_fail_closed_before_executor(tmp_path):
    calls = []

    def broken_audit(_record):
        raise OSError("audit unavailable")

    result = await omodul.execute_governed_action(
        omodul.ExecuteGovernedActionConfig(),
        omodul.ExecuteGovernedActionInput(
            request=_request(),
            decision=obase.ActionDecision(verdict="ALLOW"),
            executor=lambda _request: calls.append(True),
            audit_append=oprim.audit_append,
            audit_writer=broken_audit,
        ),
        tmp_path,
    )
    assert result["status"] == "failed"
    assert result["executed"] is False
    assert calls == []


def test_3o_single_source_and_engine_injection_contract():
    assert obase.ActionRequest.__module__.startswith("obase.")
    assert oprim.tool_invoke.__module__.startswith("oprim.")
    assert oskill.evaluate_action_policy.__module__.startswith("oskill.")
    assert omodul.govern_action.__module__.startswith("omodul.")
    points = oservi.ActionGatewayEngine.injection_points
    assert points["policy_evaluator"].kind == "oskill"
    assert points["audit_append"].kind == "oprim"
    assert points["executor"].kind == "oprim"
    assert points["side_effect_record"].kind == "oprim"
    assert "action_gateway" in oservi.list_skeletons()


@pytest.mark.asyncio
async def test_engine_uses_injected_atomic_executor(tmp_path):
    audits = []
    calls = []

    async def writer(record):
        audits.append(record.to_dict())

    async def physical(request):
        calls.append(request.action)
        return "ok"

    engine = oservi.ActionGatewayEngine(
        policy_evaluator=oskill.evaluate_action_policy,
        audit_append=oprim.audit_append,
        executor=oprim.tool_invoke,
        side_effect_record=oprim.side_effect_record,
        audit_writer=writer,
        side_effect_recorder=None,
        trigger={"on_demand": True},
        config={},
        name="test-action-gateway",
    )
    engine.run()
    result = await engine.invoke(_request(), physical_executor=physical, output_dir=tmp_path)
    assert result["status"] == "completed"
    assert calls == ["read_file"]
    assert engine.health()["status"] == "healthy"


@pytest.mark.asyncio
async def test_cancellation_does_not_execute_after_policy_wait(tmp_path):
    gate = asyncio.Event()

    async def policy(_request):
        await gate.wait()
        return obase.ActionDecision(verdict="ALLOW")

    task = asyncio.create_task(
        omodul.govern_action(
            omodul.GovernActionConfig(),
            omodul.GovernActionInput(
                request=_request(),
                policy_evaluator=policy,
                audit_append=oprim.audit_append,
                audit_writer=lambda _record: None,
            ),
            tmp_path,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_veya_adapter_assembles_existing_event_writer(tmp_path):
    audits = []
    adapter = ActionGatewayAdapter(
        policy_profile="DEVELOPMENT",
        audit_writer=lambda record: audits.append(record.to_dict()),
    )
    result = await adapter.execute(
        "read_file",
        {"path": "README.md"},
        lambda **kwargs: {"path": kwargs["path"]},
        side_effect=SideEffect.PURE_READ,
    )
    assert result["status"] == "completed"
    assert result["executed"] is True
    assert len(audits) == 3

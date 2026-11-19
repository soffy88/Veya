"""YAML workflow DSL: validation, safe templates, approvals, risk gates."""

from __future__ import annotations

import pytest
import yaml

from server import workflows as wf
from server.workflows.executor import WorkflowExecutor
from server.workflows.schema import WorkflowValidationError, parse_workflow, render_template
from veya.obase.approvals import STATUS_PENDING, ApprovalStore


def _def(steps: list[dict], **overrides: object) -> wf.WorkflowDef:
    data = {
        "id": "wf1",
        "on": {"kind": "event", "topic": "test.event"},
        "steps": steps,
        **overrides,
    }
    return parse_workflow(data)


def test_loader_reads_workspace_yaml_and_empty_dir(tmp_path):
    empty = wf.load_workflows(tmp_path)
    assert empty == []
    folder = tmp_path / ".veya" / "workflows"
    folder.mkdir(parents=True)
    (folder / "wf1.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "wf1",
                "on": {"kind": "schedule", "cron": "0 9 * * *"},
                "steps": [{"id": "s1", "kind": "tool", "tool": "echo", "args": {"x": "hi"}}],
            }
        ),
        encoding="utf-8",
    )
    items = wf.load_workflows(tmp_path)
    assert len(items) == 1 and items[0].id == "wf1"
    assert items[0].trigger.kind == "schedule"


def test_schema_rejects_unknown_kinds_and_unsafe_ids():
    with pytest.raises(WorkflowValidationError):
        _def([{"id": "s1", "kind": "shell"}])
    with pytest.raises(WorkflowValidationError):
        _def([{"id": "s;rm", "kind": "delay", "seconds": 1}])
    with pytest.raises(WorkflowValidationError):
        parse_workflow({"id": "wf1", "on": {"kind": "schedule"}, "steps": []})


def test_templates_are_lookup_only_no_eval():
    context = {"trigger": {"value": "ok"}, "steps": {"a": {"output": {"n": 2}}}}
    assert render_template("{{trigger.value}}/{{steps.a.output.n}}", context) == "ok/2"
    assert render_template("{{trigger.__class__}}", context) == "{{trigger.__class__}}"
    assert render_template("{{__import__('os')}}", context) == "{{__import__('os')}}"


@pytest.mark.asyncio
async def test_executor_renders_and_runs_tool(tmp_path):
    seen = []

    def runner(name, args):
        seen.append((name, args))
        return {"ok": True}

    store = ApprovalStore(str(tmp_path / "a.db"))
    executor = WorkflowExecutor(approvals=store, tool_runner=runner)
    workflow = _def(
        [
            {
                "id": "s1",
                "kind": "tool",
                "tool": "local_tool",
                "args": {"x": "{{trigger.value}}"},
            }
        ]
    )
    result = await executor.run(workflow, {"value": "hello"})
    assert result.status == "completed"
    assert seen == [("local_tool", {"x": "hello"})]


@pytest.mark.asyncio
async def test_approval_suspends_and_resumes(tmp_path):
    store = ApprovalStore(str(tmp_path / "a.db"))
    calls = []
    executor = WorkflowExecutor(
        approvals=store, tool_runner=lambda name, args: calls.append(name) or {"ok": True}
    )
    workflow = _def(
        [
            {"id": "gate", "kind": "approval"},
            {"id": "after", "kind": "tool", "tool": "local_tool", "args": {}},
        ]
    )
    first = await executor.run(workflow, {"value": "x"})
    assert first.status == "suspended" and first.approval_id
    assert store.get_approval(first.approval_id)["status"] == STATUS_PENDING
    assert calls == []
    resumed = await executor.run(
        workflow, {}, approval_id=first.approval_id, resume_decision="once"
    )
    assert resumed.status == "completed" and calls == ["local_tool"]


@pytest.mark.asyncio
async def test_outbound_step_needs_owner_and_risk_gate(tmp_path):
    store = ApprovalStore(str(tmp_path / "a.db"))
    calls = []
    denied = WorkflowExecutor(
        approvals=store,
        tool_runner=lambda name, args: calls.append(name),
        is_owner=lambda: False,
    )
    workflow = _def([{"id": "send", "kind": "tool", "tool": "send_email", "args": {}}])
    result = await denied.run(workflow)
    assert result.status == "denied" and "owner" in result.reason
    assert calls == []

    allowed = WorkflowExecutor(
        approvals=store,
        tool_runner=lambda name, args: calls.append(name) or {"sent": True},
        is_owner=lambda: True,
    )
    gated = await allowed.run(workflow)
    assert gated.status == "suspended" and gated.approval_id
    done = await allowed.run(workflow, approval_id=gated.approval_id, resume_decision="once")
    assert done.status == "completed" and calls == ["send_email"]


@pytest.mark.asyncio
async def test_flag_off_means_no_workflows(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WORKFLOWS", "0")
    folder = tmp_path / ".veya" / "workflows"
    folder.mkdir(parents=True)
    (folder / "wf1.yaml").write_text("id: wf1\n", encoding="utf-8")
    assert wf.load_workflows(tmp_path) == []


@pytest.mark.asyncio
async def test_schedule_and_webhook_triggers_match(tmp_path):
    folder = tmp_path / ".veya" / "workflows"
    folder.mkdir(parents=True)
    (folder / "cron.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "cron",
                "on": {"kind": "schedule", "cron": "0 9 * * *"},
                "steps": [{"id": "n", "kind": "notify", "text": "hi"}],
            }
        ),
        encoding="utf-8",
    )
    (folder / "hook.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "hook",
                "on": {"kind": "webhook", "topic": "deploy.done"},
                "steps": [{"id": "n", "kind": "notify", "text": "hi"}],
            }
        ),
        encoding="utf-8",
    )
    items = {d.id: d for d in wf.load_workflows(tmp_path)}
    assert items["cron"].trigger.matches(kind="schedule")
    assert items["hook"].trigger.matches(kind="webhook", topic="deploy.done")
    assert not items["hook"].trigger.matches(kind="webhook", topic="other")

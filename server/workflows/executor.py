"""Execute declarative workflow definitions against existing Veya surfaces.

Approvals use `veya.obase.approvals.ApprovalStore` (suspend + durable
resume). Outbound/destructive tool steps are risk-classified via
`server.action_risk` and require explicit owner authorization.  Scheduler
integration stays with `server.automata`/`RoutineRegistry`; this executor
only runs an already-triggered definition.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from veya.obase.approvals import (
    DECISION_DENY,
    STATUS_PENDING,
    STATUS_RESOLVED,
    ApprovalStore,
)

from .schema import WorkflowDef, WorkflowStep

ToolRunner = Callable[[str, dict[str, Any]], Any]


@dataclass
class WorkflowResult:
    status: str
    workflow_id: str
    outputs: dict[str, Any] = field(default_factory=dict)
    approval_id: str | None = None
    reason: str = ""


def _owner_allowed(step: WorkflowStep, is_owner: bool) -> bool:
    # `owner_only=True` explicitly requires owner; a step may also request the
    # owner gate implicitly (e.g. external side effects checked by caller).
    return bool(is_owner) or not step.owner_only


class WorkflowExecutor:
    """Small, synchronous-state executor; never introduces a second mainline."""

    def __init__(
        self,
        *,
        approvals: ApprovalStore | None = None,
        tool_runner: ToolRunner | None = None,
        is_owner: Callable[[], bool] | None = None,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.approvals = approvals or ApprovalStore()
        self.tool_runner = tool_runner
        self.is_owner = is_owner or (lambda: False)
        self.sleep = sleep or asyncio.sleep

    async def run(
        self,
        workflow: WorkflowDef,
        trigger: Mapping[str, Any] | None = None,
        *,
        approval_id: str | None = None,
        resume_decision: str | None = None,
    ) -> WorkflowResult:
        """Execute steps in order; return `suspended` when an approval is due."""

        if not workflow.enabled:
            return WorkflowResult("skipped", workflow.id, reason="disabled")
        context: dict[str, Any] = {"trigger": dict(trigger or {}), "steps": {}}
        start = 0
        if approval_id:
            row = self.approvals.get_approval(approval_id)
            if row is None:
                return WorkflowResult("blocked", workflow.id, reason="unknown approval")
            if str(row.get("status")) == STATUS_PENDING:
                if resume_decision:
                    row = self.approvals.resolve_approval(approval_id, decision=resume_decision)
                else:
                    return WorkflowResult("suspended", workflow.id, approval_id=approval_id)
            if row is None or str(row.get("decision")) == DECISION_DENY:
                return WorkflowResult("denied", workflow.id, approval_id=approval_id)
            if str(row.get("status")) != STATUS_RESOLVED:
                return WorkflowResult("suspended", workflow.id, approval_id=approval_id)
            try:
                stored = row.get("tool_args") or {}
                start = int(stored.get("resume_from") or 0)
                context = dict((row.get("tool_args") or {}).get("context") or context)
                resume_args = stored.get("args") or {}
                resumed_step = workflow.steps[start]
                if resumed_step.kind == "approval":
                    start += 1  # the gate itself is complete after approval
                elif resumed_step.kind == "tool":
                    output = self.tool_runner(resumed_step.tool, dict(resume_args))
                    if asyncio.iscoroutine(output):
                        output = await output
                    context["steps"][resumed_step.id] = {"output": output}
                    start += 1
            except (TypeError, ValueError):
                return WorkflowResult("blocked", workflow.id, reason="corrupt approval")

        for index, step in enumerate(workflow.steps):
            if index < start:
                continue
            rendered = {
                "args": _render(step.args, context),
                "text": _render(step.text, context),
            }
            external_step = step.kind == "tool" and self._needs_authorization(
                step, rendered["args"]
            )
            if external_step and not self.is_owner():
                return WorkflowResult("denied", workflow.id, reason="owner authorization required")
            if step.kind == "approval" or external_step:
                if not _owner_allowed(step, self.is_owner()):
                    return WorkflowResult(
                        "denied", workflow.id, reason="owner authorization required"
                    )
                request_id = uuid.uuid4().hex
                self.approvals.create_approval(
                    {
                        "request_id": request_id,
                        "scope": f"workflow:{workflow.id}",
                        "tool": step.tool or "workflow.approval",
                        "tool_args": {
                            "resume_from": index,
                            "context": context,
                            "args": rendered["args"],
                        },
                        "actor": "workflow",
                    }
                )
                return WorkflowResult("suspended", workflow.id, approval_id=request_id)
            if step.kind == "delay":
                await self.sleep(min(float(step.seconds), 3600.0))
            elif step.kind in {"tool", "agent", "notify"} and self.tool_runner is not None:
                tool_name = step.tool or f"workflow.{step.kind}"
                output = self.tool_runner(tool_name, dict(rendered["args"]))
                if asyncio.iscoroutine(output):
                    output = await output
                context["steps"][step.id] = {"output": output}
                context["steps"][step.id]["output"]["text"] = rendered["text"]
            context.setdefault("steps", {}).setdefault(step.id, {})
        return WorkflowResult("completed", workflow.id, outputs=context["steps"])

    @staticmethod
    def _needs_authorization(step: WorkflowStep, args: dict[str, Any]) -> bool:
        if not step.tool:
            return False
        from server.action_risk import RISK_DESTRUCTIVE, RISK_OUTBOUND, classify_action

        return classify_action(step.tool, args) in {RISK_OUTBOUND, RISK_DESTRUCTIVE}


def _render(value: Any, context: Mapping[str, Any]) -> Any:
    from .schema import render_template

    return render_template(value, context)

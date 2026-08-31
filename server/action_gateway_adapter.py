"""Veya Layer-4 assembly for the canonical 3O Action Gateway.

This module binds task/session context, the existing user approval mechanism,
canonical Veya events, and the existing :class:`SideEffectLedger` to 3O
callables.  It does not define a second policy, audit, or ledger store.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any, cast

from runtime.execution.side_effects import SideEffectLedger
from server.events import append_canonical_event, current_task_id
from server.permission_profiles import (
    ProfileName,
    RiskLevel,
    current_profile,
    decide,
    default_profile,
)
from server.tool_registry import SideEffect
from veya.platform import load


def _effect_for(
    name: str,
    side_effect: SideEffect | str | None,
    *,
    classify: Callable[..., str],
) -> str:
    if side_effect is not None:
        raw = side_effect.value if isinstance(side_effect, SideEffect) else str(side_effect)
        return {
            "pure_read": "read",
            "local_write": "local_write",
            "process_exec": "process",
            "network_write": "network",
            "external_mutation": "remote",
            "privileged": "destructive",
        }.get(raw, raw)
    return classify(name)


class ActionGatewayAdapter:
    """Bind one Veya task to one reusable 3O Action Gateway instance."""

    def __init__(
        self,
        *,
        ledger: SideEffectLedger | None = None,
        goal_run_id: str | None = None,
        work_item_id: str | None = None,
        approval_resolver: Callable[..., Any] | None = None,
        audit_writer: Callable[..., Any] | None = None,
        policy_profile: ProfileName | str | None = None,
        policy_hook: Callable[[Any], Any] | None = None,
    ) -> None:
        self.ledger = ledger
        self.goal_run_id = goal_run_id or current_task_id() or "veya:unbound"
        self.work_item_id = work_item_id or self.goal_run_id
        self._approval_resolver = approval_resolver
        self._audit_writer = audit_writer or self._append_event
        self._policy_profile = policy_profile
        self._policy_hook = policy_hook

    @staticmethod
    def _append_event(record: Any) -> Any:
        payload = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        return append_canonical_event(
            "action_gateway.audit",
            payload,
            actor=str(payload.get("actor") or "system"),
            task_id=current_task_id(),
        )

    async def _resolve_approval(self, request: Any) -> bool:
        if self._approval_resolver is not None:
            result = self._approval_resolver(request)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        from server.user_control import request_approval

        obase = load("obase")
        safe_arguments = obase.redact_value(dict(request.arguments))
        return await request_approval(request.action, safe_arguments)

    def _evaluate_policy(self, request: Any) -> Any:
        if self._policy_hook is not None:
            try:
                override = self._policy_hook(request)
                if override is not None:
                    return override
            except Exception as exc:
                obase = load("obase")
                return obase.ActionDecision(
                    verdict="DENY",
                    reason=f"policy hook failed: {type(exc).__name__}",
                    request_id=request.request_id,
                )
        oskill = load("oskill")
        profile = self._policy_profile or current_profile() or default_profile()
        permission = decide(profile, request.action, dict(request.arguments))
        decision = {
            "allow": "ALLOW",
            "deny": "DENY",
            "ask": "REQUIRE_APPROVAL",
        }[permission.action]
        obase = load("obase")
        rule = obase.PolicyRule(
            rule_id=f"veya-profile:{permission.profile.value}",
            action=request.action,
            effect=request.effect,
            resource=request.resource or "*",
            decision=decision,
        )
        if request.effect != "read" and permission.risk == RiskLevel.R0:
            return obase.ActionDecision(
                verdict="REQUIRE_APPROVAL",
                reason="non-read effect has no named permission profile",
                request_id=request.request_id,
            )
        if not request.context.get("side_effect_declared") and request.effect != "read":
            return obase.ActionDecision(
                verdict="REQUIRE_APPROVAL",
                reason="unannotated non-read action requires explicit policy",
                request_id=request.request_id,
            )
        return oskill.evaluate_action_policy(
            request,
            rules=[rule],
            context={"scope": permission.scope, "risk": permission.risk.value},
        )

    async def _record_side_effect(self, **kwargs: Any) -> Any:
        if self.ledger is None:
            raise RuntimeError("SideEffectLedger is required for non-read actions")
        request = kwargs["request"]
        return await self.ledger.execute(
            goal_run_id=self.goal_run_id,
            work_item_id=self.work_item_id,
            operation_key=str(kwargs["operation_key"]),
            operation_type=str(kwargs["operation_type"]),
            target_ref=str(kwargs["target_ref"]),
            request=request.to_dict(),
            provider=kwargs["provider"],
            capability=str(kwargs.get("capability", "manual_only")),
        )

    async def execute(
        self,
        name: str,
        kwargs: Mapping[str, Any] | None,
        executor: Callable[..., Any],
        *,
        side_effect: SideEffect | str | None = None,
        effect_capability: str = "manual_only",
        operation_version: str = "1",
        resource: str = "",
        source: str = "master_tool",
        request_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Govern and execute a Veya callable through the 3O chain."""
        obase = load("obase")
        oskill = load("oskill")
        oprim = load("oprim")
        oservi = load("oservi")
        effect = _effect_for(name, side_effect, classify=oskill.classify_action_effect)
        context = {
            "task_id": current_task_id(),
            "side_effect_declared": side_effect is not None,
        }
        context.update(dict(request_context or {}))
        request = obase.ActionRequest(
            action=name,
            effect=effect,
            resource=resource or name,
            arguments=dict(kwargs or {}),
            actor="master",
            source=source,
            context=context,
        )

        async def physical(request_value: Any) -> Any:
            result = executor(**dict(request_value.arguments))
            if inspect.isawaitable(result):
                return await result
            return result

        engine = oservi.ActionGatewayEngine(
            policy_evaluator=self._evaluate_policy,
            audit_append=oprim.audit_append,
            executor=oprim.tool_invoke,
            side_effect_record=oprim.side_effect_record,
            approval_resolver=self._resolve_approval,
            audit_writer=self._audit_writer,
            side_effect_recorder=self._record_side_effect,
            trigger={"on_demand": True},
            config={},
            name="veya-action-gateway",
        )
        return cast(
            dict[str, Any],
            await engine.invoke(
                request,
                physical_executor=physical,
                operation_key=f"veya:{name}:{operation_version}:{request.fingerprint}",
                target_ref=resource or name,
                capability=effect_capability,
            ),
        )


__all__ = ["ActionGatewayAdapter"]

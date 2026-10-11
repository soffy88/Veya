"""Veya Layer-4 assembly for the canonical 3O Action Gateway.

This module binds task/session context, the existing user approval mechanism,
canonical Veya events, and the existing :class:`SideEffectLedger` to 3O
callables.  It does not define a second policy, audit, or ledger store.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from runtime.bot_scope import DEFAULT_BOT_ID
from runtime.execution.side_effects import SideEffectLedger
from server.authorization import Principal, authorize_tool
from server.events import append_canonical_event, current_task_id
from server.permission_profiles import ProfileName
from server.tool_registry import SideEffect
from veya.platform import load
from veya.remote.executor_registry import get_executor_registry
from veya.remote.permission_engine import OperationContext, PermissionEngine


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
    classified = str(classify(name) or "").strip().lower()
    # `classify_action_effect` answers "remote" for anything it does not
    # recognise, and "remote" is a scope rather than an effect. Measured over the
    # live 146-tool surface it returns "remote" for 138 of them, so taking it at
    # face value would have declared almost the whole tool surface to be a remote
    # mutation — and with the engine now honouring a declared remote effect, that
    # would have put 138 tools behind approval. A non-match is unknown, which is
    # what the effect registry already prescribes.
    return "unknown" if classified == "remote" else classified


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
        output_dir: str | Path | None = None,
        # P3-A: the bot that owns this gateway binding. Its side effects are
        # recorded under this bot and never replayed by another bot.
        bot_id: str = DEFAULT_BOT_ID,
        authorization_principal: Principal | None = None,
        # NANO-W10 item 3: an optional pre-policy reviewer.  It may only
        # *downgrade* a verdict (allow -> ask/block); it can never grant
        # something the policy chain would have refused, and it is bound only
        # while VEYA_AUTO_REVIEW is on, so the default path is unchanged.
        review_hook: Callable[[Any], Any] | None = None,
    ) -> None:
        self.ledger = ledger
        self.goal_run_id = goal_run_id or current_task_id() or "veya:unbound"
        self.work_item_id = work_item_id or self.goal_run_id
        self.bot_id = bot_id
        self.authorization_principal = authorization_principal
        self.review_hook = review_hook
        self._approval_resolver = approval_resolver
        self._audit_writer = audit_writer or self._append_event
        self._policy_profile = policy_profile
        self._policy_hook = policy_hook
        self._permission_engine = PermissionEngine()
        self._policy_resolver = None
        if output_dir is not None:
            self.output_dir = Path(output_dir).expanduser()
        else:
            configured_output = os.environ.get("VEYA_OUTPUT_DIR", "").strip()
            base = (
                Path(configured_output).expanduser()
                if configured_output
                else Path.home() / ".veya" / "runs"
            )
            self.output_dir = base / "action_gateway"

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

    def _get_policy_resolver(self) -> Any:
        if self._policy_resolver is None:
            from veya.remote.policy_resolver import PolicyResolver

            self._policy_resolver = PolicyResolver(engine=self._permission_engine)
        return self._policy_resolver

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
        effect = str(request.effect or "read")
        effect_to_field = {
            "read": ("read", "none", "none"),
            "local_write": ("write", "none", "none"),
            "process": ("none", "inspect", "none"),
            "network": ("none", "none", "network"),
            "remote": ("none", "none", "none"),
            "destructive": ("write", "none", "none"),
            "privileged": ("write", "none", "none"),
        }
        filesystem_effect, process_effect, network_effect = effect_to_field.get(
            effect, ("write", "none", "none")
        )
        target = str(request.resource or "")
        target_path = Path(target).expanduser() if target and target != "*" else None
        service_effect = (
            "system"
            if request.action.startswith("systemctl") and "--user" not in str(request.arguments)
            else "user"
            if request.action.startswith("systemctl")
            else "none"
        )
        context = OperationContext(
            actor=self.bot_id,
            tool=request.action,
            operation=request.action,
            workspace_root=Path.cwd(),
            cwd=Path.cwd(),
            target_paths=(target_path,) if target_path else (),
            filesystem_effect=filesystem_effect,
            process_effect=process_effect,
            network_effect=network_effect,
            service_effect=service_effect,
            privilege_level="host" if effect == "privileged" else "user",
            reversibility="destructive" if effect == "destructive" else "reversible",
            remote_effect="mutation" if effect == "remote" else "none",
            goal_run_id=self.goal_run_id,
            executor_identity=(
                get_executor_registry().identity(str(request.arguments.get("worker")))
                if isinstance(request.arguments, Mapping) and request.arguments.get("worker")
                else None
            ),
        )
        try:
            from veya.remote.policy_resolver import PolicyRequest

            resolver = self._get_policy_resolver()
            policy_request = PolicyRequest(
                actor=self.bot_id,
                tool=request.action,
                args=dict(request.arguments) if isinstance(request.arguments, Mapping) else {},
                workspace=str(Path.cwd()),
                cwd=str(Path.cwd()),
                goal_id=self.goal_run_id,
                # Server-side declared effect. Without this the resolver built
                # its own context and lost it, so the engine never saw it.
                effect=str(request.effect or ""),
            )
            policy_decision = resolver.resolve(policy_request)
            decision = policy_decision.decision.value
            reason = policy_decision.reason
        except Exception:
            permission = self._permission_engine.evaluate(context)
            decision = permission.decision.value
            reason = permission.reason.value
        if decision == "APPROVAL_REQUIRED":
            settled = self._settle_approval_sync(request, str(reason))
            if settled is not None:
                decision, reason = settled
        obase = load("obase")
        return obase.ActionDecision(
            verdict=decision,
            reason=str(reason),
            request_id=request.request_id,
        )

    def evaluate_policy(self, request: Any) -> Any:
        """Evaluate the canonical policy for one request.

        The same authority :meth:`execute` uses, exposed for a caller that only
        needs the *decision* — a REST endpoint asking "may this happen?" — so it
        does not have to stage the action to find out. It adds no policy of its
        own and records nothing; only :meth:`execute` performs an effect.

        The reviewer is deliberately *not* consulted here: this is the raw policy
        answer, and a caller asking "may this happen?" must not have a model call
        silently move the answer.
        """
        return self._evaluate_policy(request)

    async def _evaluate_policy_with_review(self, request: Any) -> Any:
        """The engine's policy evaluator: the review verdict, then the policy.

        The reviewer may only turn an ALLOW into an ask or a block.  It runs
        first so a blocked command never reaches the permission engine, and any
        failure inside it denies — a reviewer that could not answer has granted
        nothing.
        """

        from server.feature_flags import enabled as flag_enabled

        if not flag_enabled("VEYA_AUTO_REVIEW"):
            return self._evaluate_policy(request)
        hook = self.review_hook
        if hook is None:
            return self._evaluate_policy(request)
        obase = load("obase")
        try:
            outcome = hook(request)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            verdict = str((outcome or {}).get("verdict") or "").lower()
            reason = str((outcome or {}).get("reason") or "auto-review")
            if verdict == "ask":
                # APPROVAL_REQUIRED is the engine's own "a human must decide"
                # verdict, so the escalation lands on the existing
                # SideEffectLedger approval path below rather than a new one.
                settled = self._settle_approval_sync(request, f"auto-review: {reason}")
                if settled is not None:
                    return obase.ActionDecision(
                        verdict=settled[0],
                        reason=settled[1],
                        request_id=request.request_id,
                    )
                return obase.ActionDecision(
                    verdict="APPROVAL_REQUIRED",
                    reason=f"auto-review asked for a human: {reason}",
                    request_id=request.request_id,
                )
            if verdict == "block":
                return obase.ActionDecision(
                    verdict="DENY",
                    reason=f"auto-review blocked this action: {reason}",
                    request_id=request.request_id,
                )
        except Exception as exc:
            return obase.ActionDecision(
                verdict="DENY",
                reason=f"auto-review failed: {type(exc).__name__}",
                request_id=request.request_id,
            )
        return self._evaluate_policy(request)

    def _settle_approval_sync(self, request: Any, reason: str) -> tuple[str, str] | None:
        """Resolve an approval requirement here, because nothing downstream will.

        The injected ActionGatewayEngine maps every non-ALLOW verdict to
        `{"status": "failed"}` and never consults its own `approval_resolver`, so
        an approval requirement surfaces as a failure even when the caller supplied
        a resolver that would have granted it. Settling it here means the engine
        only ever sees ALLOW or DENY, which is what its contract implies.

        Returns None when there is nothing to settle — no resolver, so the verdict
        is left alone and the caller's own approval flow applies.

        An awaitable result cannot be resolved from this synchronous policy hook.
        That is reported as an explicit denial rather than passed through as a
        silent failure or, worse, allowed: a requirement nobody could evaluate must
        not become permission.
        """
        resolver = self._approval_resolver
        if resolver is None:
            return None
        try:
            outcome = resolver(request)
        except Exception as exc:
            return (
                "DENY",
                f"{reason}; approval resolver failed: {type(exc).__name__}",
            )
        if inspect.isawaitable(outcome):
            return (
                "DENY",
                f"{reason}; approval resolver is asynchronous and cannot be "
                "evaluated from the synchronous policy hook",
            )
        if bool(outcome):
            return "ALLOW", f"{reason}; approval granted"
        return "DENY", f"{reason}; approval refused"

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
            bot_id=self.bot_id,
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
        arguments = dict(kwargs or {})
        stable_payload = {
            "goal_run_id": self.goal_run_id,
            "work_item_id": self.work_item_id,
            "action": name,
            "effect": effect,
            "resource": resource or name,
            "operation_version": operation_version,
            "source": source,
            "capability": effect_capability,
            "arguments": obase.redact_value(arguments),
        }
        stable_digest = hashlib.sha256(
            json.dumps(stable_payload, sort_keys=True, ensure_ascii=False, default=str).encode(
                "utf-8"
            )
        ).hexdigest()[:24]
        request = obase.ActionRequest(
            action=name,
            effect=effect,
            resource=resource or name,
            arguments=arguments,
            actor="master",
            request_id=f"veya-operation:{stable_digest}",
            source=source,
            context=context,
        )
        if self.authorization_principal is not None:
            authorize_tool(
                self.authorization_principal,
                name,
                physical=effect != "read",
                gateway_bound=True,
            )

        async def physical(request_value: Any) -> Any:
            result = executor(**dict(request_value.arguments))
            if inspect.isawaitable(result):
                return await result
            return result

        engine = oservi.ActionGatewayEngine(
            policy_evaluator=self._evaluate_policy_with_review,
            audit_append=oprim.audit_append,
            executor=oprim.tool_invoke,
            side_effect_record=oprim.side_effect_record,
            approval_resolver=self._resolve_approval,
            audit_writer=self._audit_writer,
            side_effect_recorder=self._record_side_effect,
            trigger={"on_demand": True},
            config={"output_dir": str(self.output_dir)},
            name="veya-action-gateway",
        )
        return cast(
            dict[str, Any],
            await engine.invoke(
                request,
                physical_executor=physical,
                operation_key=f"veya:{self.goal_run_id}:{self.work_item_id}:{name}:{operation_version}:{stable_digest}",
                target_ref=resource or name,
                capability=effect_capability,
            ),
        )

def build_auto_review_hook(
    llm_caller: Callable[..., Any] | None = None,
    *,
    instruction: str = "",
    untrusted_sources: list[str] | None = None,
    output_dir: Path | None = None,
) -> Callable[[Any], Any]:
    """Create a review hook callable for ActionGatewayAdapter.

    Uses omodul_auto_review with provenance checked against firewall.sanitize.
    """
    from veya.omodul.auto_review import AutoReviewConfig, AutoReviewInput, auto_review

    async def _hook(request: Any) -> dict[str, Any]:
        action = getattr(request, "action", "") or ""
        args = dict(getattr(request, "arguments", {}) or {})
        effect = getattr(request, "effect", "") or ""
        command = ""
        if action in ("terminal_run", "shell_exec", "run_command", "exec"):
            command = str(args.get("command") or args.get("cmd") or action)
        else:
            command = action

        inst_text = str(instruction or "")
        untrusted = list(untrusted_sources or [])
        if inst_text:
            try:
                from server.firewall import VeyaFirewall

                sanitized = VeyaFirewall.sanitize(inst_text)
                if sanitized.get("has_injection") or sanitized.get("risk_level") in (
                    "high",
                    "critical",
                    "suspicious",
                ):
                    if "instruction_injection" not in untrusted:
                        untrusted.append("instruction_injection")
            except Exception:
                pass

        from server.feature_flags import enabled as flag_enabled

        if flag_enabled("VEYA_BOT_SECRETS"):
            try:
                from server.bot_team.secrets import bot_secret_store
                from veya.oskill.pure.secret_masking import mask_secret_values, redact_secrets_payload

                m_map = bot_secret_store().get_masking_map()
                if m_map:
                    command = mask_secret_values(command, m_map)
                    inst_text = mask_secret_values(inst_text, m_map)
                    args = redact_secrets_payload(args, m_map)
            except Exception:
                pass

        cfg = AutoReviewConfig(llm_caller=llm_caller)
        inp = AutoReviewInput(
            command=command,
            instruction=inst_text,
            tool=action,
            effect=effect,
            untrusted_sources=untrusted,
            arguments=args,
            goal_run_id=getattr(request, "goal_run_id", "") or "",
        )

        return await auto_review(cfg, inp, output_dir=output_dir)

    return _hook


__all__ = ["ActionGatewayAdapter", "build_auto_review_hook"]

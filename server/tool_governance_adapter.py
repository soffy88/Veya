"""Veya Layer-4 assembly for the canonical 3O tool/MCP governance chain.

This adapter binds existing Veya tool callables, user approval, canonical
ActionGateway, SideEffectLedger, and audit events.  It owns no policy,
approval store, ledger, MCP transport, or secret storage.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from runtime.execution.side_effects import SideEffectLedger
from server.action_gateway_adapter import ActionGatewayAdapter
from server.events import current_task_id
from veya.platform import load

_task_governance_ctx: contextvars.ContextVar[TaskGovernanceContext | None] = contextvars.ContextVar(
    "veya_task_governance", default=None
)


def current_task_governance() -> TaskGovernanceContext | None:
    """Return the product-task governance context, if one is active."""

    return _task_governance_ctx.get()


def bind_task_governance(
    *, task_id: str, session_id: str, trace_id: str, output_dir: str | Path | None = None
) -> contextvars.Token:
    """Bind one task to the existing Layer-4 governance adapter.

    This is a request context, not another policy or ledger.  It lets the
    existing MasterToolRegistry and SkillHub retain their public protocols
    while product executions use the PR-13 canonical tool boundary.
    """

    return _task_governance_ctx.set(
        TaskGovernanceContext(
            task_id=task_id,
            session_id=session_id,
            trace_id=trace_id,
            output_dir=Path(output_dir) if output_dir is not None else None,
        )
    )


def reset_task_governance(token: contextvars.Token) -> None:
    """Restore the previous task governance context."""

    _task_governance_ctx.reset(token)


def _effect_for_legacy(name: str, declared: str | None, arguments: Mapping[str, Any]) -> str:
    """Map existing Veya metadata to the canonical 3O effect vocabulary."""

    if declared:
        return {
            "pure_read": "read",
            "local_write": "local_write",
            "process_exec": "process",
            "network_write": "network",
            "external_mutation": "remote",
            "privileged": "destructive",
        }.get(declared, declared)
    # Existing Veya risk classification is the only legacy fallback.  An
    # unclassified dynamic skill is treated as process work (fail closed in
    # production profiles), never silently promoted to a read operation.
    from server.permission_profiles import RiskLevel, classify_risk

    risk = classify_risk(name, dict(arguments))
    return {
        RiskLevel.R0: "read",
        RiskLevel.R1: "local_write",
        RiskLevel.R2: "process",
        RiskLevel.R3: "network",
        RiskLevel.R4: "destructive",
    }[risk]


def _operation_key(task_id: str, name: str, arguments: Mapping[str, Any]) -> str:
    """Create a stable task-scoped key when the legacy API has no call id."""

    payload = json.dumps(dict(arguments), sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"legacy:{task_id}:{name}:{digest}"


@dataclass
class TaskGovernanceContext:
    """Thin product binding for legacy registry/SkillHub execution.

    The physical callable is injected per invocation.  Policy, approval,
    audit, and idempotency remain owned by the existing 3O Action Gateway and
    SideEffectLedger implementations.
    """

    task_id: str
    session_id: str
    trace_id: str
    output_dir: Path | None = None
    _adapter: ToolGovernanceAdapter | None = field(default=None, init=False, repr=False)
    _ledger: SideEffectLedger | None = field(default=None, init=False, repr=False)
    _ledger_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _grants: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    async def _ensure_adapter(self, *, needs_ledger: bool) -> ToolGovernanceAdapter:
        async with self._ledger_lock:
            if self._adapter is None:
                self._adapter = ToolGovernanceAdapter(
                    ledger=None,
                    goal_run_id=self.task_id,
                    work_item_id=self.task_id,
                    audit_writer=self._write_audit,
                    output_dir=self.output_dir
                    or Path(".veya") / "runs" / self.task_id / "outputs" / "tool_governance",
                )
            if needs_ledger and self._ledger is None:
                self._ledger = await self._build_ledger()
                self._adapter.bind_ledger(self._ledger)
            return self._adapter

    async def _build_ledger(self) -> SideEffectLedger:
        """Reuse the durable runtime repository, with the existing SQLite
        repository as the local/test fallback when durable runtime is off.
        """

        from runtime.execution.durable import DurableExecutionRepository
        from runtime.execution.runtime import get_durable_runtime

        durable = get_durable_runtime()
        if durable.config.enabled:
            if not durable._started:
                await durable.start()
            repository = durable.repository
        else:
            repository = DurableExecutionRepository(
                sqlite_path=Path(
                    os.environ.get("VEYA_EXECUTION_SQLITE_PATH", ".veya/execution-runtime.sqlite3")
                )
            )
            await repository.migrate()
        return SideEffectLedger(repository)

    def _write_audit(self, record: Any) -> Any:
        """Persist the existing audit record with the product task context."""

        from server.events import append_canonical_event

        payload = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        return append_canonical_event(
            "action_gateway.audit",
            payload,
            actor=str(payload.get("actor") or "system"),
            session_id=self.session_id,
            trace_id=self.trace_id,
            task_id=self.task_id,
        )

    async def execute_native(
        self,
        *,
        name: str,
        arguments: Mapping[str, Any],
        executor: Callable[..., Any],
        schema: Mapping[str, Any] | None = None,
        declared_effect: str | None = None,
        effect_capability: str = "manual_only",
        operation_version: str = "1",
        kind: str = "native",
    ) -> str:
        """Govern one existing native callable and return the legacy string ABI."""

        del kind  # Native registry entries are intentionally represented as native ToolSpec.
        effect = _effect_for_legacy(name, declared_effect, arguments)
        adapter = await self._ensure_adapter(needs_ledger=effect != "read")
        obase = load("obase")
        spec = obase.ToolSpec(
            name=name,
            kind="native",
            effect=effect,
            input_schema=dict(schema or {}),
            capabilities=frozenset({effect_capability}),
            version=operation_version,
        )

        async def physical(**kwargs: Any) -> Any:
            from server.tool_registry import _invoke_callback

            return await _invoke_callback(executor, dict(kwargs))

        adapter.register_native(spec, physical)
        grant = self._grants.get(spec.identity)
        if grant is None:
            obase = load("obase")
            grant = obase.Grant(
                tool=spec.identity,
                subject="master",
                allowed_effects=frozenset({spec.effect}),
                tool_version=spec.version,
            )
            self._grants[spec.identity] = grant
        operation_key = _operation_key(self.task_id, name, arguments)
        result = await adapter.execute(
            name,
            arguments,
            grant=grant,
            request_id=operation_key,
            operation_key=operation_key,
            target_ref=spec.identity,
            capability=effect_capability,
        )
        return self._legacy_result(name, result)

    async def execute_skill(
        self,
        *,
        name: str,
        arguments: Mapping[str, Any],
        executor: Callable[..., Any],
        schema: Mapping[str, Any] | None = None,
        declared_effect: str | None = None,
    ) -> str:
        """Govern a dynamic skill while allowing SkillHub to keep its schema
        validation and dispatch semantics inside the physical callable.
        """

        effective_name = name
        if name == "run_skill":
            effective_name = str(arguments.get("skill_name") or name)
        effect = _effect_for_legacy(
            effective_name,
            declared_effect,
            arguments,
        )
        # A dynamic skill has no stable side-effect annotation in old
        # manifests.  Unknown skills are process work, which is conservative.
        if (
            declared_effect is None
            and effective_name == name
            and name
            not in {
                "list_skills",
                "skill_search",
                "skill_show",
            }
        ):
            effect = "process"
        adapter = await self._ensure_adapter(needs_ledger=effect != "read")
        obase = load("obase")
        spec = obase.ToolSpec(
            name=name,
            kind="native",
            effect=effect,
            input_schema=dict(schema or {}),
            capabilities=frozenset({"manual_only"}),
        )

        async def physical(**kwargs: Any) -> Any:
            # The governed outer call is the canonical approval/audit boundary;
            # SkillHub's old guard is skipped by its governed execution helper.
            return await executor(**dict(kwargs))

        adapter.register_native(spec, physical)
        grant = self._grants.get(spec.identity)
        if grant is None:
            obase = load("obase")
            grant = obase.Grant(
                tool=spec.identity,
                subject="master",
                allowed_effects=frozenset({spec.effect}),
                tool_version=spec.version,
            )
            self._grants[spec.identity] = grant
        operation_key = _operation_key(self.task_id, name, arguments)
        result = await adapter.execute(
            name,
            arguments,
            grant=grant,
            request_id=operation_key,
            operation_key=operation_key,
            target_ref=spec.identity,
            capability="manual_only",
        )
        return self._legacy_result(name, result)

    @staticmethod
    def _legacy_result(name: str, result: Mapping[str, Any]) -> str:
        if result.get("status") == "completed" and result.get("executed"):
            from server.tool_registry import _to_str

            return _to_str(result.get("result"), limit=8000)
        error = result.get("error")
        error_type = error.get("type") if isinstance(error, Mapping) else "ToolGovernanceError"
        raise RuntimeError(f"tool '{name}' was not executed ({error_type})")


class ToolGovernanceAdapter:
    """Bind Layer-4 native/MCP tools to one reusable governance engine."""

    def __init__(
        self,
        *,
        ledger: SideEffectLedger | None = None,
        goal_run_id: str | None = None,
        work_item_id: str | None = None,
        approval_resolver: Callable[..., Any] | None = None,
        audit_writer: Callable[..., Any] | None = None,
        policy_profile: str | None = None,
        output_dir: str | Path = ".veya/tool_governance",
    ) -> None:
        self._action_gateway = ActionGatewayAdapter(
            ledger=ledger,
            goal_run_id=goal_run_id,
            work_item_id=work_item_id,
            approval_resolver=approval_resolver,
            audit_writer=audit_writer,
            policy_profile=policy_profile,
        )
        self.output_dir = Path(output_dir)
        self._specs: dict[str, Any] = {}
        self._native: dict[str, Callable[..., Any]] = {}
        self._mcp: dict[str, Any] = {}
        self._engine = self._build_engine()
        self._engine.run()

    def _build_engine(self) -> Any:
        oprim = load("oprim")
        oskill = load("oskill")
        omodul = load("omodul")
        oservi = load("oservi")
        return oservi.ToolGovernanceEngine(
            registry=self._specs,
            tool_resolve=oprim.tool_resolve,
            prepare_tool_execution=oskill.prepare_tool_execution,
            governed_tool_transaction=omodul.governed_tool_transaction,
            govern_action=omodul.govern_action,
            execute_governed_action=omodul.execute_governed_action,
            policy_evaluator=self._action_gateway._evaluate_policy,
            tool_call=oprim.tool_call,
            mcp_call=oprim.mcp_call,
            credential_resolve=oprim.credential_resolve,
            secret_read=oprim.secret_read,
            audit_append=oprim.audit_append,
            audit_writer=self._action_gateway._audit_writer,
            approval_resolver=self._action_gateway._resolve_approval,
            side_effect_record=oprim.side_effect_record,
            side_effect_recorder=self._action_gateway._record_side_effect,
            trigger={"on_demand": True},
            config={"output_dir": str(self.output_dir)},
            name="veya-tool-governance",
        )

    @property
    def engine(self) -> Any:
        return self._engine

    def bind_ledger(self, ledger: SideEffectLedger) -> None:
        """Attach the existing ledger after a task first needs a write gate."""

        self._action_gateway.ledger = ledger

    def register_native(self, spec: Any, executor: Callable[..., Any]) -> None:
        """Register a native ToolSpec and its existing physical callable."""
        obase = load("obase")
        if not isinstance(spec, obase.ToolSpec) or spec.kind != "native":
            raise ValueError("register_native requires a native obase.ToolSpec")
        self._specs[spec.identity] = spec
        self._native[spec.identity] = executor

    def register_mcp(self, spec: Any, client: Any) -> None:
        """Register a versioned MCP server/tool contract and existing client."""
        obase = load("obase")
        if not isinstance(spec, obase.MCPServerSpec):
            raise ValueError("register_mcp requires an obase.MCPServerSpec")
        registry = obase.McpClientRegistry
        registry.register_server(spec, client)
        for tool in spec.tools:
            if tool.credential_ref is None and spec.credential_ref is not None:
                from dataclasses import replace

                tool = replace(tool, credential_ref=spec.credential_ref)
            self._specs[tool.identity] = tool
            self._mcp[tool.identity] = client

    def _lookup(self, name: str, *, kind: str, server: str | None, version: str) -> Any:
        identity = (
            f"native/{name}@{version}" if kind == "native" else f"mcp/{server}/{name}@{version}"
        )
        spec = self._specs.get(identity)
        if spec is None:
            raise KeyError(f"tool contract not found: {identity}")
        if kind == "mcp":
            obase = load("obase")
            server_spec = obase.McpClientRegistry.spec(str(server))
            registered_tool = server_spec.tool(name, version)
            if (
                not server_spec.enabled
                or registered_tool is None
                or registered_tool.identity != identity
            ):
                raise PermissionError(f"MCP tool contract is stale or disabled: {identity}")
        return spec

    @staticmethod
    def _grant(spec: Any, actor: str) -> Any:
        obase = load("obase")
        return obase.Grant(
            tool=spec.identity,
            subject=actor,
            allowed_effects=frozenset({spec.effect}),
            tool_version=spec.version,
        )

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        kind: str = "native",
        server: str | None = None,
        version: str = "1",
        actor: str = "master",
        grant: Any | None = None,
        request_id: str | None = None,
        credential_resolver: Any = None,
        output_dir: str | Path | None = None,
        operation_key: str = "",
        target_ref: str = "",
        capability: str = "manual_only",
        on_step: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one native or MCP call through ToolGovernanceEngine."""
        obase = load("obase")
        spec = self._lookup(name, kind=kind, server=server, version=version)
        selected_grant = grant or self._grant(spec, actor)
        ref = spec.credential_ref
        request = obase.ToolCallRequest(
            tool=spec.name,
            kind=spec.kind,
            server=spec.server,
            version=spec.version,
            arguments=dict(arguments or {}),
            actor=actor,
            source="veya_tool_governance",
            grant=selected_grant,
            credential_ref=ref,
            request_id=request_id or uuid.uuid4().hex,
            context={"task_id": current_task_id()},
        )
        executor = None
        client = None
        if spec.kind == "native":
            physical = self._native.get(spec.identity)
            if physical is None:
                return {
                    "status": "failed",
                    "error": {"type": "ToolNotFound", "message": "native executor missing"},
                    "executed": False,
                }

            async def executor(action_request: Any, _injected_secret: str | None = None) -> Any:
                kwargs = dict(request.arguments)
                if _injected_secret is not None:
                    kwargs["_injected_secret"] = _injected_secret
                result = physical(**kwargs)
                if inspect.isawaitable(result):
                    return await result
                return result
        else:
            client = self._mcp.get(spec.identity)
            if client is None:
                return {
                    "status": "failed",
                    "error": {"type": "McpClientMissing", "message": "MCP client missing"},
                    "executed": False,
                }

        return cast(
            dict[str, Any],
            await self._engine.invoke(
                request,
                spec=spec,
                registry=self._specs,
                executor=executor,
                mcp_client=client,
                credential_resolver=credential_resolver,
                operation_key=operation_key or request.fingerprint,
                target_ref=target_ref or spec.identity,
                capability=capability,
                output_dir=Path(output_dir) if output_dir else None,
                on_step=on_step,
            ),
        )

    def revoke(self, grant: Any) -> Any:
        """Return a revoked copy; grants remain caller-owned data."""
        from dataclasses import replace

        return replace(grant, revoked=True)


__all__ = [
    "TaskGovernanceContext",
    "ToolGovernanceAdapter",
    "bind_task_governance",
    "current_task_governance",
    "reset_task_governance",
]

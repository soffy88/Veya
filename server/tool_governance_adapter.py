"""Veya Layer-4 assembly for the canonical 3O tool/MCP governance chain.

This adapter binds existing Veya tool callables, user approval, canonical
ActionGateway, SideEffectLedger, and audit events.  It owns no policy,
approval store, ledger, MCP transport, or secret storage.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from runtime.execution.side_effects import SideEffectLedger
from server.action_gateway_adapter import ActionGatewayAdapter
from server.events import current_task_id
from veya.platform import load


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


__all__ = ["ToolGovernanceAdapter"]

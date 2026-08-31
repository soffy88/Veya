"""Veya Layer-4 assembly for the canonical 3O Browser Computer.

This module binds the existing Computer Supervisor and Action Gateway to the
3O browser mechanism.  Browser driver operations remain in ``obase`` and
``oprim``; this adapter only supplies Veya worktree/task context.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from runtime.execution.side_effects import SideEffectLedger
from server.action_gateway_adapter import ActionGatewayAdapter
from server.computer_supervisor_adapter import ComputerSupervisorAdapter, _computer_profile
from server.tool_registry import SideEffect
from veya.platform import load


class BrowserComputerAdapter:
    """Bind one browser session to one existing Veya computer/worktree."""

    def __init__(
        self,
        *,
        name: str = "veya-browser-computer",
        output_dir: Path | None = None,
        browser_adapter: Any | None = None,
        ledger: SideEffectLedger | None = None,
        goal_run_id: str | None = None,
        work_item_id: str | None = None,
        approval_resolver: Callable[..., Any] | None = None,
        audit_writer: Callable[..., Any] | None = None,
        policy_profile: str | None = None,
    ) -> None:
        obase = load("obase")
        oprim = load("oprim")
        omodul = load("omodul")
        oservi = load("oservi")
        oskill = load("oskill")
        self.output_dir = output_dir
        self.computer = ComputerSupervisorAdapter(
            name=f"{name}-computer",
            output_dir=output_dir,
        )
        self.browser_adapter = browser_adapter or obase.PlaywrightBrowserAdapter()

        def bind(atomic: Callable[..., Any]) -> Callable[..., Any]:
            async def bound(*args: Any, **kwargs: Any) -> Any:
                kwargs["adapter"] = self.browser_adapter
                return await atomic(*args, **kwargs)

            return bound

        self._browser_create = bind(oprim.browser_create)
        self._browser_start = bind(oprim.browser_start)
        self._browser_status = bind(oprim.browser_status)
        self._browser_attach = bind(oprim.browser_attach)
        self._browser_stop = bind(oprim.browser_stop)
        self._browser_reset = bind(oprim.browser_reset)
        self._browser_set_control_state = bind(oprim.browser_set_control_state)
        self._browser_actions = {
            "navigate": bind(oprim.browser_navigate),
            "snapshot": bind(oprim.browser_snapshot),
            "click": bind(oprim.browser_click),
            "type": bind(oprim.browser_type),
            "download": bind(oprim.browser_download),
            "upload": bind(oprim.browser_upload),
            "screenshot": bind(oprim.browser_screenshot),
        }

        async def computer_prepare(
            profile: Any,
            *,
            attach: bool = False,
            output_dir: Path | None = None,
            on_step: Callable[[dict[str, Any]], Any] | None = None,
        ) -> dict[str, Any]:
            return await self.computer.prepare_profile(
                profile,
                attach=attach,
                output_dir=output_dir,
                on_step=on_step,
            )

        def takeover_policy(request: Any) -> Any:
            handle = request.context.get("browser_handle", {})
            state = request.context.get("control_state")
            if not state and isinstance(handle, Mapping):
                state = handle.get("control_state", "AGENT_CONTROL")
            decision = oskill.review_browser_takeover_need(
                request.action,
                control_state=str(state or "AGENT_CONTROL"),
                context=request.context,
            )
            if decision["verdict"] == "ALLOW_AGENT":
                return None
            verdict = "DENY"
            reason = f"{decision['verdict']}: {decision['reason']}"
            return obase.ActionDecision(
                verdict=verdict,
                reason=reason,
                policy_id="browser-takeover",
                request_id=request.request_id,
            )

        self.gateway = ActionGatewayAdapter(
            ledger=ledger,
            goal_run_id=goal_run_id,
            work_item_id=work_item_id,
            approval_resolver=approval_resolver,
            audit_writer=audit_writer,
            policy_profile=policy_profile,
            policy_hook=takeover_policy,
        )
        self.engine = oservi.BrowserComputerEngine(
            computer_prepare=computer_prepare,
            browser_create=self._browser_create,
            browser_start=self._browser_start,
            browser_status=self._browser_status,
            browser_attach=self._browser_attach,
            browser_stop=self._browser_stop,
            browser_reset=self._browser_reset,
            browser_set_control_state=self._browser_set_control_state,
            prepare_browser_session=omodul.prepare_browser_session,
            trigger={"on_demand": True},
            config={"output_dir": str(output_dir) if output_dir else ".veya/browser"},
            name=name,
        )

    def run(self) -> None:
        self.computer.run()
        self.engine.run()

    def stop(self) -> None:
        self.engine.stop()
        self.computer.stop()

    async def prepare_worktree(
        self,
        worktree_path: str | Path,
        *,
        profile_id: str = "local_restricted",
        owner_id: str = "",
        browser_profile: Any | None = None,
        attach: bool = False,
        on_step: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        target = self.computer._assert_worktree(worktree_path)
        computer_profile = _computer_profile(target, profile_id, owner_id=owner_id)
        obase = load("obase")
        selected_browser = browser_profile or obase.BrowserProfile(
            id=f"browser-{target.name}",
        )
        self.run()
        result = cast(
            dict[str, Any],
            await self.engine.prepare(
                computer_profile,
                selected_browser,
                attach=attach,
                output_dir=self.output_dir,
                on_step=on_step,
            ),
        )
        result["worktree_path"] = str(target)
        return result

    async def status(self, handle: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.engine.status(handle))

    async def stop_browser(self, handle: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.engine.stop_browser(handle))

    async def reset(self, handle: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.engine.reset(handle))

    async def take_control(self, handle: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.engine.take_control(handle))

    async def return_control(self, handle: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.engine.return_control(handle))

    @staticmethod
    def _session_state(status: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
        raw = status.get("handle") or status.get("browser") or {}
        handle = raw if isinstance(raw, Mapping) else {}
        return str(handle.get("control_state") or "AGENT_CONTROL"), handle

    async def action(
        self,
        handle: Any,
        action: str,
        *,
        context: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run one browser action through the existing Action Gateway."""
        normalized = action.removeprefix("browser_")
        if normalized not in self._browser_actions:
            return {"status": "failed", "error": f"unknown browser action: {action}"}
        status = cast(dict[str, Any], await self._browser_status(handle))
        if not status.get("ok"):
            return status
        control_state, status_handle = self._session_state(status)
        request_context = dict(context or {})
        request_context.update(
            {
                "browser_handle": dict(status_handle),
                "control_state": control_state,
                "action_arguments": {
                    key: value for key, value in kwargs.items() if key not in {"text", "file_paths"}
                },
            }
        )
        safe_kwargs = {
            key: value for key, value in kwargs.items() if key not in {"text", "file_paths"}
        }
        action_context = {"session_id": status_handle.get("session_id", ""), **safe_kwargs}
        physical = self._browser_actions[normalized]

        async def execute(**_request_arguments: Any) -> Any:
            return await physical(handle, **kwargs)

        oskill = load("oskill")
        effect = oskill.classify_browser_action_effect(normalized)
        side_effect = SideEffect.PURE_READ if effect == "read" else SideEffect.NETWORK_WRITE
        result = await self.gateway.execute(
            f"browser_{normalized}",
            action_context,
            execute,
            side_effect=side_effect,
            effect_capability="manual_only",
            resource=f"browser:{status_handle.get('session_id', '')}",
            source="browser_computer",
            request_context=request_context,
        )
        takeover = oskill.review_browser_takeover_need(
            normalized,
            control_state=control_state,
            context=request_context,
        )
        result["browser_takeover"] = takeover
        if takeover["verdict"] != "ALLOW_AGENT":
            result["verdict"] = takeover["verdict"]
        return result


__all__ = ["BrowserComputerAdapter"]

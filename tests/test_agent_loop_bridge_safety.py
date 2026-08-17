"""Safety regressions for the strict agent-loop bridge."""

from __future__ import annotations

from typing import Any

import pytest


class _ScriptedLlm:
    def __init__(self, script: list[dict]) -> None:
        self._script = script
        self._calls = 0
        self.seen_tools: list[list[dict]] = []

    async def complete(self, messages: list[dict], **kwargs: Any) -> dict:
        self.seen_tools.append(kwargs.get("tools") or [])
        reply = self._script[min(self._calls, len(self._script) - 1)]
        self._calls += 1
        return {"choices": [{"message": reply}]}

    async def close(self) -> None:
        pass


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


@pytest.fixture(autouse=True)
def _clean_global_tool_guard():
    from server.tool_guard import global_tool_guard

    policies = list(global_tool_guard._policies)
    trail = list(global_tool_guard._trail)
    global_tool_guard.clear_policies()
    global_tool_guard._trail.clear()
    yield
    global_tool_guard._policies[:] = policies
    global_tool_guard._trail[:] = trail


@pytest.mark.asyncio
async def test_default_executor_keeps_master_tool_guard(tmp_path):
    """Strict dispatch must not reach a raw callback after ToolGuard denies it."""
    from server.agent_loop_bridge import run_strict_chat
    from server.tool_guard import global_tool_guard
    from server.tool_registry import master_tools

    physical_calls: list[str] = []

    def physical_tool(value: str) -> str:
        physical_calls.append(value)
        return f"unsafe:{value}"

    master_tools.register(
        "strict_guarded_test_tool",
        "A test-only guarded tool",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        physical_tool,
    )
    global_tool_guard.register_policy(
        "deny_strict_test_tool",
        lambda name, kwargs, source: (
            "blocked in test" if name == "strict_guarded_test_tool" else None
        ),
        enforce=True,
    )
    llm = _ScriptedLlm(
        [
            _tool_call("strict_guarded_test_tool", {"value": "secret"}),
            {"role": "assistant", "content": "denial handled"},
        ]
    )

    try:
        result = await run_strict_chat(
            "run guarded tool",
            llm=llm,
            max_rounds=3,
            kv_path=str(tmp_path / "guarded.db"),
        )
    finally:
        master_tools.unregister("strict_guarded_test_tool")

    assert physical_calls == []
    assert result["tool_calls"] == [{"tool": "strict_guarded_test_tool", "ok": False}]
    denial = global_tool_guard.trail()[-1]
    assert denial["tool"] == "strict_guarded_test_tool"
    assert denial["source"] == "master_tool"
    assert denial["decision"] == "deny"


@pytest.mark.asyncio
async def test_injected_complete_tool_surface_uses_injected_executor(tmp_path):
    """Injected schemas are visible to the LLM and dispatch through one executor."""
    from server.agent_loop_bridge import run_strict_chat

    schemas = [
        {
            "type": "function",
            "function": {
                "name": "dynamic_test_tool",
                "description": "A tool supplied by the caller",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
            },
        }
    ]
    dispatched: list[tuple[str, dict]] = []

    async def executor(name: str, kwargs: dict) -> str:
        dispatched.append((name, kwargs))
        return f"injected:{kwargs['value']}"

    llm = _ScriptedLlm(
        [
            _tool_call("dynamic_test_tool", {"value": 7}),
            {"role": "assistant", "content": "injected tool completed"},
        ]
    )
    result = await run_strict_chat(
        "run injected tool",
        llm=llm,
        max_rounds=3,
        kv_path=str(tmp_path / "injected.db"),
        tool_schemas=schemas,
        tool_executor=executor,
    )

    assert llm.seen_tools[0] == schemas
    assert dispatched == [("dynamic_test_tool", {"value": 7})]
    assert result["final_answer"] == "injected tool completed"
    assert result["tool_calls"] == [{"tool": "dynamic_test_tool", "ok": True}]


@pytest.mark.asyncio
async def test_strict_system_tool_obeys_plan_mode_guard(tmp_path):
    """The full strict path must deny a mutating system tool before execution."""
    from server import user_control
    from server.agent_loop_bridge import run_strict_chat
    from server.coordinator_master import MasterCoordinator

    class Automata:
        called = False

        def register_cron_task(self, **_kwargs: Any) -> str:
            self.called = True
            return "scheduled"

    automata = Automata()
    coordinator = MasterCoordinator(
        automata=automata,
        llm_fn=lambda *_args, **_kwargs: None,
    )
    llm = _ScriptedLlm(
        [
            _tool_call(
                "system_create_automation",
                {"cron_expr": "0 9 * * *", "task_prompt": "unsafe"},
            ),
            {"role": "assistant", "content": "denial handled"},
        ]
    )
    tokens = user_control.activate(
        mode="plan", require_approval=True, session_id="strict-plan"
    )
    try:
        result = await run_strict_chat(
            "schedule it",
            llm=llm,
            max_rounds=3,
            kv_path=str(tmp_path / "strict-plan.db"),
            tool_schemas=coordinator._agent.get_all_tool_schemas(),
            tool_executor=coordinator._agent.handle_tool_call,
        )
    finally:
        user_control.deactivate(tokens)

    assert automata.called is False
    assert result["tool_calls"] == [{"tool": "system_create_automation", "ok": False}]


@pytest.mark.asyncio
async def test_strict_uses_injected_llm_caller_and_request_config(tmp_path):
    from server.agent_loop_bridge import run_strict_chat

    seen: dict[str, Any] = {}

    async def caller(_messages: list[dict], **kwargs: Any) -> dict:
        seen.update(kwargs)
        return {
            "choices": [{"message": {"role": "assistant", "content": "custom backend"}}]
        }

    result = await run_strict_chat(
        "hello",
        llm_caller=caller,
        llm_kwargs={"provider": "custom", "model": "custom-model"},
        max_rounds=1,
        kv_path=str(tmp_path / "custom-llm.db"),
        tool_schemas=[],
    )

    assert result["final_answer"] == "custom backend"
    assert seen["provider"] == "custom"
    assert seen["model"] == "custom-model"

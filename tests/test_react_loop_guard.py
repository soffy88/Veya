"""Focused regressions for the MasterAgent post-tool convergence guard."""

from __future__ import annotations

import asyncio
import json

import pytest

from oservi.master_agent import MasterAgent


def _tool_response(name: str, args: dict[str, object]) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call-{name}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }
        ],
        "usage": {},
    }


def _text_response(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {}}


class _Tools:
    def __init__(self, results: dict[str, str], failures: set[str] | None = None) -> None:
        self.results = results
        self.failures = failures or set()
        self.calls: list[str] = []

    def get_all_schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in self.results
        ]

    def list_tools(self) -> list[str]:
        return list(self.results)

    def describe(self, name: str) -> str:
        return name

    def has(self, name: str) -> bool:
        return name in self.results

    async def execute(self, name: str, kwargs: dict) -> str:
        self.calls.append(name)
        if name in self.failures:
            self.failures.remove(name)
            raise RuntimeError("intentional failure")
        return self.results[name]


class _Skills:
    def get_all_schemas(self) -> list[dict]:
        return []

    def list_skills(self) -> list[str]:
        return []

    def describe(self, name: str) -> str:
        return name

    def reload_skills(self) -> dict[str, int]:
        return {"loaded": 0, "skipped": 0}

    async def execute(self, name: str, kwargs: dict) -> str:
        raise AssertionError(f"unexpected skill call: {name}")


class _Memory:
    def inject_subconscious(self) -> str:
        return ""


def _agent(tools: _Tools, notify=None, *, max_rounds: int = 5) -> MasterAgent:
    return MasterAgent(
        llm_caller=lambda *_args, **_kwargs: None,
        tools=tools,
        skill_hub=_Skills(),
        memory=_Memory(),
        swarm=None,
        vault=None,
        max_rounds=max_rounds,
        notify=notify,
    )


@pytest.mark.asyncio
async def test_tool_result_is_fed_to_next_model_round() -> None:
    tools = _Tools({"read_probe": "observed"})
    calls: list[list[dict]] = []

    async def llm(messages, **_kwargs):
        calls.append(list(messages))
        return _tool_response("read_probe", {}) if len(calls) == 1 else _text_response("final")

    agent = _agent(tools)
    agent._llm_caller = llm

    result = await agent.chat_stream("inspect")

    assert result["status"] == "success"
    assert result["final_answer"] == "final"
    assert len(calls) == 2
    assert "[Tool read_probe SUCCESS]" in calls[1][-1]["content"]
    assert tools.calls == ["read_probe"]


@pytest.mark.asyncio
async def test_verified_tool_result_ends_without_an_extra_provider_round() -> None:
    tools = _Tools(
        {
            "coding_task_run": json.dumps(
                {"status": "completed", "acceptance_passed": True, "goal_run_id": "goal-1"}
            )
        }
    )
    calls = 0

    async def llm(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        return _tool_response("coding_task_run", {})

    agent = _agent(tools)
    agent._llm_caller = llm

    result = await agent.chat_stream("code")

    assert result["status"] == "success"
    assert calls == 1
    assert result["rounds"] == 1
    assert "acceptance_passed" in result["final_answer"]


@pytest.mark.asyncio
async def test_failure_can_continue_to_corrected_verified_action() -> None:
    tools = _Tools(
        {
            "check_once": "failed check",
            "corrected_check": json.dumps({"status": "completed", "acceptance_passed": True}),
        },
        failures={"check_once"},
    )
    events: list[dict] = []
    calls: list[list[dict]] = []

    async def llm(messages, **_kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return _tool_response("check_once", {})
        return _tool_response("corrected_check", {})

    agent = _agent(tools, events.append)
    agent._llm_caller = llm

    result = await agent.chat_stream("recover")

    assert result["status"] == "success"
    assert tools.calls == ["check_once", "corrected_check"]
    assert len(calls) == 2
    assert "[Tool check_once FAILED]" in calls[1][-1]["content"]
    assert "acceptance_passed" in result["final_answer"]


@pytest.mark.asyncio
async def test_repeated_action_is_replanned_and_cannot_spin_forever() -> None:
    tools = _Tools({"read_probe": "observed"})
    events: list[dict] = []

    async def llm(_messages, **_kwargs):
        return _tool_response("read_probe", {})

    agent = _agent(tools, events.append, max_rounds=8)
    agent._llm_caller = llm

    result = await agent.chat_stream("loop")

    assert result["status"] == "failed"
    assert result["rounds"] == 3
    assert tools.calls == ["read_probe"]
    assert any(event.get("type") == "master_replan" for event in events)
    assert any(event.get("reason") == "repeated_noop" for event in events)


@pytest.mark.asyncio
async def test_minimal_coding_task_continues_from_action_to_verification() -> None:
    tools = _Tools(
        {
            "write_marker": "marker written",
            "verify_marker": json.dumps({"status": "completed", "acceptance_passed": True}),
        }
    )
    calls: list[list[dict]] = []

    async def llm(messages, **_kwargs):
        calls.append(list(messages))
        return (
            _tool_response("write_marker", {})
            if len(calls) == 1
            else _tool_response("verify_marker", {})
        )

    agent = _agent(tools)
    agent._llm_caller = llm

    result = await asyncio.wait_for(
        agent.chat_stream("coding: write and verify the marker"), timeout=1
    )

    assert result["status"] == "success"
    assert tools.calls == ["write_marker", "verify_marker"]
    assert len(calls) == 2
    assert "acceptance_passed" in result["final_answer"]


@pytest.mark.asyncio
async def test_minimal_research_task_continues_from_fetch_to_final() -> None:
    tools = _Tools({"fetch_url": "official documentation body"})
    calls: list[list[dict]] = []

    async def llm(messages, **_kwargs):
        calls.append(list(messages))
        return _tool_response("fetch_url", {}) if len(calls) == 1 else _text_response("final research")

    agent = _agent(tools)
    agent._llm_caller = llm

    result = await asyncio.wait_for(
        agent.chat_stream("research: fetch the official documentation"), timeout=1
    )

    assert result["status"] == "success"
    assert result["final_answer"] == "final research"
    assert len(calls) == 2
    assert "[Tool fetch_url SUCCESS]" in calls[1][-1]["content"]


@pytest.mark.asyncio
async def test_minimal_recovery_task_replans_after_failure_then_finalizes() -> None:
    tools = _Tools(
        {"check_once": "not reached", "corrected_check": "corrected result"},
        failures={"check_once"},
    )
    events: list[dict] = []
    calls: list[list[dict]] = []

    async def llm(messages, **_kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return _tool_response("check_once", {})
        if len(calls) == 2:
            return _tool_response("corrected_check", {})
        return _text_response("recovery final")

    agent = _agent(tools, events.append)
    agent._llm_caller = llm

    result = await asyncio.wait_for(
        agent.chat_stream("recovery: diagnose, replan, and succeed"), timeout=1
    )

    assert result["status"] == "success"
    assert result["final_answer"] == "recovery final"
    assert tools.calls == ["check_once", "corrected_check"]
    assert len(calls) == 3
    assert any(event.get("type") == "master_replan" for event in events)
    assert "[Tool check_once FAILED]" in calls[1][-1]["content"]


@pytest.mark.asyncio
async def test_empty_post_tool_response_is_replanned_and_bounded() -> None:
    tools = _Tools({"probe": "observed"})
    events: list[dict] = []
    calls: list[list[dict]] = []

    async def llm(messages, **_kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return _tool_response("probe", {})
        if len(calls) == 2:
            return _text_response("")
        return _text_response("recovered final")

    agent = _agent(tools, events.append)
    agent._llm_caller = llm

    result = await asyncio.wait_for(agent.chat_stream("probe"), timeout=1)

    assert result["status"] == "success"
    assert result["final_answer"] == "recovered final"
    assert len(calls) == 3
    assert any(event.get("type") == "master_replan" for event in events)


@pytest.mark.asyncio
async def test_repeated_empty_post_tool_response_is_terminal_and_bounded() -> None:
    tools = _Tools({"probe": "observed"})
    events: list[dict] = []
    calls: list[list[dict]] = []

    async def llm(messages, **_kwargs):
        calls.append(list(messages))
        return _tool_response("probe", {}) if len(calls) == 1 else _text_response("")

    agent = _agent(tools, events.append, max_rounds=8)
    agent._llm_caller = llm

    result = await asyncio.wait_for(agent.chat_stream("probe"), timeout=1)

    assert result["status"] == "failed"
    assert result["rounds"] == 3
    assert len(calls) == 3
    assert any(event.get("reason") == "repeated_noop" for event in events)


@pytest.mark.asyncio
async def test_repeated_result_with_different_arguments_is_bounded() -> None:
    tools = _Tools({"read_probe": "same evidence"})
    events: list[dict] = []
    calls: list[list[dict]] = []

    async def llm(messages, **_kwargs):
        calls.append(list(messages))
        return _tool_response("read_probe", {"offset": len(calls)})

    agent = _agent(tools, events.append, max_rounds=8)
    agent._llm_caller = llm

    result = await asyncio.wait_for(agent.chat_stream("repeat evidence"), timeout=1)

    assert result["status"] == "failed"
    assert result["rounds"] == 3
    assert tools.calls == ["read_probe", "read_probe", "read_probe"]
    assert any(event.get("reason") == "repeated_noop" for event in events)


@pytest.mark.asyncio
async def test_provider_timeout_is_reported_at_the_loop_boundary() -> None:
    tools = _Tools({"probe": "observed"})
    events: list[dict] = []

    async def llm(_messages, **_kwargs):
        await asyncio.sleep(0.05)
        return _text_response("late")

    agent = _agent(tools, events.append)
    agent.llm_timeout_s = 0.01
    agent._llm_caller = llm

    result = await asyncio.wait_for(agent.chat_stream("timeout"), timeout=1)

    assert result["status"] == "failed"
    assert "timed out" in result["error"]
    assert any(event.get("reason") == "provider_timeout" for event in events)

"""Tests for the single-step MasterAgent/GoalRun boundary."""

from __future__ import annotations

import json

import pytest

from oservi.master_agent import MasterAgent


class _Tools:
    def get_all_schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_probe",
                    "description": "read-only probe",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def list_tools(self) -> list[str]:
        return ["read_probe"]

    def describe(self, name: str) -> str:
        return name

    def has(self, name: str) -> bool:
        return name == "read_probe"

    async def execute(self, name: str, kwargs: dict) -> str:
        raise AssertionError("semantic_step must not execute tools")


class _Skills:
    def get_all_schemas(self) -> list[dict]:
        return []

    def list_skills(self) -> list[str]:
        return []

    def describe(self, name: str) -> str:
        return name

    def reload_skills(self) -> dict[str, int]:
        return {"loaded": 0, "skipped": 0}


class _Memory:
    def inject_subconscious(self) -> str:
        return ""


class _Swarm:
    async def run_swarm(self, overarching_goal: str, sub_tasks: list[dict]) -> str:
        raise AssertionError("unexpected swarm execution")


class _Vault:
    async def execute_secure_tool(self, *args, **kwargs) -> str:
        raise AssertionError("unexpected vault execution")


def _tool_response() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "semantic-call-1",
                            "type": "function",
                            "function": {
                                "name": "read_probe",
                                "arguments": json.dumps({}),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {},
    }


@pytest.mark.asyncio
async def test_semantic_step_returns_data_without_physical_execution():
    calls: list[dict] = []

    async def llm(messages, **kwargs):
        calls.append({"messages": messages, "kwargs": kwargs})
        return _tool_response()

    agent = MasterAgent(
        llm,
        tools=_Tools(),
        skill_hub=_Skills(),
        memory=_Memory(),
        swarm=_Swarm(),
        vault=_Vault(),
    )

    decision = await agent.semantic_step("inspect safely", session_id="semantic-test")

    assert decision["kind"] == "action"
    assert decision["tool"] == "read_probe"
    assert decision["arguments"] == {}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_semantic_step_observes_result_for_next_model_decision():
    responses = iter(
        [
            _tool_response(),
            {
                "choices": [{"message": {"role": "assistant", "content": "candidate"}}],
                "usage": {},
            },
        ]
    )

    async def llm(messages, **kwargs):
        return next(responses)

    agent = MasterAgent(
        llm,
        tools=_Tools(),
        skill_hub=_Skills(),
        memory=_Memory(),
        swarm=_Swarm(),
        vault=_Vault(),
    )

    first = await agent.semantic_step("inspect safely", session_id="semantic-observe")
    await agent.observe_action_result(
        {"action_id": first["action_id"], "status": "failed", "attempted": True},
        session_id="semantic-observe",
    )
    second = await agent.semantic_step("replan after failure", session_id="semantic-observe")

    assert second["kind"] == "candidate"
    assert any(
        message.get("role") == "tool"
        and message.get("tool_call_id") == first["action_id"]
        for message in agent._histories["semantic-observe"]
    )

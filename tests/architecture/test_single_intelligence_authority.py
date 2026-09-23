"""Live wiring guards for the single user-facing MasterAgent authority."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _call_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def test_product_task_has_no_pre_master_llm_classifier() -> None:
    source = _source("server/coordinator_master.py")
    tree = ast.parse(source)
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert "_decide_capability" not in defined
    assert "CapabilityDecision" not in defined
    assert "system_classify_capability" not in source
    assert "product_task=True" not in source


def test_product_task_masteragent_has_canonical_tool_surface() -> None:
    from server.coordinator_master import MasterCoordinator

    async def fake_llm(_messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "done"}}], "usage": {}}

    coordinator = MasterCoordinator(llm_fn=fake_llm)
    raw = coordinator._raw_get_all_tool_schemas()
    assert coordinator.get_all_tool_schemas() == raw
    names = {item["function"]["name"] for item in raw}
    assert {"coding_task_run", "agent_loop_run"} <= names


def test_no_semantic_programmatic_tool_slicing() -> None:
    source = _source("server/coordinator_master.py")
    assert "_get_tools_for_capability" not in source
    assert "_CAPABILITY_TOOL_GROUPS" not in source
    assert "_CAPABILITY_DECISION_TOOL" not in source


@pytest.mark.asyncio
async def test_chat_route_uses_master_coordinator(monkeypatch: pytest.MonkeyPatch) -> None:
    from server import chat_coordinator

    calls: list[dict[str, Any]] = []

    async def fake_chat_stream(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, "kwargs": kwargs})
        return {"status": "success", "final_answer": "ok", "cost_usd": 0.25, "rounds": 1}

    from server import coordinator_master

    monkeypatch.setattr(coordinator_master.master_coordinator, "chat_stream", fake_chat_stream)
    result = await chat_coordinator.chat("hello", session_id="chat-authority")

    assert result["content"] == "ok"
    assert calls and calls[0]["kwargs"]["session_id"] == "chat-authority"
    assert calls[0]["kwargs"]["system_context"] == chat_coordinator.CHAT_SYSTEM_PROMPT


def test_chat_coordinator_has_no_direct_llm_authority() -> None:
    source = _source("server/chat_coordinator.py")
    tree = ast.parse(source)
    assert "llm_call" not in source
    assert "chat_stream" in _call_names(tree)
    assert "master_coordinator" in source


def test_only_one_user_facing_intelligence_authority() -> None:
    chat_route = _source("server/routes/chat.py")
    chat_adapter = _source("server/chat_coordinator.py")
    product_route = _source("server/routes/product.py")
    agent_route = _source("server/routes/legacy_agent.py")
    voice_route = _source("server/routes/voice_compat.py")
    cindy_route = _source("server/routes/cindy_compat.py")

    assert "llm_call" not in chat_route
    assert "llm_call" not in chat_adapter
    assert "master_coordinator" in chat_adapter
    assert "master_coordinator" in product_route
    assert "master_coordinator" in agent_route
    assert "master_coordinator" in voice_route
    assert "llm_call" not in cindy_route
    assert "ChatOpenAI" not in cindy_route
    assert "from browser_use" not in cindy_route
    assert "run_engine" not in agent_route
    assert "stream_engine" not in agent_route
    assert "VEYA_AGENT_LOOP" not in agent_route

    forbidden_route_wiring = re.compile(
        r"from veya\.llm import[^\n]*\bllm_call\b|"
        r"from veya\.oprim\.llm import[^\n]*\bllm_call\b|"
        r"from langchain_openai import|from browser_use import|"
        r"\bllm_call\s*\(|\b(?:run_engine|stream_engine)\s*\("
    )
    for route_path in (ROOT / "server" / "routes").glob("*.py"):
        assert not forbidden_route_wiring.search(route_path.read_text(encoding="utf-8")), (
            f"user-facing route contains an independent intelligence path: {route_path}"
        )

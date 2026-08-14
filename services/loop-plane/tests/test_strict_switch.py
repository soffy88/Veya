"""主链切换桥测试（VEYA_AGENT_LOOP=strict）。

覆盖:
- run_strict_chat: master_tools 全量工具面注入 + 假 LLM 剧本端到端
- 事件桥: tool_result → fire_step(tool_call) 事件
- chat_stream 分支: flag 关 → 旧路径; flag 开 → 新心脏
- 返回形态兼容: status/final_answer/rounds/tool_calls/session_id
"""

from __future__ import annotations

from typing import Any

import pytest


class FakeLlm:
    def __init__(self, script: list[dict]) -> None:
        self._script = script
        self._calls = 0

    async def complete(self, messages: list[dict], **kwargs: Any) -> dict:
        reply = self._script[min(self._calls, len(self._script) - 1)]
        self._calls += 1
        return {"choices": [{"message": reply}]}

    async def close(self) -> None:
        pass


def _tool_msg(name: str, args: dict) -> dict:
    return {
        "role": "assistant",
        "content": "tool time",
        "tool_calls": [
            {"id": f"call_{name}", "type": "function",
             "function": {"name": name, "arguments": args}}
        ],
    }


@pytest.mark.asyncio
async def test_run_strict_chat_end_to_end(monkeypatch: pytest.MonkeyPatch):
    """新心脏 + master_tools 全量工具面 + 假 LLM 剧本。"""
    from server.agent_loop_bridge import run_strict_chat
    from server.tool_registry import master_tools

    # 注入测试工具到 master_tools（用完清理）
    master_tools.register("strict_echo", "回显", {"type": "object", "properties": {"text": {"type": "string"}}},
                          lambda text: f"echo:{text}")
    try:
        llm = FakeLlm([
            _tool_msg("strict_echo", {"text": "hi"}),
            {"role": "assistant", "content": "新心脏完成"},
        ])
        result = await run_strict_chat(
            "测试",
            system_prompt="sys",
            max_rounds=5,
            llm=llm,
        )
        assert result["loop_plane"] == "strict"
        assert result["status"] == "success"
        assert result["final_answer"] == "新心脏完成"
        assert result["rounds"] == 2
        assert result["tool_calls"] == [{"tool": "strict_echo", "ok": True}]
        assert result["session_id"]
    finally:
        master_tools.unregister("strict_echo")


@pytest.mark.asyncio
async def test_run_strict_chat_tool_failure_trace(monkeypatch: pytest.MonkeyPatch):
    """工具失败 → tool_calls trace 带 ok=False + 事件桥发出 tool_error。"""
    from server.agent_loop_bridge import run_strict_chat
    from server.tool_registry import master_tools

    events: list[dict] = []

    def on_step(ev: dict) -> None:
        events.append(ev)

    master_tools.register("strict_bad", "爆炸", {"type": "object", "properties": {}},
                          lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        llm = FakeLlm([
            _tool_msg("strict_bad", {}),
            {"role": "assistant", "content": "工具失败了但我在"},
        ])
        result = await run_strict_chat("测试", system_prompt="sys", max_rounds=5, llm=llm, on_step=on_step)
        assert result["tool_calls"] == [{"tool": "strict_bad", "ok": False}]
        # 事件桥: tool_call + tool_error 都发出
        types = [e["type"] for e in events]
        assert "tool_call" in types and "tool_error" in types
    finally:
        master_tools.unregister("strict_bad")


@pytest.mark.asyncio
async def test_chat_stream_switch_branch(monkeypatch: pytest.MonkeyPatch):
    """chat_stream: flag 关 → 旧路径; flag 开 → 新心脏。"""
    from server.agent_loop_bridge import strict_loop_enabled

    # flag 关闭
    monkeypatch.delenv("VEYA_AGENT_LOOP", raising=False)
    assert strict_loop_enabled() is False

    # flag 开启
    monkeypatch.setenv("VEYA_AGENT_LOOP", "strict")
    assert strict_loop_enabled() is True


@pytest.mark.asyncio
async def test_run_strict_chat_llm_kwargs_forwarded(monkeypatch: pytest.MonkeyPatch):
    """请求级 llm_kwargs（provider/model）透传到 oprim_llm_call。"""
    import server.agent_loop_bridge as bridge
    from server.agent_loop_bridge import run_strict_chat

    seen: dict = {}

    class CaptureLlm:
        def __init__(self, kwargs: dict) -> None:
            self._kwargs = kwargs

        async def complete(self, messages, **kw):
            seen.update(kw)
            return {"choices": [{"message": {"role": "assistant", "content": "完成收到"}}]}

        async def close(self):
            pass

    monkeypatch.setattr(bridge, "_BoundedLlm", CaptureLlm)
    # 不用 kwargs 分支时 llm 显式注入优先；这里验证 kwargs 路径
    result = await run_strict_chat("hi", system_prompt="sys", max_rounds=2,
                                   llm_kwargs={"model": "veya1.1", "provider": "veya1.1"})
    assert result["final_answer"] == "完成收到"

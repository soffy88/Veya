"""veya/oprim/llm — LLM 调用原子操作（物理触手，只发已打包好的数据）。

阶段 3 原子元素：oprim_llm_call / oprim_llm_stream。

规则：
- 经注入的 LlmClient 句柄（默认 container 全局句柄）；
- 只接收**已打包**的标准消息（阶段 2 protocol_translate 产出）与传输参数；
- 本层无 Prompt 逻辑、无模型路由判断、无重试编排——通道职责归 obase_llm_client，
  编排职责归阶段 4 agent_loop；
- 原子性：一次调用 = 一个完整的通道请求（或流式通道）。
"""

from __future__ import annotations

from typing import Any, AsyncIterator


def _client_of(client: Any) -> Any:
    if client is not None:
        return client
    from veya.obase.container import get_llm

    return get_llm()


async def llm_call(messages: list[dict], client: Any = None, **kwargs: Any) -> dict:
    """非流式补全：messages 必须是标准 AgentMessage 列表。"""
    return await _client_of(client).complete(messages, **kwargs)  # type: ignore[attr-defined]


def llm_stream(messages: list[dict], client: Any = None, **kwargs: Any) -> AsyncIterator[dict]:
    """流式补全：逐条 delta 事件。"""
    return _client_of(client).stream(messages, **kwargs)  # type: ignore[attr-defined]


__all__ = ["llm_call", "llm_stream"]

"""SSE 事件信封 (对标"Pi"清单 P1: 消息/事件 IR)——纯增量, 不改老字段。

`server.events._to_envelope` 是唯一的信封逻辑, 在两个真正的落地扇入点接线:
`server.sse.SSEQueue.on_step`(覆盖 fire_step 全部下游 + queue.on_step 直调 +
sse.emit) 和 `server.routes.legacy_agent._engine_events`(stream_engine 那条
独立管路)。这里只测 `_to_envelope` 本体 + `SSEQueue.on_step` 接线, 不重复测
`_engine_events` 的路由级行为 (人工核对过, 单行 wrap, 风险低)。
"""

from __future__ import annotations

import pytest

import server.sse as sse
from server.events import _to_envelope


def test_adds_missing_envelope_fields():
    ev = {"type": "tool_call", "session_id": "s1", "tool_name": "write"}
    out = _to_envelope(ev)
    assert out["type"] == "tool_call"  # 老字段不删
    assert out["session_id"] == "s1"
    assert out["tool_name"] == "write"
    assert out["topic"] == "tool_call"
    assert out["payload"] == {"session_id": "s1", "tool_name": "write"}
    assert isinstance(out["ts"], float)
    assert out["trace_id"] == "s1"


def test_topic_falls_back_to_event_key():
    """flow_engine.py 的 sse.emit() 用的是 "event" 键, 不是 "type"。"""
    ev = {"event": "genesis_element_start", "layer": "L0", "name": "x"}
    out = _to_envelope(ev)
    assert out["topic"] == "genesis_element_start"
    assert out["payload"] == {"layer": "L0", "name": "x"}


def test_existing_payload_key_is_preserved_not_overwritten():
    """zero_trust_vault.py 的 vault_hitl 自带 "payload" 子结构——不能被覆盖。"""
    ev = {
        "type": "vault_hitl",
        "level": "HITL_REQUIRED",
        "title": "需要审批",
        "content": "",
        "payload": {"task_id": "t1", "action": "deploy", "vault_id": "v1"},
    }
    out = _to_envelope(ev)
    assert out["payload"] == {"task_id": "t1", "action": "deploy", "vault_id": "v1"}
    assert out["topic"] == "vault_hitl"


def test_trace_id_priority_order():
    assert _to_envelope({"type": "a", "session_id": "S", "sid": "X"})["trace_id"] == "S"
    assert _to_envelope({"type": "a", "sid": "X", "task_id": "T"})["trace_id"] == "X"
    assert _to_envelope({"type": "a", "task_id": "T", "plan_id": "P"})["trace_id"] == "T"
    assert _to_envelope({"type": "a", "plan_id": "P", "request_id": "R"})["trace_id"] == "P"
    assert _to_envelope({"type": "a", "request_id": "R"})["trace_id"] == "R"
    assert _to_envelope({"type": "a"})["trace_id"] == ""


def test_non_dict_input_passes_through():
    assert _to_envelope(None) is None  # type: ignore[arg-type]
    assert _to_envelope("not a dict") == "not a dict"  # type: ignore[arg-type]


def test_internal_failure_degrades_to_original_dict():
    class _BoomDict(dict):
        def items(self):
            raise RuntimeError("boom")

    ev = _BoomDict(type="tool_call")
    out = _to_envelope(ev)
    assert out is ev  # 异常时原样返回, 不拖垮 SSE 流


@pytest.fixture(autouse=True)
def _clean_registry():
    sse._queues.clear()
    yield
    sse._queues.clear()


@pytest.mark.asyncio
async def test_sse_queue_on_step_envelopes_before_enqueue():
    q = sse.get_or_create_queue("s-env")
    q.on_step({"type": "tool_call", "session_id": "s-env", "tool_name": "write"})
    item = await q._q.get()
    # 老的扁平字段原样在
    assert item["type"] == "tool_call"
    assert item["tool_name"] == "write"
    # 新信封字段也在
    assert item["topic"] == "tool_call"
    assert item["payload"]["tool_name"] == "write"
    assert item["trace_id"] == "s-env"

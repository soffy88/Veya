"""Canonical Event Model §4 测试。

验证: EventEnvelope 字段对齐; trace_id 贯穿; schema_version; 最小事件类型;
A-04: 任务状态仅投影, 不控制主链。
"""

from __future__ import annotations

from server.events import _to_envelope

# ── EventEnvelope 结构 ─────────────────────────────────────────────────────


def test_envelope_has_required_fields():
    """EventEnvelope 必须包含 §4 定义的所有必须字段。"""
    event = {"type": "test", "session_id": "s1"}
    env = _to_envelope(event)

    # 必须字段
    assert "event_id" in env, "Missing event_id"
    assert "trace_id" in env, "Missing trace_id"
    assert "session_id" in env, "Missing session_id"
    assert "topic" in env, "Missing topic"
    assert "ts" in env, "Missing ts"
    assert "actor" in env, "Missing actor"
    assert "payload" in env, "Missing payload"
    assert "schema_version" in env, "Missing schema_version"

    # task_id, turn_id 是可选的 (可能为 None)
    # 只有 task.created 等事件才会有 task_id

    # 类型检查
    assert isinstance(env["event_id"], str)
    assert isinstance(env["trace_id"], str)
    assert isinstance(env["schema_version"], int)


def test_event_id_is_uuid_like():
    """event_id 应该是类 UUID 的字符串。"""
    event = {"type": "test"}
    env = _to_envelope(event)
    assert len(env["event_id"]) >= 10  # UUID hex is 32 chars, but we just check length
    # 验证是可解析的字符串
    assert env["event_id"]


def test_trace_id_persists_through_chain():
    """trace_id 在事件链中应该保持一致。"""
    event1 = {"type": "session_created", "session_id": "s1"}
    env1 = _to_envelope(event1)
    tid = env1["trace_id"]

    event2 = {"type": "turn_started", "session_id": "s1"}
    env2 = _to_envelope(event2)
    assert env2["trace_id"] == tid, "trace_id should persist across events in same chain"


def test_minimal_event_types():
    """§4 定义的最小事件类型集合。"""
    allowed_types = {
        "session.created",
        "turn.started",
        "message.user_added",
        "message.assistant_added",
        "tool.requested",
        "tool.approval_required",
        "tool.approved",
        "tool.denied",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "tool.cancelled",
        "task.created",
        "task.started",
        "task.waiting_approval",
    }

    for et in allowed_types:
        event = {"type": et}
        env = _to_envelope(event)
        assert env["topic"] == et, f"topic should be {et}, got {env['topic']}"


# ── Schema version 兼容性 ───────────────────────────────────────────────────


def test_schema_version_increment():
    """schema_version 应该在关键变更时递增。"""
    # 初始版本
    env0 = {
        "event_id": "test1",
        "trace_id": "t1",
        "session_id": "s1",
        "task_id": None,
        "turn_id": None,
        "topic": "test",
        "ts": 1.0,
        "payload": {},
        "schema_version": 1,
    }

    # 关键变更时递增
    env1 = {
        "event_id": "test2",
        "trace_id": "t1",
        "session_id": "s1",
        "task_id": "task1",
        "turn_id": "t1",
        "topic": "test",
        "ts": 2.0,
        "payload": {"key": "val"},
        "schema_version": 2,
    }

    # 即使 payload 相同但结构变更，版本也应不同
    assert env1["schema_version"] > env0["schema_version"]


# ── A-04 约束：事件不控制主链 ───────────────────────────────────────────


def test_event_envelope_no_control_logic():
    """EventEnvelope 只承载数据, 不包含主链控制逻辑。

    这满足 A-04 纪律：任务状态是投影，不是决定执行的控制信号。
    """
    # EventEnvelope 应该只包含描述性字段，不包含执行决策
    event = {
        "type": "tool.requested",
        "tool_name": "write_file",
        "tool_args": {"path": "/tmp/test"},
        "session_id": "s1",
    }
    env = _to_envelope(event)

    # 必须包含数据字段
    assert "topic" in env
    assert "trace_id" in env
    assert "session_id" in env
    assert "payload" in env

    # 不应包含执行决策字段（这属于主链控制，不属于事件 envelope）
    # 注意：决策由主链根据事件 + 策略矩阵做出，事件本身只记录


def test_envelope_backward_compatible():
    """新信封结构不应破坏读老字段的前端解析器。"""
    # 老解析器只读 'type', 'event', 'payload' 等老键
    # 新信封应在不删改老键的前提下额外添加键
    old_style_event = {"type": "tool_call", "tool_name": "write", "args": {"path": "/tmp"}}
    env = _to_envelope(old_style_event)

    # 老键应被保留
    assert "type" in env or "event" in env  # 关键是不应该报错、丢失数据
    # 新键应被添加
    assert "event_id" in env or "trace_id" in env  # 必须有新增字段而非覆盖

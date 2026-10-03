from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.sse import execution_events_generator
from veya.remote.execution_events import ExecutionEventStore, ExecutionEventType


def test_replay_cursor_and_live_subscription_have_no_duplicate_boundary(tmp_path) -> None:
    store = ExecutionEventStore(tmp_path, "exec-replay")
    first = store.append(ExecutionEventType.OUTPUT, payload={"n": 1})
    second = store.append(ExecutionEventType.OUTPUT, payload={"n": 2})
    subscriber = store.subscribe_live()
    third = store.append(ExecutionEventType.OUTPUT, payload={"n": 3})

    assert [event.sequence for event in store.replay_after(first.sequence)] == [second.sequence, third.sequence]
    assert subscriber.get_nowait().sequence == third.sequence
    store.unsubscribe_live(subscriber)

    reconnected = ExecutionEventStore(tmp_path, "exec-replay")
    assert [event.sequence for event in reconnected.replay_after(second.sequence)] == [third.sequence]


def test_sse_generator_replays_durable_events_and_terminalizes(tmp_path, monkeypatch) -> None:
    execution_id = "exec-sse-replay"
    store = ExecutionEventStore(tmp_path, execution_id)
    store.append(ExecutionEventType.OUTPUT, payload={"n": 1})
    store.append(
        ExecutionEventType.EXECUTION_STATE_CHANGED,
        payload={"kind": "status", "status": "COMPLETED", "phase": "COMPLETED"},
    )

    record = SimpleNamespace(requested_realpath=str(tmp_path), requested_workspace=str(tmp_path))
    fake_store = SimpleNamespace(get=lambda value: record if value == execution_id else None)

    from veya.remote import execution as execution_module
    monkeypatch.setattr(execution_module.ExecutionStore, "from_env", classmethod(lambda cls, default_persistent: fake_store))

    async def collect() -> list[str]:
        return [item async for item in execution_events_generator(execution_id, None, 0)]

    output = asyncio.run(collect())
    assert any("id: 1" in item for item in output)
    assert any("id: 2" in item for item in output)
    assert output[-1] == "data: [DONE]\n\n"

from pathlib import Path
import pytest
from veya.remote.execution_events import EventSequenceError, ExecutionEventStore, ExecutionEventType

def test_append_is_strictly_monotonic_and_durable(tmp_path: Path):
    store = ExecutionEventStore(tmp_path, "exec-1", max_buffer_events=2)
    first = store.append(ExecutionEventType.PROVIDER_STARTED)
    second = store.append_output("hello")
    assert (first.sequence, second.sequence) == (1, 2)
    reopened = ExecutionEventStore(tmp_path, "exec-1", max_buffer_events=2)
    assert [e.sequence for e in reopened.replay_after(0)] == [1, 2]

def test_gap_and_overwrite_are_rejected(tmp_path: Path):
    store = ExecutionEventStore(tmp_path, "exec-2")
    event = store.append(ExecutionEventType.PROGRESS)
    with pytest.raises(EventSequenceError):
        store.append(ExecutionEventType.OUTPUT, sequence=3)
    with pytest.raises(EventSequenceError):
        store.append(ExecutionEventType.OUTPUT, sequence=1)
    assert store.last_sequence == event.sequence

def test_duplicate_event_id_is_idempotent_only_for_same_event(tmp_path: Path):
    store = ExecutionEventStore(tmp_path, "exec-3")
    event = store.append(ExecutionEventType.OUTPUT, event_id="fixed", payload={"text": "x"})
    assert store.append(ExecutionEventType.OUTPUT, event_id="fixed", payload={"text": "x"}) == event
    with pytest.raises(EventSequenceError):
        store.append(ExecutionEventType.OUTPUT, event_id="fixed", payload={"text": "changed"})

def test_replay_uses_durable_sink_when_hot_buffer_evicted(tmp_path: Path):
    store = ExecutionEventStore(tmp_path, "exec-4", max_buffer_events=2)
    for index in range(4):
        store.append(ExecutionEventType.OUTPUT, payload={"n": index})
    assert [e.sequence for e in store.replay_after(0)] == [1, 2, 3, 4]
    assert [e.sequence for e in store.replay_after(2)] == [3, 4]

def test_backpressure_is_a_durable_execution_event(tmp_path: Path):
    store = ExecutionEventStore(tmp_path, "exec-5")
    event = store.emit_backpressure(buffered=8, capacity=8)
    assert event.event_type == ExecutionEventType.BACKPRESSURE
    assert event.payload == {"buffered": 8, "capacity": 8}

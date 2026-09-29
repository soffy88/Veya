"""P0_01 lifecycle event bus: authority -> durable journal -> SSE projection."""

from __future__ import annotations

import json

import pytest

from server.events import _to_envelope
from server.lifecycle_events import (
    REPLAYABLE_CLASSES,
    SCHEMA_VERSION,
    TAXONOMY,
    DurabilityClass,
    LifecycleEventBus,
    build_event,
    build_session_projection,
    coerce_stored_envelope,
    project_event_to_sse,
    to_journal_envelope,
    validate_lifecycle_event,
)


@pytest.fixture
def bus(tmp_path):
    return LifecycleEventBus(journal_path=tmp_path / "lifecycle.jsonl")


def _chain(bus: LifecycleEventBus, session_id: str = "sess_chain"):
    root = bus.emit("run.created", session_id=session_id, run_id="run_1")
    child = bus.emit_child(root, "task.started", task_id="task_1")
    recovery = bus.emit_child(child, "recovery.completed", payload={"strategy": "retry"})
    return root, child, recovery


# ── schema validation ────────────────────────────────────────────────────


def test_valid_event_passes_validation(bus):
    event = bus.emit("run.started", session_id="s1", run_id="r1")
    assert event.schema_version == SCHEMA_VERSION
    assert event.durability_class == DurabilityClass.DURABLE


def test_unknown_event_type_rejected(bus):
    with pytest.raises(ValueError, match="unknown lifecycle event_type"):
        bus.emit("nope.unknown", session_id="s1")


def test_durability_mismatch_rejected():
    event = build_event("run.started", session_id="s1")
    tampered = LifecycleEventBus.__new__(LifecycleEventBus)
    bad = event.__class__(**{**event.__dict__, "durability_class": DurabilityClass.EPHEMERAL})
    with pytest.raises(ValueError, match="durability mismatch"):
        validate_lifecycle_event(bad)
    assert tampered is not None


def test_non_serializable_payload_rejected(bus):
    with pytest.raises(ValueError, match="JSON-serializable"):
        bus.emit("run.started", session_id="s1", payload={"bad": object()})


def test_empty_session_rejected(bus):
    with pytest.raises(ValueError, match="session_id"):
        bus.emit("run.started", session_id="")


def test_taxonomy_covers_all_required_families():
    families = {
        "run": False,
        "task": False,
        "execution": False,
        "approval": False,
        "workspace": False,
        "verification": False,
        "recovery": False,
        "reconciliation": False,
    }
    for event_type in TAXONOMY:
        kind = event_type.split(".")[0]
        if kind in families:
            families[kind] = True
    assert all(families.values()), families
    assert TAXONOMY["policy.decision"] is DurabilityClass.DURABLE
    assert TAXONOMY["completion.recorded"] is DurabilityClass.DURABLE


# ── causation / correlation ──────────────────────────────────────────────


def test_root_event_self_correlates(bus):
    root = bus.emit("run.created", session_id="s1")
    assert root.correlation_id == root.event_id
    assert root.causation_id is None


def test_parent_child_recovery_chain(bus):
    root, child, recovery = _chain(bus)
    assert child.causation_id == root.event_id
    assert child.correlation_id == root.correlation_id
    assert recovery.causation_id == child.event_id
    assert recovery.correlation_id == root.correlation_id


# ── durability classes & persistence ─────────────────────────────────────


def test_durable_event_persisted_to_journal(bus):
    event = bus.emit("task.completed", session_id="s1", task_id="t1")
    lines = (bus.journal_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored["event_id"] == event.event_id
    assert stored["topic"] == "task.completed"
    assert stored["payload"]["durability_class"] == "durable"


def test_replayable_event_persisted_and_replayed(bus):
    event = bus.emit("execution.step_completed", session_id="s1")
    assert event.durability_class == DurabilityClass.REPLAYABLE
    replayed = bus.replay(session_id="s1")
    assert [e.event_id for e in replayed] == [event.event_id]


def test_ephemeral_event_never_persisted(bus):
    event = bus.emit("execution.progress", session_id="s1", payload={"pct": 50})
    assert event.durability_class == DurabilityClass.EPHEMERAL
    assert not bus.journal_path.exists() or bus.journal_path.read_text().strip() == ""
    assert bus.replay(session_id="s1") == []


def test_envelope_preserves_event_id_for_dedup():
    event = build_event("run.started", session_id="s1", event_id="fixed-id")
    envelope = to_journal_envelope(event)
    assert envelope["event_id"] == "fixed-id"


# ── replay & projection ──────────────────────────────────────────────────


def test_replay_rebuilds_session_projection(bus):
    root, child, recovery = _chain(bus)
    bus.emit("execution.progress", session_id="sess_chain")  # ephemeral: skipped
    projection = bus.rebuild_projection(session_id="sess_chain")
    assert projection["runs"]["run_1"]["status"] == "created"
    assert projection["tasks"]["task_1"]["status"] == "started"
    assert projection["recoveries"][0]["causation_id"] == child.event_id
    assert projection["recoveries"][0]["correlation_id"] == root.correlation_id
    assert projection["replayed"] == 3
    assert recovery.event_id in [r["event_id"] for r in projection["recoveries"]]


def test_replay_from_sse_history_forbidden(bus):
    event = bus.emit("run.started", session_id="s1")
    with pytest.raises(RuntimeError, match="forbidden"):
        bus.replay_from_sse_history([project_event_to_sse(event)])


# ── duplicate / idempotency ──────────────────────────────────────────────


def test_duplicate_emit_is_idempotent(bus):
    first = bus.emit("run.started", session_id="s1", event_id="dup-1")
    second = bus.emit("run.started", session_id="s1", event_id="dup-1")
    assert second is first
    lines = bus.journal_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_restart_dedup_via_journal(bus, tmp_path):
    bus.emit("run.started", session_id="s1", event_id="restart-dup")
    bus2 = LifecycleEventBus(journal_path=tmp_path / "lifecycle.jsonl")
    bus2.emit("run.started", session_id="s1", event_id="restart-dup")
    lines = bus.journal_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


# ── SSE projection ───────────────────────────────────────────────────────


def test_sse_frame_projects_all_identity_fields(bus):
    root, child, _ = _chain(bus)
    frame = project_event_to_sse(child)
    assert frame.startswith(f"id: {child.event_id}\n")
    assert f"event: {child.event_type}\n" in frame
    body = json.loads(frame.split("data: ", 1)[1])
    assert body["causation_id"] == root.event_id
    assert body["correlation_id"] == root.correlation_id
    assert body["durability_class"] == "durable"


def test_sse_module_projection_matches_authority(bus):
    from server.sse import sse_frame_for_lifecycle

    event = bus.emit("verification.passed", session_id="s1")
    assert sse_frame_for_lifecycle(event) == project_event_to_sse(event)


# ── restart / recovery ───────────────────────────────────────────────────


def test_restart_replays_same_projection(bus, tmp_path):
    _chain(bus, session_id="sess_restart")
    before = bus.rebuild_projection(session_id="sess_restart")
    bus2 = LifecycleEventBus(journal_path=tmp_path / "lifecycle.jsonl")
    after = bus2.rebuild_projection(session_id="sess_restart")
    assert after == before


# ── backward compatibility ───────────────────────────────────────────────


def test_legacy_envelope_coerces_to_replayable():
    legacy = _to_envelope({"type": "tool_call", "session_id": "s-old", "tool_name": "write"})
    event = coerce_stored_envelope(legacy)
    assert event.session_id == "s-old"
    assert event.durability_class == DurabilityClass.REPLAYABLE
    assert event.correlation_id


def test_emit_legacy_unknown_topic_coerces(bus):
    event = bus.emit_legacy("tool_call", session_id="s-old", payload={"tool_name": "w"})
    assert event.durability_class == DurabilityClass.REPLAYABLE


def test_emit_legacy_known_topic_uses_taxonomy(bus):
    event = bus.emit_legacy("task.created", session_id="s1", task_id="t1")
    assert event.event_type == "task.created"
    assert event.durability_class is DurabilityClass.DURABLE


def test_old_envelope_keys_preserved():
    out = _to_envelope({"type": "tool_call", "session_id": "s1", "tool_name": "write"})
    assert out["type"] == "tool_call"
    assert out["tool_name"] == "write"
    assert out["topic"] == "tool_call"


def test_replayable_classes_constant():
    assert frozenset({DurabilityClass.DURABLE, DurabilityClass.REPLAYABLE}) == REPLAYABLE_CLASSES
    assert DurabilityClass.EPHEMERAL not in REPLAYABLE_CLASSES
    assert build_session_projection([])["replayed"] == 0


# ── session journal projection ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_journal_projects_lifecycle_event():
    from server.session_events import (
        durable_session_store,
        lifecycle_to_session_payload,
    )

    event = build_event("task.completed", session_id="sess_proj_a", task_id="t_a")
    payload = lifecycle_to_session_payload(event)
    assert payload["lifecycle"] is True
    assert payload["event_type"] == "task.completed"
    stored = await durable_session_store.persist_lifecycle_event("sess_proj_a", event)
    assert stored["event"] == "task.completed"
    replayed = await durable_session_store.replay_lifecycle_events(
        "sess_proj_a", stored["epoch"], stored["seq"] - 1
    )
    assert replayed[-1].event_id == event.event_id
    assert replayed[-1].correlation_id == event.correlation_id


@pytest.mark.asyncio
async def test_emit_lifecycle_wires_bus_journal_and_session(tmp_path):
    import asyncio

    from server.session_events import durable_session_store
    from server.sse import emit_lifecycle, sse_frame_for_lifecycle

    session_id = "sess_wire_b"
    bus = LifecycleEventBus(journal_path=tmp_path / "wire.jsonl")
    root = emit_lifecycle(session_id, "run.created", {"goal": "demo"}, bus=bus, run_id="run_w")
    child = bus.emit_child(root, "recovery.completed", payload={"strategy": "retry"})
    await asyncio.sleep(1.0)  # let the session-journal projection task land
    frame = sse_frame_for_lifecycle(root)
    assert "event: run.created" in frame
    head = await durable_session_store.get_stream_head(session_id)
    rows = await durable_session_store.replay_lifecycle_events(session_id, head[0], 0)
    assert root.event_id in [e.event_id for e in rows]
    assert child.correlation_id == root.correlation_id

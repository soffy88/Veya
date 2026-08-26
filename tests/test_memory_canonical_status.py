"""Canonical memory lifecycle serialization with legacy storage compatibility."""

from __future__ import annotations

from server.memory_controller import MemoryController, _MemoryStore


def test_memory_canonical_status_keeps_legacy_storage_alias(tmp_path):
    controller = MemoryController(_MemoryStore(tmp_path / "memory.json"))
    record = controller.observe("fact")

    assert record.status == "candidate"
    assert record.canonical_status == "active"
    payload = controller._store.get(record.memory_id).canonical_dict()
    assert payload["status"] == "active"
    assert payload["legacy_status"] == "candidate"


def test_memory_correction_serializes_superseded_and_active(tmp_path):
    controller = MemoryController(_MemoryStore(tmp_path / "memory.json"))
    old = controller.observe("old fact")
    new_id = controller.correct_record(old.memory_id, content="new fact")

    assert new_id is not None
    assert controller.get(old.memory_id).canonical_status == "superseded"
    assert controller.get(new_id).canonical_status == "active"

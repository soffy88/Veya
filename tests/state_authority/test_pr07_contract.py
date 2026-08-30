from __future__ import annotations

import pytest

from runtime.state_authority.doctor import StateDoctorStatus, diagnose
from runtime.state_authority.memory_refs import MemoryRef, resolve_memory_ref
from runtime.state_authority.migration import dry_run_audit
from runtime.state_authority.models import MemoryView
from runtime.state_authority.ownership import (
    StateNamespace,
    assert_session_id,
    assert_writer,
    declared_ownership,
)
from server.coordinator_master import MasterCoordinator
from server.memory_bank import VeyaMemoryBank
from server.memory_controller import MemoryController, _MemoryStore
from veya.obase.adapters import SqliteKvStore
from veya.omodul.session_tree import SessionTreeMgr
from veya.oprim.snapshot import snapshot_delete


def test_sa04_namespace_owners_are_unique():
    owners = declared_ownership()
    assert {x.namespace for x in owners} == set(StateNamespace)
    assert len(owners) == len({x.namespace for x in owners})


def test_sa05_wrong_writer_is_rejected():
    with pytest.raises(AssertionError):
        assert_writer(StateNamespace.CONVERSATION, "GoalRunProjection")


def test_sa06_session_ids_cannot_cross_namespaces():
    assert_session_id(StateNamespace.CONVERSATION, "chat-1")
    assert_session_id(StateNamespace.EXECUTION, "goalrun-task-1")
    with pytest.raises(AssertionError):
        assert_session_id(StateNamespace.CONVERSATION, "goalrun-task-1")
    with pytest.raises(AssertionError):
        assert_session_id(StateNamespace.EXECUTION, "chat-1")


def test_sa10_memory_refs_are_typed_and_minimal():
    assert MemoryRef("semantic", "memory-1", version=2).domain == "semantic"
    assert MemoryRef("preference", "pref-1").domain == "preference"
    with pytest.raises(ValueError):
        MemoryRef("other", "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MemoryRef("semantic", "")


def test_sa12_memory_view_contains_refs_not_payloads():
    view = MemoryView(semantic=[MemoryRef("semantic", "m1")], preferences=[])
    assert view.all_refs() == [MemoryRef("semantic", "m1")]
    assert not hasattr(view, "content")


def test_sa10_semantic_ref_resolves_current_record(tmp_path, monkeypatch):
    controller = MemoryController(_MemoryStore(tmp_path / "semantic.json"))
    record = controller.observe("winner", scope="project")
    monkeypatch.setattr("server.memory_controller.memory_controller", controller)
    assert resolve_memory_ref(MemoryRef("semantic", record.memory_id))["content"] == "winner"


def test_sa11_preference_ref_resolves_from_memory_bank(tmp_path, monkeypatch):
    bank = VeyaMemoryBank(tmp_path / "preferences.json")
    bank.add_preference("use concise answers")
    pref = bank.list_preferences()[0]
    monkeypatch.setattr("server.memory_bank.memory_bank", bank)
    assert resolve_memory_ref(MemoryRef("preference", pref["id"]))["rule"] == "use concise answers"


def test_sa13_memory_correction_resolves_latest_winner(tmp_path, monkeypatch):
    controller = MemoryController(_MemoryStore(tmp_path / "semantic.json"))
    old = controller.observe("old", scope="project")
    replacement_id = controller.correct_record(old.memory_id, content="newer")
    monkeypatch.setattr("server.memory_controller.memory_controller", controller)
    assert replacement_id is not None
    assert resolve_memory_ref(MemoryRef("semantic", replacement_id))["content"] == "newer"


def test_sa14_memory_hub_is_non_authoritative():
    report = diagnose()["memory"]
    assert report["distillation_pipeline"] == "VeyaMemoryHub"
    assert report["distillation_role"] == "distillation/retrieval adapter"
    assert report["durable_semantic_authority"] is False


def test_sa15_state_doctor_ready():
    report = diagnose()
    assert report["status"] == StateDoctorStatus.STATE_READY
    assert report["session"]["authority_cycles"] == 0
    assert report["memory"]["semantic_authority"] == "MemoryController"
    assert report["memory"]["preference_authority"] == "MemoryBank"
    assert report["memory"]["durable_semantic_authority"] is False


def test_migration_dry_run_does_not_require_data_movement():
    result = dry_run_audit()
    assert result["migration_required"] is False
    assert result["duplicates_requiring_migration"] == 0
    assert result["orphans"] == 0
    assert result["authority_conflicts"] == 0


@pytest.mark.asyncio
async def test_sa07_session_projection_loss_rebuilds_from_history(tmp_path):
    kv = SqliteKvStore(str(tmp_path / "tree.db"))
    tree = SessionTreeMgr(kv=kv)

    class History:
        async def save(self, sid, messages):
            return None

    coordinator = MasterCoordinator(history_store=History(), session_tree=tree, max_rounds=1)
    coordinator._agent._histories = {
        "chat-1": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]
    }
    await coordinator._persist_history("chat-1")
    expected = tree.messages("chat-1")
    snapshot_delete("chat-1", kv=kv)
    assert tree.messages("chat-1") == []
    coordinator._mirror_to_session_tree("chat-1", coordinator._agent._histories["chat-1"])
    rebuilt = [m for m in tree.messages("chat-1") if m["role"] != "system"]
    original = [m for m in expected if m["role"] != "system"]
    assert rebuilt == original

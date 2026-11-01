"""Memory lifecycle: kind classification, until expiry, tidy deferred."""

from __future__ import annotations

import json

import pytest

from server.bot_team.memory import CORE_FILE
from server.bot_team.memory_layers import LayeredMemoryStore, recall, remember
from veya.oskill.pure.memory_layers import (
    entry_expired,
    entry_until,
    normalize_entry,
    validate_entry_kind,
    validate_entry_until,
)


def test_normalize_entry_renders_kind_and_until():
    entry = normalize_entry(
        "prefers tea",
        source="part 1 #a1",
        added="2026-11-02",
        bot_id="chef",
        kind="preference",
        until="2099-01-01",
    )
    assert entry == (
        "prefers tea [source: part 1 #a1; added: 2026-11-02; by: chef; "
        "kind: preference; until: 2099-01-01]"
    )
    # Re-normalising strips the old suffix instead of doubling it.
    again = normalize_entry(
        entry, source="part 1 #a1", added="2026-11-02", bot_id="chef"
    )
    assert again == "prefers tea [source: part 1 #a1; added: 2026-11-02; by: chef]"
    # Legacy entries without kind/until keep their shape.
    legacy = normalize_entry(
        "likes tea", source="part 1 #a1", added="2026-10-11", bot_id="chef"
    )
    assert legacy == "likes tea [source: part 1 #a1; added: 2026-10-11; by: chef]"


def test_kind_and_until_validation():
    assert validate_entry_kind("") == ""
    assert validate_entry_kind("Candidate") == "candidate"
    with pytest.raises(ValueError, match="unknown memory kind"):
        validate_entry_kind("rumour")
    assert validate_entry_until("") == ""
    assert validate_entry_until("2099-12-31") == "2099-12-31"
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        validate_entry_until("31/12/2099")
    with pytest.raises(ValueError, match="already past"):
        validate_entry_until("2000-01-01")


def test_entry_expiry_parsing():
    live = "x [source: s; added: 2026-11-02; by: b; until: 2099-01-01]"
    assert entry_until(live) == "2099-01-01"
    assert entry_expired(live, today="2026-11-02") is False
    assert entry_expired(live, today="2099-01-02") is True
    assert entry_until("plain line, no suffix") is None
    assert entry_expired("plain line, no suffix") is False


def _store(tmp_path) -> LayeredMemoryStore:
    return LayeredMemoryStore("chef", bots_root=tmp_path / "bots-root")


def test_append_entry_persists_kind_until_and_refuses_bad_values(tmp_path):
    store = _store(tmp_path)
    result = store.append_entry(
        "candidate lead", kind="candidate", until="2099-06-01"
    )
    assert result["ok"] is True
    assert "kind: candidate; until: 2099-06-01" in result["entry"]
    assert "kind: candidate" in store.core_text("bot")
    before = store.core_text("bot")
    with pytest.raises(ValueError, match="unknown memory kind"):
        store.append_entry("bad kind", kind="rumour")
    with pytest.raises(ValueError, match="already past"):
        store.append_entry("born expired", until="2000-01-01")
    assert store.core_text("bot") == before


def test_recall_and_search_hide_expired_entries(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.append_entry("live fact", kind="fact", until="2099-01-01")
    # Expired entries cannot be written through the front door (past until
    # is refused), so plant one the way an older write would have left it.
    core = store.core_text("bot")
    expired = (
        "old lead [source: part 1 #chef; added: 2026-01-01; by: chef; "
        "kind: candidate; until: 2026-01-02]"
    )
    store._store.write(CORE_FILE, f"{core}{expired}\n")
    monkeypatch.setattr(
        "server.bot_team.memory_layers._bound_bot", lambda: "chef"
    )
    monkeypatch.setattr(
        "server.bot_team.memory_layers.LayeredMemoryStore",
        lambda bot_id: store,
    )
    recalled = json.loads(recall(limit=10))
    assert [entry for entry in recalled["entries"] if "old lead" in entry] == []
    assert any("live fact" in entry for entry in recalled["entries"])
    hits = store.search("lead")
    assert [hit for hit in hits if "old lead" in hit["line"]] == []
    # The expired line is still on disk until gardening: expiry hides, it
    # does not delete.
    assert "old lead" in store.core_text("bot")


def test_remember_tool_refuses_bad_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setattr(
        "server.bot_team.memory_layers._bound_bot", lambda: "chef"
    )
    outcome = json.loads(remember("x", kind="rumour"))
    assert outcome["ok"] is False
    assert "unknown memory kind" in outcome["error"]

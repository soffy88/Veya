"""server.goal_session_map tests — pure file-backed session_id↔goal_id association."""

from __future__ import annotations

import pytest

from server import goal_session_map as gsm


@pytest.fixture(autouse=True)
def isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(gsm, "GOAL_LOOPS_DIR", tmp_path / "loops")


def test_missing_session_returns_none():
    assert gsm.get_goal_id("nope") is None


def test_set_then_get_round_trips():
    gsm.set_goal_id("s1", "g1")
    assert gsm.get_goal_id("s1") == "g1"


def test_multiple_sessions_independent():
    gsm.set_goal_id("s1", "g1")
    gsm.set_goal_id("s2", "g2")
    assert gsm.get_goal_id("s1") == "g1"
    assert gsm.get_goal_id("s2") == "g2"


def test_overwrite_replaces_association():
    gsm.set_goal_id("s1", "g1")
    gsm.set_goal_id("s1", "g2")
    assert gsm.get_goal_id("s1") == "g2"


def test_clear_removes_association():
    gsm.set_goal_id("s1", "g1")
    gsm.clear_goal_id("s1")
    assert gsm.get_goal_id("s1") is None


def test_clear_on_unknown_session_is_a_no_op():
    gsm.clear_goal_id("ghost")  # must not raise


def test_survives_fresh_read_from_disk(tmp_path, monkeypatch):
    """Simulates a process restart: association must be readable by a totally
    fresh call sequence, not just within the same Python process state."""
    gsm.set_goal_id("s1", "g1")
    # nothing cached at module level to reset — get_goal_id always reads from disk
    assert gsm.get_goal_id("s1") == "g1"

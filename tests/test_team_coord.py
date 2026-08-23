"""server.team_coord — 点对点协作存储(oh-my-openagent Team Mode 内化，见 memory
project_veya_pi_gap_audit)。邮箱 + 共享任务列表(claim) + 协商式关闭。
"""

from __future__ import annotations

import pytest

from server.team_coord import TeamError, TeamStore


@pytest.fixture
def store(tmp_path) -> TeamStore:
    return TeamStore(path=tmp_path / "teams.json")


def test_create_team_with_members(store):
    team = store.create(
        "explorers", description="探路队", lead="atlas", members=[{"id": "scout-1"}]
    )
    assert "atlas" in team.members
    assert "scout-1" in team.members
    assert team.members["atlas"].kind == "lead"


def test_create_duplicate_team_raises(store):
    store.create("t1")
    with pytest.raises(TeamError):
        store.create("t1")


def test_delete_with_active_members_requires_force(store):
    store.create("t1", members=[{"id": "m1"}])
    with pytest.raises(TeamError):
        store.delete("t1")
    store.delete("t1", force=True)
    assert store.get("t1").status == "deleted"


def test_list_excludes_deleted(store):
    store.create("t1")
    store.create("t2")
    store.delete("t1", force=True)
    names = [t["name"] for t in store.list()]
    assert names == ["t2"]


# ── 邮箱 ─────────────────────────────────────────────────────────────


def test_send_and_read_directed_message(store):
    store.create("t1", members=[{"id": "a"}, {"id": "b"}])
    store.send_message("t1", from_member="a", content="hi b", to_member="b")
    inbox_b = store.read_messages("t1", member_id="b")
    assert len(inbox_b) == 1
    assert inbox_b[0].content == "hi b"
    # 第二次读, unread_only 默认为空(已读过)
    assert store.read_messages("t1", member_id="b") == []


def test_broadcast_reaches_all_members(store):
    store.create("t1", members=[{"id": "a"}, {"id": "b"}])
    store.send_message("t1", from_member="a", content="everyone", to_member=None)
    assert len(store.read_messages("t1", member_id="b")) == 1
    assert len(store.read_messages("t1", member_id="a")) == 1  # 广播含发送者自己也能读


def test_message_not_addressed_to_member_is_invisible(store):
    store.create("t1", members=[{"id": "a"}, {"id": "b"}, {"id": "c"}])
    store.send_message("t1", from_member="a", content="just for b", to_member="b")
    assert store.read_messages("t1", member_id="c") == []


# ── 共享任务列表 ───────────────────────────────────────────────────────


def test_task_claim_prevents_double_claim(store):
    store.create("t1", members=[{"id": "a"}, {"id": "b"}])
    task = store.task_create("t1", title="scout auth")
    store.task_update("t1", task.id, status="claimed", claimed_by="a")
    with pytest.raises(TeamError):
        store.task_update("t1", task.id, status="claimed", claimed_by="b")


def test_task_list_filters_by_status(store):
    store.create("t1")
    t1 = store.task_create("t1", title="x")
    store.task_create("t1", title="y")
    store.task_update("t1", t1.id, status="done")
    open_tasks = store.task_list("t1", status_filter="open")
    assert len(open_tasks) == 1
    assert open_tasks[0].title == "y"


def test_task_update_missing_task_raises(store):
    store.create("t1")
    with pytest.raises(TeamError):
        store.task_update("t1", "nope", status="done")


# ── 协商式关闭 ───────────────────────────────────────────────────────


def test_shutdown_negotiation_approve(store):
    store.create("t1", members=[{"id": "a"}])
    store.shutdown_request("t1", member_id="a", reason="done for today")
    assert store.get("t1").members["a"].status == "shutdown_requested"
    store.approve_shutdown("t1", member_id="a")
    assert store.get("t1").members["a"].status == "shutdown_approved"


def test_shutdown_negotiation_reject_returns_to_active(store):
    store.create("t1", members=[{"id": "a"}])
    store.shutdown_request("t1", member_id="a")
    store.reject_shutdown("t1", member_id="a", reason="still needed")
    assert store.get("t1").members["a"].status == "active"


def test_approve_without_pending_request_raises(store):
    store.create("t1", members=[{"id": "a"}])
    with pytest.raises(TeamError):
        store.approve_shutdown("t1", member_id="a")


def test_delete_allows_requester_even_if_still_active(store):
    """delete 的活跃成员检查排除发起者自己, 否则谁都删不掉自己所在的队。"""
    store.create("t1", members=[{"id": "lead"}])
    store.delete("t1", requested_by="lead")
    assert store.get("t1").status == "deleted"


# ── 状态聚合 ─────────────────────────────────────────────────────────


def test_status_aggregates_members_and_tasks(store):
    store.create("t1", members=[{"id": "a"}, {"id": "b"}])
    t = store.task_create("t1", title="x")
    store.task_update("t1", t.id, status="claimed", claimed_by="a")
    store.task_create("t1", title="y")

    rec = store.status("t1")
    assert rec["members"] == {"a": "active", "b": "active"}
    assert rec["tasks"] == {"open": 1, "claimed": 1, "done": 0}


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "teams.json"
    store1 = TeamStore(path=path)
    store1.create("t1", members=[{"id": "a"}])
    store1.task_create("t1", title="x")

    store2 = TeamStore(path=path)
    assert store2.get("t1") is not None
    assert len(store2.task_list("t1")) == 1

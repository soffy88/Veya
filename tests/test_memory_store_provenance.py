"""docs/dev/rfc-11-state-authority-scoping.md §9.4: memory invalidate/supersede/
provenance——之前一条记忆一旦写入就永远被检索到，没有"这条记忆错了/过时了"的
表达方式。这里直接对真实 SqliteMemoryStore(tmp_path 独立 db，不碰共享环境)
验证行为，不是 mock。
"""

from __future__ import annotations

import pytest

from veya.oskill.memory_store import SqliteMemoryStore


def test_add_defaults_confidence_and_scope(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.db")
    mid = store.add_sync("u1", "fact", "user likes Python")

    mems = store.all_for_user_sync("u1")
    assert len(mems) == 1
    assert mems[0]["id"] == mid
    assert mems[0]["confidence"] == 1.0  # 缺省, 向后兼容
    assert mems[0]["scope"] == "user"
    assert mems[0]["invalidated"] is False
    assert mems[0]["superseded_by"] is None
    assert mems[0]["created_at"] == mems[0]["last_verified_at"]  # 首次写入两者相等


def test_dedup_touch_bumps_salience_not_confidence_or_created_at(tmp_path):
    """去重触达(同 user 同 text 再次 add)只应该是"再次被印证", 不该悄悄改写
    confidence/created_at——那两个字段的语义是"这条记忆本身有多确定/何时首次
    产生", 跟"又被提到一次"是两件事。"""
    store = SqliteMemoryStore(tmp_path / "m.db")
    mid1 = store.add_sync("u1", "fact", "user likes Python", confidence=0.6)
    before = store.all_for_user_sync("u1")[0]

    mid2 = store.add_sync("u1", "fact", "user likes Python", confidence=0.99)  # 故意传不同值
    after = store.all_for_user_sync("u1")[0]

    assert mid1 == mid2  # 去重, 不新增
    assert after["salience"] > before["salience"]
    assert after["confidence"] == before["confidence"] == 0.6  # 没被第二次的 0.99 覆盖
    assert after["created_at"] == before["created_at"]
    assert after["last_verified_at"] >= before["last_verified_at"]


def test_invalidate_removes_from_retrieval_but_keeps_row(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.db")
    mid = store.add_sync("u1", "fact", "outdated fact")

    changed = store.invalidate_sync(mid)

    assert changed is True
    assert store.all_for_user_sync("u1") == []  # 默认视图看不到
    kept = store.all_for_user_sync("u1", include_invalidated=True)
    assert len(kept) == 1 and kept[0]["invalidated"] is True  # 但行还在, 没物理删除
    assert store.retrieve_sync("u1", "outdated") == []


def test_invalidate_nonexistent_or_already_invalidated_returns_false(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.db")
    assert store.invalidate_sync("never-existed") is False

    mid = store.add_sync("u1", "fact", "x")
    assert store.invalidate_sync(mid) is True
    assert store.invalidate_sync(mid) is False  # 已经废弃, 不能废弃第二次


def test_supersede_links_provenance_and_replaces_in_retrieval(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.db")
    old_id = store.add_sync("u1", "fact", "user likes Python", confidence=0.7)

    new_id = store.supersede_sync(
        old_id, "u1", "fact", "user likes Python and Rust", confidence=0.95
    )

    assert new_id != old_id
    active = store.all_for_user_sync("u1")
    assert [m["id"] for m in active] == [new_id]  # 旧的不在默认视图里

    full = {m["id"]: m for m in store.all_for_user_sync("u1", include_invalidated=True)}
    assert full[old_id]["invalidated"] is True
    assert full[old_id]["superseded_by"] == new_id  # 溯源指针
    assert full[new_id]["confidence"] == 0.95

    assert [m["text"] for m in store.retrieve_sync("u1", "python")] == [
        "user likes Python and Rust"
    ]


def test_supersede_target_missing_still_writes_new_memory(tmp_path):
    """old_id 不存在(比如已经被清理过)不该拒绝写入修正后的新记忆。"""
    store = SqliteMemoryStore(tmp_path / "m.db")

    new_id = store.supersede_sync("never-existed", "u1", "fact", "corrected fact")

    assert store.all_for_user_sync("u1")[0]["id"] == new_id


@pytest.mark.asyncio
async def test_async_wrappers_roundtrip(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.db")
    mid = await store.add("u1", "fact", "async fact", confidence=0.8)

    mems = await store.all_for_user("u1")
    assert mems[0]["id"] == mid and mems[0]["confidence"] == 0.8

    new_id = await store.supersede(mid, "u1", "fact", "async fact v2")
    assert (await store.retrieve("u1", "async"))[0]["text"] == "async fact v2"

    assert await store.invalidate(new_id) is True
    assert await store.all_for_user("u1") == []

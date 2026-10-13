"""Bot-facing skill_create reaches the Personal Runtime (SkillSpec v2)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_skill_create_accepts_global_scope_and_writes_v2(monkeypatch, tmp_path):
    from runtime.personal import runtime as personal_runtime
    from runtime.personal.runtime import PersonalRuntimeStore
    from server import tool_registry

    store = PersonalRuntimeStore(sqlite_path=tmp_path / "personal.sqlite3", production=False)
    monkeypatch.setattr(personal_runtime, "_PERSONAL_RUNTIME", store)
    monkeypatch.setattr(
        tool_registry, "_personal_request_ids", lambda: ("user_sk", None, None, None)
    )
    created = await tool_registry._tool_skill_create(
        "morning-brief", "查天气，列日程，给三条待办建议", scope_type="global"
    )
    skill = await store.get_skill(created["skill_id"], versions=True)
    assert skill["scope_type"] == "user" and skill["scope_id"] == "user_sk"
    assert [v["status"] for v in skill["versions"]] == ["candidate"]


def test_skill_create_is_not_approval_gated_but_confirm_path_unchanged():
    from server.user_control import _is_gated_tool

    assert _is_gated_tool("skill_create") is False
    assert _is_gated_tool("skill_delete") is True

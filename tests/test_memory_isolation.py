"""server.coordinator_master._memory_user_id — 回归 (2026-08-16)。

此前硬编码返回 "default"：不管谁登录，所有账号提炼出的长期记忆 (个人偏好/
事实) 全部读写同一个桶——是当前默认路径 (不需要开任何 flag) 就在生效的
跨账号记忆串味。修复后改从 server.auth 的 contextvar 取已鉴权 user_id。
"""

from __future__ import annotations

from server import auth as auth_mod
from server.coordinator_master import master_coordinator


def _as_user(user_id: str, username: str | None = None):
    return auth_mod._user_ctx.set({"user_id": user_id, "username": username or user_id})


def test_memory_user_id_defaults_to_anonymous_when_not_logged_in():
    token = _as_user("anonymous")
    try:
        assert master_coordinator._memory_user_id() == "anonymous"
    finally:
        auth_mod._user_ctx.reset(token)


def test_memory_user_id_follows_logged_in_user():
    token = _as_user("alice-uuid", "alice")
    try:
        assert master_coordinator._memory_user_id() == "alice-uuid"
    finally:
        auth_mod._user_ctx.reset(token)


def test_memory_user_id_distinguishes_different_accounts():
    """回归核心断言: 两个不同账号必须拿到不同的记忆归属, 不再共享 'default'。"""
    token_a = _as_user("alice-uuid", "alice")
    try:
        uid_a = master_coordinator._memory_user_id()
    finally:
        auth_mod._user_ctx.reset(token_a)

    token_b = _as_user("bob-uuid", "bob")
    try:
        uid_b = master_coordinator._memory_user_id()
    finally:
        auth_mod._user_ctx.reset(token_b)

    assert uid_a == "alice-uuid"
    assert uid_b == "bob-uuid"
    assert uid_a != uid_b
    assert "default" not in (uid_a, uid_b)

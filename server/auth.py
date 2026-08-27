"""server/auth.py — 自建账号认证 (注册/登录/token, 零外部依赖)。

- users/tokens 存 ``~/.veya/auth.db`` (veya-data 卷, 重启不丢)。
- 密码: ``hashlib.scrypt`` (标准库, 免 bcrypt 依赖) + hmac 常量时间比较。
- Token: ``secrets.token_hex(24)``, 存 tokens 表 (可撤销, 30 天过期)。
- 鉴权: FastAPI 依赖 ``get_current_user`` → 从 ``Authorization: Bearer <token>``
  解析用户, 并把 ``user_id`` 写入 contextvar (工具/存储按用户隔离时可读)。

隔离策略 (P0 渐进): 未登录请求回落默认用户 ``anonymous`` (行为不变),
登录请求用真实用户 — 前端登录后自动带 token, 数据按 user_id 隔离。
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Header

_DB_PATH = Path.home() / ".veya" / "auth.db"
_TOKEN_TTL = 30 * 24 * 3600  # 30 天

# 当前请求的用户 (FastAPI 依赖设置; 工具/存储读取做按用户隔离)
_ANONYMOUS_USER = {"user_id": "anonymous", "username": "anonymous"}
_user_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "auth_user", default=None
)

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, _connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "  id TEXT PRIMARY KEY,"
            "  username TEXT NOT NULL UNIQUE,"
            "  password_hash TEXT NOT NULL,"
            "  created_at INTEGER NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tokens ("
            "  token TEXT PRIMARY KEY,"
            "  user_id TEXT NOT NULL,"
            "  created_at INTEGER NOT NULL,"
            "  expires_at INTEGER NOT NULL"
            ")"
        )


_init_db()


# ── 密码 ──────────────────────────────────────────────────────────────


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + dk.hex()


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except (ValueError, TypeError):
        return False
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return hmac.compare_digest(dk, expected)


# ── 用户/Token 操作 ───────────────────────────────────────────────────


def create_user(username: str, password: str) -> dict[str, str]:
    """注册。返回 {user_id, username}。重复用户名抛 ValueError。"""
    username = str(username).strip()
    if not (3 <= len(username) <= 32) or not username.replace("_", "").replace("-", "").isalnum():
        raise ValueError("用户名需 3-32 位字母/数字/下划线")
    if len(str(password)) < 6:
        raise ValueError("密码至少 6 位")
    uid = uuid.uuid4().hex
    with _lock, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
                (uid, username, _hash_password(str(password)), int(time.time())),
            )
        except sqlite3.IntegrityError:
            raise ValueError("用户名已存在") from None
    return {"user_id": uid, "username": username}


def authenticate(username: str, password: str) -> dict[str, str] | None:
    """登录校验。成功返回 user, 失败返回 None。"""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username=?",
            (str(username).strip(),),
        ).fetchone()
    if row is None or not _verify_password(str(password), row[2]):
        return None
    return {"user_id": row[0], "username": row[1]}


def issue_token(user_id: str) -> str:
    token = secrets.token_hex(24)
    now = int(time.time())
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO tokens (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, user_id, now, now + _TOKEN_TTL),
        )
    return token


def resolve_token(token: str) -> dict[str, Any] | None:
    """校验 token → {user_id, username}; 无效/过期返回 None。"""
    if not token:
        return None
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT t.user_id, u.username FROM tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token=? AND t.expires_at>?",
            (token, int(time.time())),
        ).fetchone()
    if row is None:
        return None
    return {"user_id": row[0], "username": row[1]}


def revoke_token(token: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM tokens WHERE token=?", (token,))


# ── FastAPI 集成 ──────────────────────────────────────────────────────


def current_user() -> dict[str, Any]:
    """读取当前请求用户 (contextvar)。工具/存储用它做按用户隔离。"""
    return _user_ctx.get() or _ANONYMOUS_USER


# Inject the request-local identity into the lower history store without making
# ``veya.oservi`` import this service layer (3O dependency direction guard).
try:
    from veya.oservi.history_store import set_user_id_provider

    set_user_id_provider(current_user)
except Exception:
    pass


def set_user(user: dict[str, Any]) -> None:
    """在指定异步上下文内显式设置当前用户 (SSE 流式 task 用, 不依赖 FastAPI 依赖)。"""
    _user_ctx.set(user)


def get_current_user(
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """FastAPI 依赖: 解析 Authorization: Bearer <token> → 当前用户。

    无 token / 无效 token → 回落 anonymous (渐进隔离, 不破坏现有调用)。
    登录用户的数据按 user_id 隔离; 前端登录后自动携带 token。
    """
    user: dict[str, Any] = {"user_id": "anonymous", "username": "anonymous"}
    if authorization and authorization.lower().startswith("bearer "):
        resolved = resolve_token(authorization[7:].strip())
        if resolved:
            user = resolved
    _user_ctx.set(user)
    return user


def require_user(
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """FastAPI 依赖: 强制登录。未登录抛 401 (由路由层转 HTTPException)。"""
    user = get_current_user(authorization)
    if user["user_id"] == "anonymous":
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="请先登录")
    return user

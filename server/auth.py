"""server/auth.py — 自建账号认证 (注册/登录/token, 零外部依赖)。

- users/tokens 存 ``~/.veya/auth.db`` (veya-data 卷, 重启不丢)。
- 密码: ``hashlib.scrypt`` (标准库, 免 bcrypt 依赖) + hmac 常量时间比较。
- Token: ``secrets.token_hex(24)``; **只存 sha256 摘要** (H2/L1), 可撤销, 30 天过期。
  旧库里的明文 token 在首次校验时自动迁移 (见 :func:`resolve_token`) —
  用户不掉登录。
- 鉴权: FastAPI 依赖 ``require_user`` → 从 ``Authorization: Bearer <token>``
  解析用户, 并把 ``user_id`` 写入 contextvar (工具/存储按用户隔离时可读)。

安全模型 (SEC H1):
- **默认拒绝**。未提供/无效 token → 401, 不再回落 anonymous。
- 唯一的例外是本地单用户模式: ``VEYA_LOCAL_SINGLE_USER=1`` 且请求来自
  loopback (``127.0.0.1`` / ``::1``), 供 ``veya start`` 本地开发免登录使用。
  生产部署 (非 loopback, 或多用户) 一律需要 token。
- 管理类/危险端点用 :func:`require_owner` (owner/admin only)。
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

from fastapi import Header, Request

_DB_PATH = Path(os.environ.get("VEYA_AUTH_DB", str(Path.home() / ".veya" / "auth.db")))
_TOKEN_TTL = 30 * 24 * 3600  # 30 天

# 当前请求的用户 (FastAPI 依赖设置; 工具/存储读取做按用户隔离)。
#
# ``None`` 表示"未认证"。安全关键路径 (路由鉴权/owner 判断) 必须用
# ``require_user`` / ``require_owner``; ``current_user()`` 保留给工具与存储的
# 按用户隔离键 (它把未认证统一成 anonymous, 只是数据分桶的默认值)。
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
            "  created_at INTEGER NOT NULL,"
            "  is_owner INTEGER NOT NULL DEFAULT 0"
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
        _migrate_owner_column(conn)


def _migrate_owner_column(conn: sqlite3.Connection) -> None:
    """给既有 users 表补 ``is_owner`` 列 (旧库没有此列)。

    ``CREATE TABLE IF NOT EXISTS`` 不会改已存在的表, 所以旧库上的 owner 判定会
    直接报 "no such column"。新列默认 0 — 已有账号不会被静默提权, owner 身份
    由 :func:`is_owner` 的 ``VEYA_OWNER_USER_ID`` 路径另行授予。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if not cols or "is_owner" in cols:
        return
    conn.execute("ALTER TABLE users ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0")


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


# ── Token 摘要 ───────────────────────────────────────────────────────


def hash_token(token: str) -> str:
    """sha256 摘要; tokens 表只存这个值, 数据库泄露不等于会话泄露 (H2/L1)。"""
    return hashlib.sha256(str(token).encode()).hexdigest()


def _looks_hashed(value: str) -> bool:
    """判断输入是否"看起来像已存储的 sha256 摘要"而非可调用的明文 token。

    ``secrets.token_hex(24)`` 是 48 个 hex 字符; sha256 摘要 64 个 hex。长度
    不同即可判定, 无需猜内容。返回值只用于: ``resolve_token`` 拒绝"看起来像
    摘要"的输入 (摘要本身不是凭据, H2/L1, fail-closed), 让调用方持有的明文
    token 走正常校验/迁移路径。
    """
    if len(value) != 64:
        return False  # 非 64 位 hex 一律按"可调用的明文 token"处理
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _migrate_plaintext_token(conn: sqlite3.Connection, plaintext: str, user_id: str) -> None:
    """把一行遗留明文 token 换成 sha256 摘要 (用户保持已登录)。"""
    expires_at = int(time.time()) + _TOKEN_TTL
    conn.execute("DELETE FROM tokens WHERE token=?", (plaintext,))
    conn.execute(
        "INSERT OR REPLACE INTO tokens (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (hash_token(plaintext), user_id, int(time.time()), expires_at),
    )


# ── 用户/Token 操作 ───────────────────────────────────────────────────


def create_user(
    username: str,
    password: str,
    *,
    is_owner: bool = False,
) -> dict[str, Any]:
    """注册。返回 {user_id, username, is_owner}。重复用户名抛 ValueError。"""
    username = str(username).strip()
    if not (3 <= len(username) <= 32) or not username.replace("_", "").replace("-", "").isalnum():
        raise ValueError("用户名需 3-32 位字母/数字/下划线")
    if len(str(password)) < 8:
        raise ValueError("密码至少 8 位")
    uid = uuid.uuid4().hex
    with _lock, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at, is_owner)"
                " VALUES (?,?,?,?,?)",
                (
                    uid,
                    username,
                    _hash_password(str(password)),
                    int(time.time()),
                    1 if is_owner else 0,
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError("用户名已存在") from None
    return {"user_id": uid, "username": username, "is_owner": bool(is_owner)}


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    """登录校验。成功返回 user (含 is_owner), 失败返回 None。"""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, is_owner FROM users WHERE username=?",
            (str(username).strip(),),
        ).fetchone()
    if row is None or not _verify_password(str(password), row[2]):
        return None
    return {"user_id": row[0], "username": row[1], "is_owner": bool(row[3])}


def issue_token(user_id: str) -> str:
    """签发新 token (明文只返回给调用方, 库里只落 sha256)。"""
    token = secrets.token_hex(24)
    now = int(time.time())
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO tokens (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (hash_token(token), user_id, now, now + _TOKEN_TTL),
        )
    return token


def resolve_token(token: str) -> dict[str, Any] | None:
    """校验 token → {user_id, username, is_owner}; 无效/过期返回 None。"""
    if not token:
        return None
    now = int(time.time())
    # 调用方只可能持有明文 token; 摘要本身不是凭据 (H2)。如果调用方传进来的
    # 已经是 64 位 hex, 那它既不是合法 token 也不是明文遗留值 —— 直接拒绝, 不
    # 进入下面的 "明文命中" 分支。
    if _looks_hashed(token):
        return None
    with _lock, _connect() as conn:
        # 新库: 摘要命中。
        row = conn.execute(
            "SELECT t.user_id, u.username, u.is_owner FROM tokens t"
            " JOIN users u ON u.id = t.user_id WHERE t.token=? AND t.expires_at>?",
            (hash_token(token), now),
        ).fetchone()
        if row is not None:
            return {"user_id": row[0], "username": row[1], "is_owner": bool(row[2])}
        # 旧库 (H2 迁移): 明文命中后立即换成摘要, 用户不掉登录。
        legacy = conn.execute(
            "SELECT t.user_id, u.username, u.is_owner FROM tokens t"
            " JOIN users u ON u.id = t.user_id WHERE t.token=? AND t.expires_at>?",
            (token, now),
        ).fetchone()
        if legacy is not None:
            _migrate_plaintext_token(conn, token, legacy[0])
            return {"user_id": legacy[0], "username": legacy[1], "is_owner": bool(legacy[2])}
    return None


def revoke_token(token: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM tokens WHERE token=?", (hash_token(token),))
        # 旧库也可能存着明文 (未登录过的遗留 token)。
        conn.execute("DELETE FROM tokens WHERE token=?", (token,))


def list_users() -> list[dict[str, Any]]:
    """只读账号清单 (SEC 审计用): 用户名 + 创建时间 + owner 标记, 不含任何 token。"""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at, is_owner FROM users ORDER BY created_at ASC"
        ).fetchall()
    return [
        {
            "user_id": r[0],
            "username": r[1],
            "created_at": r[2],
            "is_owner": bool(r[3]),
        }
        for r in rows
    ]


def count_users() -> int:
    with _lock, _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


# ── 本地单用户模式 ────────────────────────────────────────────────────


def _local_single_user_enabled() -> bool:
    return str(os.environ.get("VEYA_LOCAL_SINGLE_USER", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_loopback(host: str | None) -> bool:
    """True when the peer address is a loopback origin.

    只认 socket 对端地址 (``request.client.host``), 绝不信 ``X-Forwarded-Host``
    — 后者是客户端可伪造的。空 host (不提供服务端 socket 信息的场景, 如单元
    测试的 TestClient) 视为非 loopback, 默认拒绝。
    """
    return bool(host) and host in {"127.0.0.1", "::1", "localhost"}


def _peer_host(request: Any) -> str | None:
    """从 Request 取 socket 对端地址 (缺失时 None → 非 loopback)。"""
    client = getattr(request, "client", None)
    return getattr(client, "host", None)


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


def _bearer_token(authorization: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _resolve_request_user(
    authorization: str | None,
    request: Request = None,
) -> dict[str, Any] | None:
    """共用鉴权基元: 解析 bearer token, 或 (仅本地单用户模式) 放行 loopback。

    loopback 判定取 **socket 对端地址**, 不取任何请求头 — 头是客户端可伪造的。
    """
    token = _bearer_token(authorization)
    if token:
        resolved = resolve_token(token)
        if resolved:
            _user_ctx.set(resolved)
            return resolved
        return None
    # SEC H1: AuthGateMiddleware 已解析的请求身份 (来自 ?token= / cookie)。
    # 中间件只在通过校验后 set; 这里读回, 让依赖与中间件用同一个身份。
    if request is not None:
        existing = _user_ctx.get()
        if existing is not None:
            return existing
    if _local_single_user_enabled() and _is_loopback(_peer_host(request)):
        local = {"user_id": "local", "username": "local", "is_owner": True}
        _user_ctx.set(local)
        return local
    return None


def get_current_user(
    authorization: str | None = Header(None),
    request: Request = None,
) -> dict[str, Any] | None:
    """FastAPI 依赖: 解析 ``Authorization: Bearer <token>`` → 当前用户。

    无 token / 无效 token → ``None`` (**不再回落 anonymous**, SEC H1)。
    本地单用户模式 (``VEYA_LOCAL_SINGLE_USER=1`` + loopback) 返回本地默认
    用户, 供 ``veya start`` 免登录开发使用。
    """
    if request is None:
        # 非 FastAPI 调用方 (工具内部/脚本): 只看 token, 不放行本地模式。
        return _resolve_request_user(authorization, None)
    return _resolve_request_user(authorization, request)


def require_user(
    authorization: str | None = Header(None),
    request: Request = None,
) -> dict[str, Any]:
    """FastAPI 依赖: 强制认证。未认证抛 401 (SEC H1 默认拒绝)。"""
    user = get_current_user(authorization, request)
    if user is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _owner_env_ids() -> set[str]:
    """环境变量声明的 owner (逗号分隔), 供 ``VEYA_OWNER_USER_ID`` 使用。"""
    raw = os.environ.get("VEYA_OWNER_USER_IDS") or os.environ.get("VEYA_OWNER_USER_ID") or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def is_owner(user: dict[str, Any] | None) -> bool:
    """owner/admin 判定 (SEC H1/Owner 模型)。

    依据 (任一满足):
    - 用户行 ``is_owner`` / ``is_admin`` 标志;
    - ``role`` / ``roles`` 含 owner 或 admin;
    - ``user_id`` 命中 ``VEYA_OWNER_USER_IDS`` / ``VEYA_OWNER_USER_ID``。
    """
    if not user:
        return False
    user_id = str(user.get("user_id") or "")
    roles = user.get("roles") if isinstance(user.get("roles"), (list, tuple, set)) else []
    role = str(user.get("role") or "").lower()
    return bool(
        user.get("is_owner") is True
        or user.get("is_admin") is True
        or role in {"owner", "admin"}
        or {str(r).lower() for r in roles}.intersection({"owner", "admin"})
        or (bool(user_id) and user_id in _owner_env_ids())
    )


def require_owner(
    authorization: str | None = Header(None),
    request: Request = None,
) -> dict[str, Any]:
    """FastAPI 依赖: 强制 owner/admin。未认证 401, 非 owner 403 (SEC H1)。"""
    user = get_current_user(authorization, request)
    if user is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="请先登录")
    if not is_owner(user):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="仅限 owner")
    return user


# ── 全局默认拒绝 (SEC H1) ──────────────────────────────────────────────


def public_signup_enabled() -> bool:
    """``/api/v1/auth/register`` 是否对公网开放 (默认 OFF, SEC 注册项)。

    单人部署无需公开注册: owner 账号由 ``veya auth adduser`` / 邀请码创建。
    只有显式 ``VEYA_PUBLIC_SIGNUP=1`` 才允许匿名注册。
    """
    return str(os.environ.get("VEYA_PUBLIC_SIGNUP", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# 明确公开 (匿名可访问) 的路径。默认拒绝, 此表是唯一的例外来源 (SEC H1)。
# 前缀匹配 (path == entry 或以 entry + "/" 开头)。顺序无关。
_PUBLIC_PREFIXES: tuple[str, ...] = (
    # 健康检查 / OpenAPI 文档
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    # 认证入口 (register 另有 signup 开关)
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/logout",
    "/auth/providers",  # 只返回"是否已配置", 不含值
    # OAuth 2.0 (远程 MCP 授权, 客户端需要匿名可达)
    "/.well-known",
    "/register",
    "/authorize",
    "/token",
    "/revoke",
    # 已验签的 webhook / IM 入口 (签名校验在各自 handler 内, fail-closed)
    "/im/",
    "/api/v1/channels/dingtalk/webhook",
    "/api/v1/channels/feishu/webhook",
    "/api/v1/channels/telegram/webhook",
    "/api/v1/channels/wecom/webhook",
    "/api/v1/channels/wecom/verify",
    "/api/v1/webhooks",
    "/automata/webhooks",
    "/api/v1/automata/webhooks",
    "/omni/webhook",
    # CAD 销转 S2S 接口 (受 VEYA_CAD_SALES_BOT 门禁控制)
    "/api/v1/cad-sales",
    # 公开分享 (token 即凭据)
    "/api/v1/a/",  # 公开 artifact 分享
    "/api/v1/h/",  # 公开渲染 HTML 页面
    "/api/v1/html/",
    "/api/v1/push/public_key",
    # 设备配对: 未登录的新设备申请配对码 (confirm 仍要求登录)
    "/api/v1/pairing/request",
    # 前端静态资源 / SPA 根
    "/static",
    "/assets",
)

# 公开分享的 bot 页面 (GET /api/v1/bots/{id}/public 与其消息) 以及外部 HMAC 签名的
# 触发器 Webhook (POST /api/v1/bots/{id}/routines/webhook) —— 用后缀匹配而非前缀 (路径中间有 bot_id)。
_PUBLIC_SUFFIXES: tuple[str, ...] = ("/public", "/public/messages", "/routines/webhook")


def is_public_path(path: str) -> bool:
    """路径是否在显式公开白名单内 (SEC H1 默认拒绝的唯一豁免来源)。"""
    if path == "/" or path == "":
        return True
    if path in _PUBLIC_PREFIXES:
        return True
    for prefix in _PUBLIC_PREFIXES:
        if path.startswith(prefix.rstrip("/") + "/"):
            return True
    return any(path.endswith(suffix) for suffix in _PUBLIC_SUFFIXES)


def _request_token(scope: dict[str, Any]) -> str:
    """从 ASGI scope 取凭据: Authorization 头, 其次 ``token`` 查询参数, 再次 cookie。

    SSE 端点 (EventSource) 无法自定义 Authorization 头, 前端改带 ``?token=``;
    cookie 供同源浏览器流使用。三者都只是"承载方式", 校验仍走 :func:`resolve_token`。
    """
    headers = scope.get("headers") or []
    hmap: dict[bytes, bytes] = {}
    for k, v in headers:
        hmap.setdefault(k.lower(), v)
    authz = hmap.get(b"authorization", b"").decode("latin-1")
    token = _bearer_token(authz)
    if token:
        return token
    qs = scope.get("query_string", b"").decode("latin-1")
    if qs:
        from urllib.parse import parse_qs

        params = parse_qs(qs)
        if params.get("token"):
            return params["token"][0]
    cookie = hmap.get(b"cookie", b"").decode("latin-1")
    if cookie:
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name in {"veya_token", "veya.auth.token"} and value:
                return value
    return ""


class AuthGateMiddleware:
    """纯 ASGI 中间件: 全局默认拒绝 (SEC H1)。

    - 公开白名单 (:func:`is_public_path`) 直接放行。
    - 其余路径必须有有效 token, 否则 401 JSON。
    - 本地单用户模式 (``VEYA_LOCAL_SINGLE_USER=1`` + loopback) 放行, 供 ``veya start``。
    用纯 ASGI 实现而非 BaseHTTPMiddleware, 以免缓冲/破坏 SSE 流式响应。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if is_public_path(path):
            await self.app(scope, receive, send)
            return

        token = _request_token(scope)
        user = resolve_token(token) if token else None
        if user is None:
            client = scope.get("client") or ("", 0)
            if _local_single_user_enabled() and _is_loopback(client[0]):
                user = {"user_id": "local", "username": "local", "is_owner": True}
        if user is None:
            await _send_401(send)
            return
        _user_ctx.set(user)
        await self.app(scope, receive, send)


async def _send_401(send: Any) -> None:
    import json as _json

    body = _json.dumps({"detail": "请先登录"}, ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})

"""server/routes/auth.py — /api/v1/auth/* (注册/登录/登出/me) + 原有 /auth/key (provider key)。"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
# 兼容旧路径 /auth/key (provider API key 设置, 历史端点)
key_router = APIRouter(prefix="/auth", tags=["auth-legacy"])


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class SetKeyRequest(BaseModel):
    provider: str
    api_key: str


_KEY_ENV_NAMES = {
    "dashscope": "DASHSCOPE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


@key_router.post("/key")
async def set_api_key(req: SetKeyRequest) -> dict:
    env_name = _KEY_ENV_NAMES.get(req.provider.lower())
    if not env_name:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider!r}")
    os.environ[env_name] = req.api_key
    return {"status": "set", "provider": req.provider}


@key_router.get("/providers")
async def list_providers() -> dict:
    return {
        "providers": [
            {
                "name": p,
                "env_var": env,
                "configured": bool(os.environ.get(env)),
            }
            for p, env in _KEY_ENV_NAMES.items()
        ]
    }


@router.post("/register")
async def register(req: RegisterRequest) -> dict:
    """注册账号, 直接返回 token (注册即登录)。"""
    try:
        user = auth_mod.create_user(req.username, req.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    token = auth_mod.issue_token(user["user_id"])
    return {"user_id": user["user_id"], "username": user["username"], "token": token}


@router.post("/login")
async def login(req: LoginRequest) -> dict:
    user = auth_mod.authenticate(req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = auth_mod.issue_token(user["user_id"])
    return {"user_id": user["user_id"], "username": user["username"], "token": token}


@router.post("/logout")
async def logout(authorization: str | None = Header(None)) -> dict:
    if authorization and authorization.lower().startswith("bearer "):
        auth_mod.revoke_token(authorization[7:].strip())
    return {"status": "ok"}


@router.get("/me")
async def me(user: dict = Depends(auth_mod.get_current_user)) -> dict:
    return {"user_id": user["user_id"], "username": user["username"]}

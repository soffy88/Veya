"""
协作功能 API - P2 核心能力
提供多用户会话、实时协作等功能
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, APIRouter, HTTPException
from server import auth as auth_mod
from pydantic import BaseModel

from veya.collaboration import create_collaboration_manager

router = APIRouter(
    prefix="/collaboration", tags=["collaboration"], dependencies=[Depends(auth_mod.require_user)]
)

# 全局协作管理器
collaboration_manager = create_collaboration_manager()


class CreateSessionRequest(BaseModel):
    owner_id: str
    name: str | None = ""


class JoinSessionRequest(BaseModel):
    user_id: str
    username: str
    permission: str | None = "read"  # read | write | admin


class MessageRequest(BaseModel):
    user_id: str
    content: str
    message_type: str | None = "text"  # text | code | comment


class UpdateCursorRequest(BaseModel):
    user_id: str
    position: dict[str, int]


class UpdatePermissionRequest(BaseModel):
    owner_id: str
    target_user_id: str
    new_permission: str  # read | write | admin


@router.post("/session")
async def create_session(request: CreateSessionRequest) -> dict[str, Any]:
    """创建新会话"""
    try:
        session = await collaboration_manager.create_session(request.owner_id, request.name)
        return {
            "status": "success",
            "session_id": session.session_id,
            "name": session.name,
            "owner_id": session.owner_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create session: {e!s}")


@router.get("/session/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    """获取会话信息"""
    try:
        session = await collaboration_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        return {"status": "success", "session": session.get_info()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get session: {e!s}")


@router.post("/session/{session_id}/join")
async def join_session(session_id: str, request: JoinSessionRequest) -> dict[str, Any]:
    """加入会话"""
    try:
        session = await collaboration_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        success = await session.join(request.user_id, request.username, request.permission)

        return {
            "status": "success" if success else "failed",
            "session_id": session_id,
            "user_id": request.user_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to join session: {e!s}")


@router.post("/session/{session_id}/message")
async def send_message(session_id: str, request: MessageRequest) -> dict[str, Any]:
    """发送消息"""
    try:
        session = await collaboration_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        success = await session.add_message(request.user_id, request.content, request.message_type)

        return {
            "status": "success" if success else "failed",
            "session_id": session_id,
            "user_id": request.user_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send message: {e!s}")


@router.post("/session/{session_id}/cursor")
async def update_cursor(session_id: str, request: UpdateCursorRequest) -> dict[str, Any]:
    """更新光标位置"""
    try:
        session = await collaboration_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        await session.update_cursor(request.user_id, request.position)

        return {"status": "success", "session_id": session_id, "user_id": request.user_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update cursor: {e!s}")


@router.post("/session/{session_id}/permission")
async def update_permission(session_id: str, request: UpdatePermissionRequest) -> dict[str, Any]:
    """更新权限"""
    try:
        session = await collaboration_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        success = await session.update_permission(
            request.owner_id, request.target_user_id, request.new_permission
        )

        return {
            "status": "success" if success else "failed",
            "session_id": session_id,
            "target_user_id": request.target_user_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update permission: {e!s}")


@router.get("/sessions")
async def list_sessions(user_id: str | None = None) -> dict[str, Any]:
    """列出所有会话"""
    try:
        sessions = await collaboration_manager.list_sessions(user_id)
        return {"status": "success", "count": len(sessions), "sessions": sessions}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list sessions: {e!s}")


@router.delete("/session/{session_id}")
async def delete_session(session_id: str, user_id: str) -> dict[str, Any]:
    """删除会话"""
    try:
        success = await collaboration_manager.delete_session(session_id, user_id)

        return {"status": "success" if success else "failed", "session_id": session_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete session: {e!s}")

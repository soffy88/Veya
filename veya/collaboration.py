"""
协作功能模块 - P2 核心能力
功能：多用户会话、实时协作、版本控制、权限管理
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class Permission(StrEnum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


@dataclass
class Collaborator:
    """协作者"""

    user_id: str
    username: str
    permission: Permission
    joined_at: float = field(default_factory=time.time)
    is_online: bool = True
    cursor_position: dict[str, int] | None = None
    last_activity: float = field(default_factory=time.time)


@dataclass
class SessionVersion:
    """会话版本"""

    version_id: str
    session_id: str
    timestamp: float
    author: str
    description: str
    messages: list[dict]
    checksum: str


class CollaborativeSession:
    """
    协作会话

    功能：
    1. 多用户加入
    2. 实时消息同步
    3. 光标位置共享
    4. 权限管理
    5. 版本控制
    """

    def __init__(self, session_id: str, owner_id: str, name: str = ""):
        self.session_id = session_id
        self.name = name or session_id
        self.owner_id = owner_id
        self.created_at = time.time()

        self.collaborators: dict[str, Collaborator] = {}
        self.messages: list[dict] = []
        self.versions: list[SessionVersion] = []
        self.lock = asyncio.Lock()
        self.subscribers: list[asyncio.Queue] = []

        # 默认 owner 是管理员
        self.collaborators[owner_id] = Collaborator(
            user_id=owner_id, username=owner_id, permission=Permission.ADMIN
        )

    async def join(
        self, user_id: str, username: str, permission: Permission = Permission.READ
    ) -> bool:
        """用户加入会话"""
        async with self.lock:
            if user_id in self.collaborators:
                # 更新在线状态
                self.collaborators[user_id].is_online = True
                self.collaborators[user_id].last_activity = time.time()
                return True

            self.collaborators[user_id] = Collaborator(
                user_id=user_id, username=username, permission=permission
            )

            # 广播用户加入事件
            await self.broadcast(
                {
                    "type": "user_joined",
                    "user_id": user_id,
                    "username": username,
                    "timestamp": time.time(),
                }
            )
            return True

    async def leave(self, user_id: str):
        """用户离开会话"""
        async with self.lock:
            if user_id in self.collaborators:
                self.collaborators[user_id].is_online = False
                self.collaborators[user_id].last_activity = time.time()

                await self.broadcast(
                    {"type": "user_left", "user_id": user_id, "timestamp": time.time()}
                )

    async def add_message(self, user_id: str, content: str, message_type: str = "text"):
        """添加消息"""
        async with self.lock:
            if user_id not in self.collaborators:
                return False

            collaborator = self.collaborators[user_id]
            if collaborator.permission == Permission.READ:
                return False

            message = {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "username": collaborator.username,
                "content": content,
                "type": message_type,
                "timestamp": time.time(),
            }

            self.messages.append(message)

            await self.broadcast({"type": "new_message", "message": message})
            return True

    async def update_cursor(self, user_id: str, position: dict[str, int]):
        """更新光标位置"""
        async with self.lock:
            if user_id in self.collaborators:
                self.collaborators[user_id].cursor_position = position
                self.collaborators[user_id].last_activity = time.time()

                await self.broadcast(
                    {"type": "cursor_update", "user_id": user_id, "position": position}
                )

    async def update_permission(
        self, owner_id: str, target_user_id: str, new_permission: Permission
    ):
        """更新用户权限"""
        async with self.lock:
            if owner_id != self.owner_id:
                return False

            if target_user_id in self.collaborators:
                self.collaborators[target_user_id].permission = new_permission

                await self.broadcast(
                    {
                        "type": "permission_updated",
                        "user_id": target_user_id,
                        "permission": new_permission.value,
                    }
                )
                return True
            return False

    async def create_version(self, author_id: str, description: str) -> SessionVersion | None:
        """创建会话版本快照"""
        async with self.lock:
            if author_id not in self.collaborators:
                return None

            version = SessionVersion(
                version_id=str(uuid.uuid4()),
                session_id=self.session_id,
                timestamp=time.time(),
                author=author_id,
                description=description,
                messages=self.messages.copy(),
                checksum="",  # 简化
            )

            self.versions.append(version)
            return version

    async def restore_version(self, version_id: str, user_id: str) -> bool:
        """恢复到指定版本"""
        async with self.lock:
            if user_id != self.owner_id:
                return False

            for version in self.versions:
                if version.version_id == version_id:
                    self.messages = version.messages.copy()

                    await self.broadcast(
                        {
                            "type": "version_restored",
                            "version_id": version_id,
                            "timestamp": time.time(),
                        }
                    )
                    return True
            return False

    async def broadcast(self, message: dict):
        """广播消息给所有订阅者"""
        event = {"session_id": self.session_id, "timestamp": time.time(), "data": message}

        for queue in self.subscribers:
            with contextlib.suppress(Exception):
                await queue.put(event)

    def subscribe(self) -> asyncio.Queue:
        """订阅会话事件"""
        queue = asyncio.Queue()
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        """取消订阅"""
        if queue in self.subscribers:
            self.subscribers.remove(queue)

    def get_info(self) -> dict:
        """获取会话信息"""
        return {
            "session_id": self.session_id,
            "name": self.name,
            "owner_id": self.owner_id,
            "created_at": self.created_at,
            "collaborator_count": len(self.collaborators),
            "online_count": sum(1 for c in self.collaborators.values() if c.is_online),
            "message_count": len(self.messages),
            "version_count": len(self.versions),
            "collaborators": [
                {
                    "user_id": c.user_id,
                    "username": c.username,
                    "permission": c.permission.value
                    if hasattr(c.permission, "value")
                    else c.permission,
                    "is_online": c.is_online,
                }
                for c in self.collaborators.values()
            ],
        }


class CollaborationManager:
    """
    协作管理器

    管理所有协作会话
    """

    def __init__(self):
        self.sessions: dict[str, CollaborativeSession] = {}
        self.lock = asyncio.Lock()

    async def create_session(self, owner_id: str, name: str = "") -> CollaborativeSession:
        """创建新会话"""
        session_id = str(uuid.uuid4())
        session = CollaborativeSession(session_id, owner_id, name)

        async with self.lock:
            self.sessions[session_id] = session

        return session

    async def get_session(self, session_id: str) -> CollaborativeSession | None:
        """获取会话"""
        return self.sessions.get(session_id)

    async def list_sessions(self, user_id: str | None = None) -> list[dict]:
        """列出会话"""
        sessions = []
        for session in self.sessions.values():
            if not user_id or user_id in session.collaborators:
                sessions.append(session.get_info())
        return sessions

    async def delete_session(self, session_id: str, user_id: str) -> bool:
        """删除会话"""
        async with self.lock:
            session = self.sessions.get(session_id)
            if not session or session.owner_id != user_id:
                return False

            del self.sessions[session_id]
            return True


# 便捷函数
def create_collaboration_manager() -> CollaborationManager:
    """创建协作管理器"""
    return CollaborationManager()


if __name__ == "__main__":

    async def main():
        manager = create_collaboration_manager()

        # 创建会话
        session = await manager.create_session("user1", "Project Discussion")
        print(f"Created session: {session.session_id}")

        # 用户加入
        await session.join("user2", "Alice", Permission.WRITE)
        await session.join("user3", "Bob", Permission.READ)

        # 发送消息
        await session.add_message("user2", "Hello everyone!", "text")
        await session.add_message("user1", "Hi Alice", "text")

        # 创建版本
        version = await session.create_version("user1", "Initial discussion")
        print(f"Created version: {version.version_id}")

        # 获取会话信息
        info = session.get_info()
        print(f"Session info: {json.dumps(info, indent=2)}")

        # 列出会话
        sessions = await manager.list_sessions()
        print(f"Total sessions: {len(sessions)}")

    asyncio.run(main())

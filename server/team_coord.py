"""server.team_coord — 多智能体点对点协作(oh-my-openagent Team Mode 内化)。

对标 oh-my-openagent 的 Team Mode(见 memory project_veya_pi_gap_audit): 邮箱式
点对点消息 + 共享任务列表(claim 认领) + 协商式关闭(不是谁说关就关)。真实的
架构限制——veya 的并行执行单元(board.py 卡片)是跑外部 CLI 子进程
(codex/claude/hicode), 子进程自己的工具循环塞不进 veya 的 Python 工具, 所以
这不是给子进程用的, 是给主脑(MasterAgent)自己协调多个 session/工作项用的
邮箱: 一个 session 建 team+task, 另一个 session(不同 session_id, 甚至跨设备)
过一阵子来 claim/收消息——这跟 veya 的会话本来就是"各自独立调用、只靠持久化
共享状态"这个现实反而更贴合, 不需要假设"成员"是同一个长驻交互进程。

存储惯例同 server.board::BoardStore(单文件 JSON, ~/.veya/teams.json, 无外部
依赖)。纯逻辑, 零 LLM。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MEMBER_ACTIVE = "active"
MEMBER_SHUTDOWN_REQUESTED = "shutdown_requested"
MEMBER_SHUTDOWN_APPROVED = "shutdown_approved"
MEMBER_SHUTDOWN_REJECTED = "shutdown_rejected"

TASK_OPEN = "open"
TASK_CLAIMED = "claimed"
TASK_DONE = "done"

TEAM_ACTIVE = "active"
TEAM_DELETED = "deleted"


@dataclass
class Member:
    id: str
    kind: str = "subagent_type"
    status: str = MEMBER_ACTIVE
    joined_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Message:
    id: str = field(default_factory=lambda: f"m_{uuid.uuid4().hex[:8]}")
    from_member: str = ""
    to_member: str | None = None  # None = 广播
    content: str = ""
    ts: float = field(default_factory=time.time)
    read_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamTask:
    id: str = field(default_factory=lambda: f"t_{uuid.uuid4().hex[:8]}")
    title: str = ""
    description: str = ""
    status: str = TASK_OPEN
    claimed_by: str | None = None
    note: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Team:
    name: str
    description: str = ""
    lead: str = ""
    status: str = TEAM_ACTIVE
    members: dict[str, Member] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    tasks: dict[str, TeamTask] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "lead": self.lead,
            "status": self.status,
            "members": {mid: m.to_dict() for mid, m in self.members.items()},
            "messages": [m.to_dict() for m in self.messages],
            "tasks": {tid: t.to_dict() for tid, t in self.tasks.items()},
            "created_at": self.created_at,
        }


class TeamError(ValueError):
    pass


class TeamStore:
    """点对点协作存储(~/.veya/teams.json)。单文件全量, 惯例同 BoardStore。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path or Path.home() / ".veya" / "teams.json")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._teams: dict[str, Team] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):  # pragma: no cover
            return
        for name, td in data.items():
            team = Team(
                name=name,
                description=td.get("description", ""),
                lead=td.get("lead", ""),
                status=td.get("status", TEAM_ACTIVE),
                created_at=td.get("created_at", time.time()),
            )
            for mid, md in (td.get("members") or {}).items():
                team.members[mid] = Member(
                    **{k: v for k, v in md.items() if k in Member.__dataclass_fields__}
                )
            for md in td.get("messages") or []:
                team.messages.append(
                    Message(**{k: v for k, v in md.items() if k in Message.__dataclass_fields__})
                )
            for tid, taskd in (td.get("tasks") or {}).items():
                team.tasks[tid] = TeamTask(
                    **{k: v for k, v in taskd.items() if k in TeamTask.__dataclass_fields__}
                )
            self._teams[name] = team

    def _save(self) -> None:
        self._path.write_text(
            json.dumps(
                {n: t.to_dict() for n, t in self._teams.items()}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )

    def get(self, name: str) -> Team | None:
        return self._teams.get(name)

    def _require(self, name: str) -> Team:
        team = self._teams.get(name)
        if team is None:
            raise TeamError(f"team 不存在: {name}")
        return team

    # ── 生命周期 ─────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        *,
        description: str = "",
        lead: str = "",
        members: list[dict[str, str]] | None = None,
    ) -> Team:
        if name in self._teams and self._teams[name].status != TEAM_DELETED:
            raise TeamError(f"team 已存在: {name}")
        team = Team(name=name, description=description, lead=lead)
        for m in members or []:
            mid = m["id"]
            team.members[mid] = Member(id=mid, kind=m.get("kind", "subagent_type"))
        if lead and lead not in team.members:
            team.members[lead] = Member(id=lead, kind="lead")
        self._teams[name] = team
        self._save()
        return team

    def delete(self, name: str, *, requested_by: str = "", force: bool = False) -> None:
        team = self._require(name)
        if not force:
            # 只有明确批准过关闭(MEMBER_SHUTDOWN_APPROVED)的成员才算"安全"——
            # 光发了 shutdown_request 还没走完协商就放行, 等于绕过整个协商流程。
            active = [
                m
                for m in team.members.values()
                if m.status != MEMBER_SHUTDOWN_APPROVED and m.id != requested_by
            ]
            if active:
                raise TeamError(
                    f"还有成员未完成关闭协商: {[m.id for m in active]}(force=True 强制解散)"
                )
        team.status = TEAM_DELETED
        self._save()

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "status": t.status,
                "members": len(t.members),
                "open_tasks": sum(1 for tk in t.tasks.values() if tk.status == TASK_OPEN),
                "unclaimed": sum(
                    1 for tk in t.tasks.values() if tk.status == TASK_OPEN and not tk.claimed_by
                ),
            }
            for t in self._teams.values()
            if t.status != TEAM_DELETED
        ]

    # ── 邮箱 ─────────────────────────────────────────────────────────

    def send_message(
        self, name: str, *, from_member: str, content: str, to_member: str | None = None
    ) -> Message:
        team = self._require(name)
        msg = Message(from_member=from_member, to_member=to_member, content=content)
        team.messages.append(msg)
        self._save()
        return msg

    def read_messages(
        self, name: str, *, member_id: str, unread_only: bool = True
    ) -> list[Message]:
        team = self._require(name)
        out = []
        for m in team.messages:
            addressed = m.to_member is None or m.to_member == member_id
            if not addressed:
                continue
            if unread_only and member_id in m.read_by:
                continue
            out.append(m)
            if member_id not in m.read_by:
                m.read_by.append(member_id)
        self._save()
        return out

    # ── 共享任务列表 ─────────────────────────────────────────────────

    def task_create(self, name: str, *, title: str, description: str = "") -> TeamTask:
        team = self._require(name)
        task = TeamTask(title=title, description=description)
        team.tasks[task.id] = task
        self._save()
        return task

    def task_list(self, name: str, *, status_filter: str | None = None) -> list[TeamTask]:
        team = self._require(name)
        tasks = list(team.tasks.values())
        if status_filter:
            tasks = [t for t in tasks if t.status == status_filter]
        return tasks

    def task_get(self, name: str, task_id: str) -> TeamTask | None:
        team = self._require(name)
        return team.tasks.get(task_id)

    def task_update(
        self,
        name: str,
        task_id: str,
        *,
        status: str | None = None,
        claimed_by: str | None = None,
        note: str | None = None,
    ) -> TeamTask:
        team = self._require(name)
        task = team.tasks.get(task_id)
        if task is None:
            raise TeamError(f"task 不存在: {task_id}")
        if status == TASK_CLAIMED:
            if task.status == TASK_CLAIMED and task.claimed_by and task.claimed_by != claimed_by:
                raise TeamError(f"已被 {task.claimed_by} 认领")
            task.claimed_by = claimed_by
        if status is not None:
            task.status = status
        if note is not None:
            task.note = note
        task.updated_at = time.time()
        self._save()
        return task

    # ── 协商式关闭 ───────────────────────────────────────────────────

    def shutdown_request(self, name: str, *, member_id: str, reason: str = "") -> Member:
        team = self._require(name)
        member = team.members.get(member_id)
        if member is None:
            raise TeamError(f"member 不存在: {member_id}")
        member.status = MEMBER_SHUTDOWN_REQUESTED
        self._save()
        return member

    def approve_shutdown(self, name: str, *, member_id: str) -> Member:
        team = self._require(name)
        member = team.members.get(member_id)
        if member is None:
            raise TeamError(f"member 不存在: {member_id}")
        if member.status != MEMBER_SHUTDOWN_REQUESTED:
            raise TeamError(f"{member_id} 没有待处理的关闭请求(当前状态: {member.status})")
        member.status = MEMBER_SHUTDOWN_APPROVED
        self._save()
        return member

    def reject_shutdown(self, name: str, *, member_id: str, reason: str = "") -> Member:
        team = self._require(name)
        member = team.members.get(member_id)
        if member is None:
            raise TeamError(f"member 不存在: {member_id}")
        if member.status != MEMBER_SHUTDOWN_REQUESTED:
            raise TeamError(f"{member_id} 没有待处理的关闭请求(当前状态: {member.status})")
        member.status = MEMBER_ACTIVE
        self._save()
        return member

    def status(self, name: str) -> dict[str, Any]:
        team = self._require(name)
        return {
            "name": team.name,
            "status": team.status,
            "members": {mid: m.status for mid, m in team.members.items()},
            "tasks": {
                "open": sum(1 for t in team.tasks.values() if t.status == TASK_OPEN),
                "claimed": sum(1 for t in team.tasks.values() if t.status == TASK_CLAIMED),
                "done": sum(1 for t in team.tasks.values() if t.status == TASK_DONE),
            },
            "unread_messages": len(team.messages),
        }


_default_store: TeamStore | None = None


def default_team_store() -> TeamStore:
    global _default_store
    if _default_store is None:
        _default_store = TeamStore()
    return _default_store

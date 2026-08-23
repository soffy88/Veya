"""server.team_tools — team_coord 的 MasterAgent 工具面(oh-my-openagent Team Mode 内化)。

薄封装(同 server/wayfinding_tools.py 的风格): 成功用 ✅, 失败给可读原因,
不抛异常往外传(工具执行失败会被 MasterToolRegistry 当异常回喂反思, 这里的
错误更适合直接读)。业务逻辑在 server.team_coord.TeamStore。

用法顺序: team_create → team_task_create(可选) → team_send_message/
team_read_messages(协调) → team_task_update(claim/完成) → team_status(查看)
→ 完事后 team_shutdown_request → team_approve_shutdown/team_reject_shutdown
→ team_delete。
"""

from __future__ import annotations

from server.team_coord import TeamError, default_team_store


async def team_create(
    name: str, description: str = "", lead: str = "", member_ids: list[str] | None = None
) -> str:
    """建一个协作组(邮箱+共享任务列表)。lead 会自动加入 members。"""
    try:
        members = [{"id": mid} for mid in (member_ids or [])]
        team = default_team_store().create(
            name, description=description, lead=lead, members=members
        )
    except TeamError as exc:
        return f"team_create: {exc}"
    return f"✅ 已建 team '{name}', 成员: {sorted(team.members)}"


async def team_delete(name: str, requested_by: str = "", force: bool = False) -> str:
    """解散协作组。还有活跃成员(除发起者外)且 force=False 会被拒绝。"""
    try:
        default_team_store().delete(name, requested_by=requested_by, force=force)
    except TeamError as exc:
        return f"team_delete: {exc}"
    return f"✅ 已解散 team '{name}'"


async def team_list() -> str:
    """列出所有未解散的协作组及其任务概况。"""
    teams = default_team_store().list()
    if not teams:
        return "暂无协作组"
    return "\n".join(
        f"- {t['name']}: {t['members']} 成员, {t['open_tasks']} 个未完成任务"
        f"({t['unclaimed']} 未认领)"
        for t in teams
    )


async def team_send_message(name: str, from_member: str, content: str, to_member: str = "") -> str:
    """发一条消息。to_member 留空 = 广播给全体成员。"""
    try:
        default_team_store().send_message(
            name, from_member=from_member, content=content, to_member=to_member or None
        )
    except TeamError as exc:
        return f"team_send_message: {exc}"
    target = to_member or "全体"
    return f"✅ 已发给 {target}"


async def team_read_messages(name: str, member_id: str, unread_only: bool = True) -> str:
    """读某个成员的邮箱(发给他的 + 广播的)。默认只看未读, 读过的会标记已读。"""
    try:
        msgs = default_team_store().read_messages(
            name, member_id=member_id, unread_only=unread_only
        )
    except TeamError as exc:
        return f"team_read_messages: {exc}"
    if not msgs:
        return "没有新消息"
    return "\n".join(f"[{m.from_member} → {m.to_member or '全体'}] {m.content}" for m in msgs)


async def team_task_create(name: str, title: str, description: str = "") -> str:
    """加一个共享任务(open 状态, 谁都能来 claim)。"""
    try:
        task = default_team_store().task_create(name, title=title, description=description)
    except TeamError as exc:
        return f"team_task_create: {exc}"
    return f"✅ 已加任务 {task.id}: {task.title}"


async def team_task_list(name: str, status_filter: str = "") -> str:
    """列出共享任务, 可按状态过滤(open/claimed/done)。"""
    try:
        tasks = default_team_store().task_list(name, status_filter=status_filter or None)
    except TeamError as exc:
        return f"team_task_list: {exc}"
    if not tasks:
        return "没有匹配的任务"
    return "\n".join(
        f"- {t.id} [{t.status}]{f' by {t.claimed_by}' if t.claimed_by else ''}: {t.title}"
        for t in tasks
    )


async def team_task_get(name: str, task_id: str) -> str:
    """看一个任务的详情(含 note)。"""
    try:
        task = default_team_store().task_get(name, task_id)
    except TeamError as exc:
        return f"team_task_get: {exc}"
    if task is None:
        return f"任务不存在: {task_id}"
    return (
        f"{task.id} [{task.status}] {task.title}\n{task.description}\n"
        f"认领人: {task.claimed_by or '(无)'}\nnote: {task.note}"
    )


async def team_task_update(
    name: str, task_id: str, status: str = "", claimed_by: str = "", note: str = ""
) -> str:
    """更新任务状态(status=claimed 时必须带 claimed_by, 已被别人认领会拒绝)/认领人/备注。"""
    try:
        task = default_team_store().task_update(
            name, task_id, status=status or None, claimed_by=claimed_by or None, note=note or None
        )
    except TeamError as exc:
        return f"team_task_update: {exc}"
    return f"✅ {task.id} 现在是 [{task.status}]"


async def team_shutdown_request(name: str, member_id: str, reason: str = "") -> str:
    """请求某成员关闭(不是立刻关, 要走 team_approve_shutdown/team_reject_shutdown)。"""
    try:
        default_team_store().shutdown_request(name, member_id=member_id, reason=reason)
    except TeamError as exc:
        return f"team_shutdown_request: {exc}"
    return f"✅ 已发起 {member_id} 的关闭请求, 等待 approve/reject"


async def team_approve_shutdown(name: str, member_id: str) -> str:
    """批准某成员的关闭请求。"""
    try:
        default_team_store().approve_shutdown(name, member_id=member_id)
    except TeamError as exc:
        return f"team_approve_shutdown: {exc}"
    return f"✅ {member_id} 关闭已批准"


async def team_reject_shutdown(name: str, member_id: str, reason: str = "") -> str:
    """拒绝某成员的关闭请求, 成员状态回到 active。"""
    try:
        default_team_store().reject_shutdown(name, member_id=member_id, reason=reason)
    except TeamError as exc:
        return f"team_reject_shutdown: {exc}"
    return f"✅ {member_id} 关闭请求已拒绝, 继续工作"


async def team_status(name: str) -> str:
    """汇总视图: 成员状态 + 任务计数 + 消息数。"""
    try:
        rec = default_team_store().status(name)
    except TeamError as exc:
        return f"team_status: {exc}"
    members = ", ".join(f"{mid}={s}" for mid, s in rec["members"].items())
    tasks = rec["tasks"]
    return (
        f"team '{rec['name']}' [{rec['status']}]\n成员: {members}\n"
        f"任务: open={tasks['open']} claimed={tasks['claimed']} done={tasks['done']}\n"
        f"消息总数: {rec['unread_messages']}"
    )

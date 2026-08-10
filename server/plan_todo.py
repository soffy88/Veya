"""plan_todo — 会话级可执行计划看板 (模型自主创建/推进/交接, 零程序路由)。

借鉴 loopx 的 objective + todo + evidence 状态内核, 给主脑"制定计划 + 分派
任务 + 跨轮续做"的持久化载体:

- ``create_plan(objective, todos)`` → 持久化计划 (JSON), 返回 plan_id + 文本视图
- ``plan_status(plan_id?)`` → 文本视图 (未完成 todos 优先)
- ``update_todo(plan_id, todo_id, status, evidence)`` → 状态流转 + 追加证据

每次变更经 ``server.events.fire_step`` 发 ``plan_update`` SSE 事件 (前端渲染
为可折叠计划块)。存储于 ``~/.veya/plans/{plan_id}.json`` (持久 volume,
跨会话可查/续做)。纯确定性: 零 LLM, 零网络。

冻结架构兼容: 全部是模型自主调用的工具; 无程序路由 / 无自动并行分派;
事件属于"真实执行轨迹" (模型真建了计划), 不是思考噪音。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

_PLANS_ROOT = Path.home() / ".veya" / "plans"

_VALID_STATUS = {"open", "in_progress", "done", "blocked"}


# ── 存储 ──────────────────────────────────────────────────────────────

def _plans_dir() -> Path:
    """按用户隔离: ~/.veya/plans/{user_id}/ (登录用户各自目录, 匿名共用 anonymous)。"""
    try:
        from server.auth import current_user

        uid = current_user()["user_id"] or "anonymous"
    except Exception:  # noqa: BLE001
        uid = "anonymous"
    d = _PLANS_ROOT / uid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(plan_id: str) -> Path:
    # 防逃逸: plan_id 必须是 [a-z0-9-]{4,64}
    if not plan_id or not all(c.isalnum() or c in "-_" for c in plan_id) or len(plan_id) > 64:
        raise ValueError(f"非法 plan_id: {plan_id!r}")
    return _plans_dir() / f"{plan_id}.json"


def _load(plan_id: str) -> dict:
    p = _path(plan_id)
    if not p.exists():
        raise ValueError(f"计划不存在: {plan_id} (可用 plan_status 查看已有计划)")
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save(plan: dict) -> None:
    plan["updated_at"] = _now()
    with _path(plan["plan_id"]).open("w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _fire(plan: dict, action: str) -> None:
    """plan 变更 → SSE plan_update 事件 (前端渲染为计划块)。"""
    try:
        from server.events import fire_step

        fire_step({
            "type": "plan_update",
            "plan_id": plan["plan_id"],
            "action": action,
            "objective": plan.get("objective", ""),
            "todos": [
                {"id": t["id"], "title": t.get("title", ""), "status": t.get("status", "open")}
                for t in plan.get("todos", [])
            ],
        })
    except Exception:  # noqa: BLE001 — 事件推送失败绝不拖垮工具
        pass


# ── 视图 ──────────────────────────────────────────────────────────────

def _render(plan: dict, *, brief: bool = False) -> str:
    lines = [f"📋 计划 {plan['plan_id']}: {plan.get('objective', '')}"]
    lines.append(f"   更新: {plan.get('updated_at', '')}")
    for t in plan.get("todos", []):
        mark = {"done": "✅", "in_progress": "▶️", "blocked": "⛔", "open": "⬜"}.get(
            t.get("status", "open"), "⬜"
        )
        title = t.get("title", "")
        if brief:
            lines.append(f"  {mark} {title}")
            continue
        lines.append(f"  {mark} [{t.get('id', '?')}] {title}")
        if t.get("assignee"):
            lines.append(f"      指派: {t['assignee']}")
        if t.get("detail"):
            lines.append(f"      详情: {t['detail']}")
        if t.get("depends_on"):
            lines.append(f"      依赖: {', '.join(t['depends_on'])}")
        for ev in t.get("evidence", [])[-3:]:
            note = ev.get("note", "") if isinstance(ev, dict) else str(ev)
            if note:
                lines.append(f"      证据: {note}")
    done = sum(1 for t in plan.get("todos", []) if t.get("status") == "done")
    total = len(plan.get("todos", []))
    lines.append(f"   进度: {done}/{total}")
    return "\n".join(lines)


# ── 工具实现 ──────────────────────────────────────────────────────────

async def create_plan(objective: str, todos: list[dict]) -> str:
    """创建一个可执行计划 (目标 + 待办列表), 持久化, 返回 plan_id 与视图。

    todos 每项: {"id": "t1", "title": "...", "detail"?: "...",
                 "depends_on"?: ["t0"], "assignee"?: "hicode|genesis|self"}。
    status 初始 open。复杂任务先拆解再逐项执行, 跨轮可续做。
    """
    if not objective or not str(objective).strip():
        raise ValueError("objective 不能为空 (把用户意图压缩成一句可验收的目标)")
    if not todos:
        raise ValueError("todos 不能为空 (至少一个可执行步骤)")
    norm: list[dict] = []
    for i, raw in enumerate(todos):
        if not isinstance(raw, dict) or not str(raw.get("id", "")).strip():
            raise ValueError(f"todos[{i}] 缺少 id (如 t{i})")
        if not str(raw.get("title", "")).strip():
            raise ValueError(f"todos[{i}] 缺少 title")
        status = str(raw.get("status", "open"))
        if status not in _VALID_STATUS:
            raise ValueError(f"todos[{i}] status 非法: {status} (open|in_progress|done|blocked)")
        norm.append({
            "id": str(raw["id"]).strip(),
            "title": str(raw["title"]).strip(),
            "detail": str(raw.get("detail", "")).strip(),
            "depends_on": [str(d) for d in (raw.get("depends_on") or [])],
            "assignee": str(raw.get("assignee", "")).strip() or None,
            "status": status,
            "evidence": [],
            "updated_at": _now(),
        })
    plan = {
        "plan_id": uuid.uuid4().hex[:10],
        "objective": str(objective).strip(),
        "created_at": _now(),
        "updated_at": _now(),
        "todos": norm,
    }
    _save(plan)
    _fire(plan, "create")
    return f"已创建计划 {plan['plan_id']}\n" + _render(plan)


async def plan_status(plan_id: str = "") -> str:
    """查看计划进度。plan_id 为空 → 列出最近计划 (含未完成项优先)。"""
    plans_dir = _plans_dir()
    if not plan_id:
        files = sorted(plans_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
        if not files:
            return "暂无计划 (复杂任务可先 create_plan 拆解再执行)。"
        lines = ["最近计划:"]
        for p in files:
            try:
                plan = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            done = sum(1 for t in plan.get("todos", []) if t.get("status") == "done")
            total = len(plan.get("todos", []))
            lines.append(f"  {plan['plan_id']} ({done}/{total}) {plan.get('objective', '')[:60]}")
        lines.append("查看详情: plan_status(plan_id=<id>)。")
        return "\n".join(lines)
    plan = _load(plan_id)
    return _render(plan)


async def update_todo(plan_id: str, todo_id: str, status: str, evidence: str = "") -> str:
    """推进计划: 更新一个 todo 的状态并追加证据 (验证结果/产物路径)。

    status: open|in_progress|done|blocked。evidence: 简短验证说明 (如
    "pytest 12 passed" / "文件已生成: x.py")。
    """
    if status not in _VALID_STATUS:
        raise ValueError(f"status 非法: {status} (open|in_progress|done|blocked)")
    plan = _load(plan_id)
    target = next((t for t in plan["todos"] if t["id"] == todo_id), None)
    if target is None:
        raise ValueError(f"todo 不存在: {todo_id} (计划 {plan_id})")
    target["status"] = status
    target["updated_at"] = _now()
    if evidence and str(evidence).strip():
        target["evidence"].append({"at": _now(), "note": str(evidence).strip()})
        if len(target["evidence"]) > 20:
            target["evidence"] = target["evidence"][-20:]
    _save(plan)
    _fire(plan, f"todo_{todo_id}_{status}")
    return _render(plan, brief=True)


# ── 注册 ──────────────────────────────────────────────────────────────

def wire_master_tools() -> int:
    """把 plan 工具注册进 master_tools (幂等)。返回新注册数量。"""
    from server.tool_registry import master_tools

    added = 0
    tools: list[tuple[str, str, dict, Any, int]] = [
        (
            "create_plan",
            "为复杂任务创建一个可执行计划 (目标 + 待办列表), 持久化后返回 plan_id。"
            "适用: 多步骤/跨文件/需要验收的任务 (如搭建系统、重构、研究报告)。"
            "收到复杂需求时先拆解成 3-10 个有依赖顺序的 todos, 再逐项执行;"
            "每完成一项用 update_todo 标记 done 并附证据。跨轮续做先用 plan_status 看进度。",
            {
                "type": "object",
                "properties": {
                    "objective": {"type": "string", "description": "把用户意图压缩成一句可验收的目标。"},
                    "todos": {
                        "type": "array",
                        "description": "待办列表, 每项: {id: 't1', title: 简短标题, detail?: 要点, depends_on?: [其他id], assignee?: 'hicode'|'genesis'|'self'}",
                        "items": {"type": "object"},
                    },
                },
                "required": ["objective", "todos"],
            },
            lambda **kw: None,  # placeholder, 下方替换
            12000,
        ),
        (
            "plan_status",
            "查看计划进度。plan_id 为空列出最近计划 (未完成优先); 指定 id 看完整 todo 与证据。"
            "跨轮续做、或用户问「做到哪了」时调用。",
            {"type": "object", "properties": {"plan_id": {"type": "string", "description": "可选。计划 id (create_plan 返回)。"}}},
            lambda **kw: None,
            8000,
        ),
        (
            "update_todo",
            "推进计划: 更新一个 todo 的状态并追加证据 (验证结果/产物路径)。"
            "每完成一步立即调用, 让计划与真实进度一致。",
            {"type": "object", "properties": {
                "plan_id": {"type": "string", "description": "计划 id (create_plan 返回)。"},
                "todo_id": {"type": "string", "description": "todo id (create_plan 的 todos 里定义的 id)。"},
                "status": {"type": "string", "enum": ["open", "in_progress", "done", "blocked"]},
                "evidence": {"type": "string", "description": "简短验证说明 (如 pytest 12 passed / 文件已生成)。"},
            }, "required": ["plan_id", "todo_id", "status"]},
            lambda **kw: None,
            8000,
        ),
    ]
    funcs = {"create_plan": create_plan, "plan_status": plan_status, "update_todo": update_todo}
    for name, desc, params, _ph, limit in tools:
        if master_tools.has(name):
            continue
        master_tools.register(name, desc, params, funcs[name], max_result_chars=limit)
        added += 1
    return added

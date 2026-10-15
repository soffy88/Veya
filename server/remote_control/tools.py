"""W13 model-callable tools — remote host + agent control.

Registered **only** when ``VEYA_REMOTE_CONTROL`` is on (registration-time gate,
the same shape as ``server/approval_tools.py``), so a deployment with the flag
off does not grow a model-visible surface it cannot serve.

Identity is never a model argument: the caller's bot comes from
``current_bot_id()`` and its owner from ``BotRegistry``. The three mutating
tools declare ``approved: boolean`` and refuse to touch anything without an
approved record in the existing approval store — the same contract
``server/github_pr_tools.py:95`` uses.
"""

from __future__ import annotations

import json
from typing import Any

from server.remote_control import approval as approval_mod
from server.remote_control import flags
from server.remote_control import hosts as hosts_mod
from server.remote_control import service as service_mod
from server.remote_control import sessions as sessions_mod


def _caller_owner_id() -> str:
    """The owner of the bot whose turn is running. Never a model argument."""

    from server.goal_run.bot_identity import BotRegistry, current_bot_id

    bot_id = current_bot_id()
    if not bot_id:
        return ""
    spec = BotRegistry().get(bot_id)
    return str(getattr(spec, "owner_id", "") or "") if spec is not None else ""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def remote_host_list() -> str:
    """List the caller's remote hosts (read-only)."""

    owner = _caller_owner_id()
    if not owner:
        return "没有可用的远程主机：当前没有可识别的机器人身份。"
    hosts = hosts_mod.list_hosts(owner_id=owner)
    return _dump([h.to_dict() for h in hosts])


async def remote_agent_start(
    host_id: str, task: str, approved: bool = False, agent_label: str = "", transport: str = "local"
) -> str:
    """Accept a remote task (approval-gated)."""

    if not approved:
        return "已拒绝：remote_agent_start 需要 approved=true（人工批准）后才能执行。"
    owner = _caller_owner_id()
    if not owner:
        return "已拒绝：无法确定调用者身份。"
    result = await service_mod.request_start(
        host_id=host_id,
        owner_id=owner,
        task=task,
        agent_label=agent_label,
        transport=transport,
    )
    session = result["session"]
    return _dump(
        {
            "session_id": session.session_id,
            "state": session.state,
            "approval_id": result["approval_id"],
            "identity": result["identity"].get("node_name", ""),
        }
    )


async def remote_agent_cancel(session_id: str, approved: bool = False) -> str:
    """Request cancellation of a remote run (approval-gated)."""

    if not approved:
        return "已拒绝：remote_agent_cancel 需要 approved=true 后才能执行。"
    owner = _caller_owner_id()
    if not owner:
        return "已拒绝：无法确定调用者身份。"
    result = await service_mod.request_cancel(session_id, owner_id=owner)
    return _dump({"session_id": session_id, "approval_id": result["approval_id"]})


async def remote_agent_handoff(approval_id: str, approved: bool = False) -> str:
    """Apply an **already approved** hand-off (approval-gated).

    The target host is the one recorded when the hand-off was requested, so it
    cannot be redirected here.
    """

    if not approved:
        return "已拒绝：remote_agent_handoff 需要 approved=true 后才能执行。"
    session_id = approval_mod.session_for_approval(approval_id)
    if not session_id:
        return "已拒绝：找不到该审批对应的远程会话。"
    session = service_mod.handoff(session_id, approval_id=approval_id)
    return _dump(
        {
            "session_id": session.session_id,
            "state": session.state,
            "handoff_to": session.handoff_to,
        }
    )


async def remote_task_status(session_id: str) -> str:
    """Read one remote session's state and verifier evidence (read-only)."""

    owner = _caller_owner_id()
    session = sessions_mod.get_session(session_id)
    if session is None or (owner and session.owner_id != owner):
        return "找不到该远程会话。"
    evidence = session.evidence_view()
    return _dump(
        {
            "session_id": session.session_id,
            "state": session.state,
            "host_id": session.host_id,
            "agent_label": session.agent_label,
            "wake_outcome": session.wake_outcome,
            "verified": evidence.usable,
            "evidence": evidence.summary,
        }
    )


def _mutating(description: str, properties: dict[str, Any], required: list[str]):
    return (
        description,
        {
            "type": "object",
            "properties": {**properties, "approved": {"type": "boolean"}},
            "required": [*required, "approved"],
        },
    )


def _read_only(description: str, properties: dict[str, Any], required: list[str]):
    return (description, {"type": "object", "properties": properties, "required": required})


_TOOLS: list[tuple[str, str, dict[str, Any], Any, str]] = [
    (
        "remote_host_list",
        *_read_only(
            "列出当前用户可驱动的远程主机（tailnet 上的机器）。只读；不执行任何远程动作。",
            {},
            [],
        ),
        remote_host_list,
        "pure_read",
    ),
    (
        "remote_agent_start",
        *_mutating(
            "在指定远程主机上启动一个 agent 任务。属于外部变更："
            "只有 approved=true 且经由既有审批通道批准后才会记录并请求启动；"
            "缺少 approved 时绝不产生任何远程变更。",
            {
                "host_id": {
                    "type": "string",
                    "description": "远程主机 id（见 remote_host_list）。",
                },
                "task": {"type": "string", "description": "任务文本。"},
                "agent_label": {
                    "type": "string",
                    "description": "执行器标签，如 pi/claude-wb/codex。",
                },
                "transport": {"type": "string", "enum": ["local", "ssh"]},
            },
            ["host_id", "task"],
        ),
        remote_agent_start,
        "external_mutation",
    ),
    (
        "remote_agent_cancel",
        *_mutating(
            "请求取消一个正在运行的远程 agent 会话。属于外部变更，"
            "需要 approved=true；只有处于 woken/acked 的会话可取消。",
            {"session_id": {"type": "string", "description": "远程会话 id。"}},
            ["session_id"],
        ),
        remote_agent_cancel,
        "external_mutation",
    ),
    (
        "remote_agent_handoff",
        *_mutating(
            "把一次已批准的远程会话移交给另一个 agent（携带上下文）。属于外部变更，"
            "需要 approved=true 且 approval_id 是已被批准的记录；目标主机在请求时固定，"
            "不可在此改向。",
            {
                "approval_id": {
                    "type": "string",
                    "description": "已批准的远程移交审批记录 id。",
                }
            },
            ["approval_id"],
        ),
        remote_agent_handoff,
        "external_mutation",
    ),
    (
        "remote_task_status",
        *_read_only(
            "查询一个远程 agent 会话的状态与验收证据。只读。",
            {"session_id": {"type": "string", "description": "远程会话 id。"}},
            ["session_id"],
        ),
        remote_task_status,
        "pure_read",
    ),
]


def register_tools(registry: Any = None) -> int:
    """Register the W13 tools when the flag is on. Returns how many were added."""

    if not flags.enabled():
        return 0
    from server.tool_registry import SideEffect, master_tools

    target = registry if (registry is not None and hasattr(registry, "has")) else master_tools
    added = 0
    for name, description, parameters, func, effect in _TOOLS:
        if target.has(name):
            continue
        side_effect = (
            SideEffect.PURE_READ if effect == "pure_read" else SideEffect.EXTERNAL_MUTATION
        )
        target.register(
            name,
            description,
            parameters,
            func,
            max_result_chars=4000,
            parallel_safe=effect == "pure_read",
            side_effect=side_effect,
            effect_capability="none" if effect == "pure_read" else "manual_only",
        )
        added += 1
    # W17: ssh host tools — only when VEYA_SSH_HOSTS is on as well.
    from server.remote_control import ssh_tools

    added += ssh_tools.register_tools(target)
    return added


__all__ = [
    "register_tools",
    "remote_agent_cancel",
    "remote_agent_handoff",
    "remote_agent_start",
    "remote_host_list",
    "remote_task_status",
]

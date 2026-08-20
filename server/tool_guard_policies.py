"""server/tool_guard_policies — 默认工具守卫策略 (确定性笼子的具体闸门).

把既有「咨询式」安全闸 (``state_kernel.terminal_gate_check``, 原本只是主脑可选
调用的工具) 接成 ``tool_guard`` 的**强制中间件**候选。

安全灰度 (功能至上):
- 缺省 **observe 模式** (``VEYA_TOOL_GATE_ENFORCE`` != "1"): 命中只落 ``observed``
  轨迹, 不拦截任何工具 —— 先采样「哪些工具会被判为 terminal」, 零行为变化;
- 置 ``VEYA_TOOL_GATE_ENFORCE=1`` 后翻 **enforce**: terminal/不可逆动作被拦截,
  回喂主脑「需人工审批」由模型改走 ``system_secure_exec`` / 询问用户。
"""

from __future__ import annotations

import json
import os

from server.tool_guard import ToolGuard, global_tool_guard

_TERMINAL_POLICY = "terminal_action_gate"
_PREFER_GRAPH_POLICY = "prefer_code_graph"
_EGRESS_POLICY = "egress_audit"

# 结构性探索工具: 问"谁调用/依赖/在哪定义"这类问题, 先查带置信标签的代码图
# (assemble_code_context) 通常比逐文件 grep/read 更省 token、更准 (graphify 先查图范式)。
_STRUCTURAL_TOOLS = frozenset({"grep", "read_file_ast", "list_files"})


def prefer_code_graph_policy(name: str, kwargs: dict, source: str) -> str | None:
    """结构性工具命中 → 返回"优先查图"咨询语。

    默认 observe (只落 observed 轨迹, 不拦截): 采样"绕过代码图直接翻文件"的频率, 零
    行为变化; SOP 已软提示先 assemble_code_context, 本策略是可翻 enforce 的确定性接缝。
    """
    if name in _STRUCTURAL_TOOLS:
        return (
            "structural query: prefer assemble_code_context first "
            "(confidence-tagged dependency graph) before raw grep/read"
        )
    return None


def _allowlist() -> set[str]:
    """豁免工具名集合 (env ``VEYA_TOOL_GATE_ALLOWLIST``, 逗号分隔)。

    本仓 omodul 有 74+ 合法业务工具名含 delete/publish/cancel 等 terminal 词
    (delete_product / publish_products_to_channel ...) —— 它们本就该被主脑调用。
    enforce 前用 allowlist 把这些豁免掉, 才能只拦真正不可逆的外部/基建动作。
    """
    raw = os.environ.get("VEYA_TOOL_GATE_ALLOWLIST", "")
    return {t.strip() for t in raw.split(",") if t.strip()}


async def terminal_action_policy(name: str, kwargs: dict, source: str) -> str | None:
    """命中 terminal/不可逆动作 → 返回拒绝原因; 否则 None (放行)。

    **只按工具名分类** (不掺入参数值 —— 良性参数如 url/path 里的 'delete' 字样会
    造成误伤)。allowlist 中的工具直接放行。分类失败一律 fail-open (返回 None)。
    """
    if name in _allowlist():
        return None
    from server.state_kernel import terminal_gate_check

    try:
        verdict = json.loads(await terminal_gate_check(name))
    except Exception:
        return None
    if verdict.get("requires_approval"):
        return str(verdict.get("reason") or "terminal/irreversible action requires human approval")
    return None


def egress_audit_policy(name: str, kwargs: dict, source: str) -> str | None:
    """Outbound tools: always append a hash-chain record; optionally deny off-allowlist.

    Default observe (record, never deny). ``VEYA_EGRESS_ENFORCE=1`` plus
    ``VEYA_EGRESS_ALLOWLIST`` (comma hosts) blocks destinations not on the list.
    """
    from server.egress_audit import (
        destination_allowed,
        destination_of,
        digest_of,
        record_egress,
    )

    dest = destination_of(name, kwargs or {})
    if dest is None:
        return None
    owner = ""
    try:
        from server.auth import current_user

        owner = str(current_user().get("user_id") or "")
    except Exception:
        owner = ""
    try:
        record_egress(
            tool=name,
            destination=dest,
            digest=digest_of(kwargs or {}),
            owner_id=owner,
            source=source,
        )
    except Exception:
        pass
    enforce = os.environ.get("VEYA_EGRESS_ENFORCE", "0") == "1"
    if enforce and not destination_allowed(dest):
        return f"egress denied: {dest} not on VEYA_EGRESS_ALLOWLIST"
    return None


def install_default_tool_policies(guard: ToolGuard | None = None) -> None:
    """幂等安装默认守卫策略。缺省 observe 模式 (VEYA_TOOL_GATE_ENFORCE=1 翻 enforce)。"""
    guard = guard or global_tool_guard
    enforce = os.environ.get("VEYA_TOOL_GATE_ENFORCE", "0") == "1"
    if not guard.has_policy(_TERMINAL_POLICY):
        guard.register_policy(_TERMINAL_POLICY, terminal_action_policy, enforce=enforce)
    # 先查图钩子: 默认 observe (零行为变化); VEYA_PREFER_GRAPH_ENFORCE=1 才回喂主脑改走图。
    if not guard.has_policy(_PREFER_GRAPH_POLICY):
        graph_enforce = os.environ.get("VEYA_PREFER_GRAPH_ENFORCE", "0") == "1"
        guard.register_policy(_PREFER_GRAPH_POLICY, prefer_code_graph_policy, enforce=graph_enforce)
    if not guard.has_policy(_EGRESS_POLICY):
        egress_enforce = os.environ.get("VEYA_EGRESS_ENFORCE", "0") == "1"
        guard.register_policy(_EGRESS_POLICY, egress_audit_policy, enforce=egress_enforce)

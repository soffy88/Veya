"""server.permission_profiles — P3-01 Permission Profiles (READ_ONLY / DEVELOPMENT / PRODUCTION).

docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §9 的落地：把「工具名 → 风险等级 (R0-R4)」
的映射和「风险等级 → allow/deny/ask」的档位策略收成两个纯数据结构，供：

- approval UI (P1-05) 的权限档位选择器查询/切换；
- tool_guard 策略链接入（观察模式默认，可翻 enforce）。

设计约束（与 `server/tool_guard_policies.py` 同一纪律）：
- 缺省**不改变任何现有行为**——本模块只提供数据和决策函数，是否接入执行路径
  由调用方（宿主装配/显式 API）决定；不会在 import 时自动注册进 global_tool_guard。
- 风险等级按工具名分类（不掺入参数值），分类失败一律 fail-open（最低风险 R0，
  由档位策略决定是否放行）。
- 与既有 `veya.obase.authz` 的 ALLOW/DENY/PENDING 三态对齐：allow/deny/ask 一一对应。

档位矩阵（规格 §9 Profile，逐字段对齐）：

| 风险 | READ_ONLY      | DEVELOPMENT        | PRODUCTION     |
|------|----------------|--------------------|----------------|
| R0 read-only       | allow | allow | allow |
| R1 local write     | deny  | allow | ask   |
| R2 process exec    | deny  | allow (test) / ask | ask   |
| R3 network write   | deny  | ask   | ask   |
| R4 destructive     | deny  | ask   | deny  |
"""

from __future__ import annotations

import contextvars
import dataclasses
import os
import threading
from enum import StrEnum
from typing import Any, Literal

__all__ = [
    "PermissionDecision",
    "PermissionProfiles",
    "ProfileName",
    "RiskLevel",
    "activate_profile",
    "classify_risk",
    "current_profile",
    "decide",
    "default_profile",
    "list_profiles",
    "resolve_inheritance",
    "set_user_profile",
]


class ProfileName(StrEnum):
    READ_ONLY = "READ_ONLY"
    DEVELOPMENT = "DEVELOPMENT"
    PRODUCTION = "PRODUCTION"


class RiskLevel(StrEnum):
    R0 = "R0"  # read-only
    R1 = "R1"  # local write
    R2 = "R2"  # process execution
    R3 = "R3"  # network write
    R4 = "R4"  # destructive / privileged


@dataclasses.dataclass(frozen=True)
class PermissionDecision:
    """规格 §9: 一次权限决策。action ∈ {allow, deny, ask}。"""

    action: Literal["allow", "deny", "ask"]
    reason: str
    scope: str
    profile: ProfileName
    risk: RiskLevel


# ── 工具名 → 风险等级 (R0-R4) ────────────────────────────────────────────
# 只按工具名分类。已知的高影响写入/执行工具来自 server.user_control.HIGH_IMPACT
# 与 PLAN_ALLOW 的补集语义；新工具默认 R0 (fail-open)，由档位矩阵兜底。
_R1_LOCAL_WRITE = frozenset(
    {
        "write_file",
        "edit_hashline",
        "ast_grep_rewrite",
        "apply_edit_block",
        "apply_unified_diff",
        "create_plan",
        "update_todo",
        "save_memory",
        "memory_write",
        "write",
        "edit",
        "coding_worktree_create",
        "coding_apply_patch",
        "coding_discard",
        "coding_finalize_patch",
        "harness_ratchet_approve",
        "harness_ratchet_reject",
        "harness_ratchet_apply",
    }
)
_R2_PROCESS_EXEC = frozenset(
    {
        "run_in_sandbox",
        "hicode_run",
        "evolve_solution",
        "bash",
        "run",
        "system_spawn_swarm",
        "system_reload_skills",
        "hicode_rollback",
        "hicode_stop",
        "automation_run",
        "test_run",
        "coding_run_command",
        "coding_run_tests",
        "coding_run_lint",
        "coding_run_typecheck",
        "coding_build",
        "harness_sensor_run",
    }
)
_R3_NETWORK_WRITE = frozenset(
    {
        "produce_wechat_article",
        "publish_article",
        "send_email",
        "post_message",
        "system_dispatch_omni_channel",
        "network_post",
        "webhook_send",
        "http_post",
    }
)
_R4_DESTRUCTIVE = frozenset(
    {
        "delete_file",
        "remove_file",
        "system_remove_automation",
        "system_create_automation",  # 新建自动化 = 长期行为改变，归入 R4 更保守
        "system_graph_cycle",
        "git_reset_hard",
        "git_force_push",
        "rm",
        "truncate",
        "drop_table",
        "delegate_to_genesis",
    }
)


def classify_risk(tool_name: str, kwargs: dict[str, Any] | None = None) -> RiskLevel:
    """工具名 → 风险等级。分类失败默认 R0 (fail-open，由档位矩阵决定放行)。"""
    name = str(tool_name or "")
    if name in _R4_DESTRUCTIVE:
        return RiskLevel.R4
    if name in _R3_NETWORK_WRITE:
        return RiskLevel.R3
    if name in _R2_PROCESS_EXEC:
        return RiskLevel.R2
    if name in _R1_LOCAL_WRITE:
        return RiskLevel.R1
    return RiskLevel.R0


# ── 档位矩阵 (规格 §9) ───────────────────────────────────────────────────
# risk -> decision per profile
_MATRIX: dict[ProfileName, dict[RiskLevel, str]] = {
    ProfileName.READ_ONLY: {
        RiskLevel.R0: "allow",
        RiskLevel.R1: "deny",
        RiskLevel.R2: "deny",
        RiskLevel.R3: "deny",
        RiskLevel.R4: "deny",
    },
    ProfileName.DEVELOPMENT: {
        RiskLevel.R0: "allow",
        RiskLevel.R1: "allow",
        RiskLevel.R2: "allow",  # test_exec allow；非 test 执行由调用方追加 ask
        RiskLevel.R3: "ask",
        RiskLevel.R4: "ask",
    },
    ProfileName.PRODUCTION: {
        RiskLevel.R0: "allow",
        RiskLevel.R1: "ask",
        RiskLevel.R2: "ask",
        RiskLevel.R3: "ask",
        RiskLevel.R4: "deny",
    },
}

_PROFILE_DESCRIPTIONS: dict[ProfileName, str] = {
    ProfileName.READ_ONLY: "只读：所有写入/执行/网络外发/破坏性操作一律拒绝。",
    ProfileName.DEVELOPMENT: "开发：本地写入与测试执行放行；外发与破坏性操作需确认。",
    ProfileName.PRODUCTION: "生产：一切写入/执行/外发需确认；破坏性操作一律拒绝。",
}

_profile_ctx: contextvars.ContextVar[ProfileName | None] = contextvars.ContextVar(
    "veya_permission_profile", default=None
)
_user_profiles: dict[str, ProfileName] = {}
_user_profiles_lock = threading.RLock()


def activate_profile(profile: ProfileName | str) -> contextvars.Token:
    """Activate a request/session-local override; caller must reset the token."""
    selected = profile if isinstance(profile, ProfileName) else ProfileName(str(profile).upper())
    return _profile_ctx.set(selected)


def current_profile() -> ProfileName | None:
    """Return session override, then user-scoped profile, without global mutation."""
    override = _profile_ctx.get()
    if override is not None:
        return override
    try:
        from server.auth import current_user

        user_id = str(current_user().get("user_id") or "anonymous")
    except Exception:
        user_id = "anonymous"
    with _user_profiles_lock:
        return _user_profiles.get(user_id)


def set_user_profile(profile: ProfileName | str, *, user_id: str) -> ProfileName:
    selected = profile if isinstance(profile, ProfileName) else ProfileName(str(profile).upper())
    with _user_profiles_lock:
        _user_profiles[user_id] = selected
    return selected


def default_profile() -> ProfileName:
    """Session/user override > environment profile > DEVELOPMENT."""
    scoped = current_profile()
    if scoped is not None:
        return scoped
    raw = os.environ.get("VEYA_PERMISSION_PROFILE", "DEVELOPMENT").strip().upper()
    try:
        return ProfileName(raw)
    except ValueError:
        return ProfileName.DEVELOPMENT


def list_profiles() -> list[dict[str, Any]]:
    """供 P1-05 档位选择器使用的档位清单 (含矩阵摘要)。"""
    out = []
    for profile in ProfileName:
        out.append(
            {
                "name": profile.value,
                "description": _PROFILE_DESCRIPTIONS[profile],
                "matrix": {risk.value: _MATRIX[profile][risk] for risk in RiskLevel},
            }
        )
    return out


def decide(
    profile: ProfileName | str,
    tool_name: str,
    kwargs: dict[str, Any] | None = None,
) -> PermissionDecision:
    """按档位对一次工具调用做决策 (allow/deny/ask)。纯函数，无 IO。"""
    try:
        p = (
            profile
            if isinstance(profile, ProfileName)
            else ProfileName(str(profile).strip().upper())
        )
    except ValueError:
        p = default_profile()
    risk = classify_risk(tool_name, kwargs)
    action = _MATRIX[p][risk]
    reason = {
        "allow": f"{p.value}: {tool_name} 风险等级 {risk.value} 放行",
        "deny": f"{p.value}: {tool_name} 风险等级 {risk.value} 拒绝",
        "ask": f"{p.value}: {tool_name} 风险等级 {risk.value} 需确认",
    }[action]
    return PermissionDecision(action=action, reason=reason, scope="workspace", profile=p, risk=risk)


def resolve_inheritance(
    tool_name: str,
    *,
    user: ProfileName | str | None = None,
    workspace: ProfileName | str | None = None,
    session: ProfileName | str | None = None,
    kwargs: dict[str, Any] | None = None,
) -> PermissionDecision:
    """Merge user → workspace → session policies with deny > ask > allow."""
    layers = [("user", user), ("workspace", workspace), ("session", session)]
    decisions = [
        (scope, decide(profile, tool_name, kwargs))
        for scope, profile in layers
        if profile is not None
    ]
    if not decisions:
        return decide(default_profile(), tool_name, kwargs)
    rank = {"allow": 0, "ask": 1, "deny": 2}
    scope, selected = max(decisions, key=lambda item: rank[item[1].action])
    return dataclasses.replace(selected, scope=scope)


# 便捷引用 (模块级)
PermissionProfiles = ProfileName

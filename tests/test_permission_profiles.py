"""server.permission_profiles + P3-01 档位 API 测试 (docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §9/§31)。

档位矩阵逐字段对照规格 §9 Profile：
    READ_ONLY:   read=allow, write=deny, exec=deny, network_write=deny, destructive=deny
    DEVELOPMENT: read=allow, workspace_write=allow, test_exec=allow,
                 external_write=ask, destructive=ask
    PRODUCTION:  read=allow, write=ask, exec=ask, external_write=ask, destructive=deny
"""

from __future__ import annotations

from server.permission_profiles import (
    ProfileName,
    RiskLevel,
    classify_risk,
    decide,
    default_profile,
    list_profiles,
    resolve_inheritance,
)

# ── 风险等级分类 ─────────────────────────────────────────────────────────


def test_classify_risk_by_tool_name():
    assert classify_risk("grep") == RiskLevel.R0
    assert classify_risk("write_file") == RiskLevel.R1
    assert classify_risk("bash") == RiskLevel.R2
    assert classify_risk("produce_wechat_article") == RiskLevel.R3
    assert classify_risk("delete_file") == RiskLevel.R4


def test_classify_risk_unknown_tool_fail_open_r0():
    assert classify_risk("some_brand_new_tool") == RiskLevel.R0


# ── READ_ONLY 档位矩阵 ───────────────────────────────────────────────────


def test_read_only_denies_write_exec_network_destructive():
    for tool in ("write_file", "bash", "produce_wechat_article", "delete_file"):
        d = decide(ProfileName.READ_ONLY, tool)
        assert d.action == "deny", (tool, d)
    assert decide(ProfileName.READ_ONLY, "grep").action == "allow"


# ── DEVELOPMENT 档位矩阵 ─────────────────────────────────────────────────


def test_development_allows_local_write_and_exec():
    assert decide(ProfileName.DEVELOPMENT, "write_file").action == "allow"
    assert decide(ProfileName.DEVELOPMENT, "bash").action == "allow"


def test_development_asks_external_write_and_destructive():
    assert decide(ProfileName.DEVELOPMENT, "produce_wechat_article").action == "ask"
    assert decide(ProfileName.DEVELOPMENT, "delete_file").action == "ask"


# ── PRODUCTION 档位矩阵 ──────────────────────────────────────────────────


def test_production_asks_write_exec_network_and_denies_destructive():
    for tool in ("write_file", "bash", "produce_wechat_article"):
        assert decide(ProfileName.PRODUCTION, tool).action == "ask", tool
    assert decide(ProfileName.PRODUCTION, "delete_file").action == "deny"
    assert decide(ProfileName.PRODUCTION, "grep").action == "allow"


# ── 档位枚举与默认值 ─────────────────────────────────────────────────────


def test_profiles_list_has_three_profiles():
    profiles = list_profiles()
    assert [p["name"] for p in profiles] == ["READ_ONLY", "DEVELOPMENT", "PRODUCTION"]
    for p in profiles:
        assert set(p["matrix"].keys()) == {r.value for r in RiskLevel}


def test_default_profile_env_override(monkeypatch):
    assert default_profile() == ProfileName.DEVELOPMENT
    monkeypatch.setenv("VEYA_PERMISSION_PROFILE", "PRODUCTION")
    assert default_profile() == ProfileName.PRODUCTION
    monkeypatch.setenv("VEYA_PERMISSION_PROFILE", "BOGUS")
    assert default_profile() == ProfileName.DEVELOPMENT  # 非法值回退


def test_decide_invalid_profile_falls_back(monkeypatch):
    monkeypatch.setenv("VEYA_PERMISSION_PROFILE", "DEVELOPMENT")
    d = decide("BOGUS", "grep")
    assert d.profile == ProfileName.DEVELOPMENT


def test_inheritance_uses_deny_over_ask_over_allow():
    decision = resolve_inheritance(
        "write_file",
        user=ProfileName.DEVELOPMENT,
        workspace=ProfileName.PRODUCTION,
        session=ProfileName.DEVELOPMENT,
    )
    assert decision.action == "ask"
    assert decision.scope == "workspace"
    decision = resolve_inheritance(
        "delete_file",
        user=ProfileName.DEVELOPMENT,
        workspace=ProfileName.PRODUCTION,
        session=ProfileName.DEVELOPMENT,
    )
    assert decision.action == "deny"


# ── tool_guard 策略接入 (observe 默认, 零行为变化) ───────────────────────


def test_permission_profile_policy_observe_never_denies(monkeypatch):
    """默认 (VEYA_PERMISSION_PROFILE_ENFORCE != 1) 观察模式: 即使档位判 deny,
    也不拦截任何工具 —— 满足安全灰度「零行为变化」原则。"""
    from server.tool_guard import ToolGuard
    from server.tool_guard_policies import install_default_tool_policies

    monkeypatch.delenv("VEYA_TOOL_GATE_ENFORCE", raising=False)
    monkeypatch.delenv("VEYA_PERMISSION_PROFILE_ENFORCE", raising=False)
    monkeypatch.setenv("VEYA_PERMISSION_PROFILE", "READ_ONLY")

    guard = ToolGuard()
    install_default_tool_policies(guard)
    # observe: check() 不 raise
    guard.check("write_file", {})  # READ_ONLY 判 deny, 但 observe 只记录
    guard.check("delete_file", {})
    assert "permission_profile_gate" in guard.policy_names


def test_permission_profile_policy_fn_returns_reason_for_deny(monkeypatch):
    from server.tool_guard_policies import permission_profile_policy

    monkeypatch.setenv("VEYA_PERMISSION_PROFILE", "READ_ONLY")
    reason = permission_profile_policy("write_file", {}, "test")
    assert reason is not None and "R1" in reason
    assert permission_profile_policy("grep", {}, "test") is None

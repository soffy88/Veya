"""统一工具守卫通道 (server.tool_guard) — 主脑一切工具执行的确定性第一道闸。

覆盖四原则约束:
1. 缺省无策略 → 全放行, 对现有工具零行为变化 (功能至上/质量为王), 并留 allow 轨迹。
2. enforce 策略命中 → MasterToolRegistry.execute / VeyaSkillHub.execute 抛
   ToolExecutionError (回喂主脑反思), 留 deny 轨迹 (体验优先/取证)。
3. observe 策略命中 → 不拦截, 只在 allow 轨迹夹带 observed (安全灰度采样)。
4. async 策略经 acheck 生效; 策略自身异常 → fail-open (工具照常执行)。
"""

from __future__ import annotations

import pytest

from server.skill_hub import VeyaSkillHub
from server.tool_guard import ToolDenied, ToolGuard, global_tool_guard
from server.tool_registry import MasterToolRegistry, ToolExecutionError


@pytest.fixture(autouse=True)
def _clean_guard():
    global_tool_guard.clear_policies()
    global_tool_guard._trail.clear()
    yield
    global_tool_guard.clear_policies()
    global_tool_guard._trail.clear()


@pytest.fixture
def registry():
    reg = MasterToolRegistry()
    reg.register(
        "echo", "echo back x", {"properties": {"x": {"type": "string"}}}, lambda x: f"got {x}"
    )
    return reg


# ── 1. 缺省全放行 + allow 轨迹 ────────────────────────────────────────
@pytest.mark.asyncio
async def test_default_allows_and_records_trail(registry):
    result = await registry.execute("echo", {"x": "hi"})
    assert result == "got hi"  # 零行为变化
    tail = global_tool_guard.trail()[-1]
    assert tail["tool"] == "echo"
    assert tail["decision"] == "allow"
    assert tail["source"] == "master_tool"


# ── 2. enforce 策略拦截 static tool ──────────────────────────────────
@pytest.mark.asyncio
async def test_enforce_policy_blocks_master_tool(registry):
    global_tool_guard.register_policy(
        "no_echo",
        lambda name, kw, src: "echo forbidden" if name == "echo" else None,
        enforce=True,
    )
    with pytest.raises(ToolExecutionError) as ei:
        await registry.execute("echo", {"x": "hi"})
    assert "denied by policy 'no_echo'" in str(ei.value)
    assert "echo forbidden" in str(ei.value)
    tail = global_tool_guard.trail()[-1]
    assert tail["decision"] == "deny"
    assert tail["policy"] == "no_echo"


# ── 3. observe 策略命中不拦截, 只记 observed (安全灰度) ────────────────
@pytest.mark.asyncio
async def test_observe_policy_records_but_allows(registry):
    global_tool_guard.register_policy(
        "watch_echo", lambda name, kw, src: "would-deny echo" if name == "echo" else None
    )  # enforce 缺省 False
    result = await registry.execute("echo", {"x": "hi"})
    assert result == "got hi"  # observe 不改变行为
    tail = global_tool_guard.trail()[-1]
    assert tail["decision"] == "allow"
    assert tail["observed"] == [{"policy": "watch_echo", "reason": "would-deny echo"}]


# ── 4. enforce 在 skill 查找之前生效 (动态技能同样收口) ───────────────
@pytest.mark.asyncio
async def test_enforce_policy_blocks_skill_hub_before_lookup(tmp_path):
    hub = VeyaSkillHub(skills_dir=tmp_path)  # 空技能目录
    global_tool_guard.register_policy("deny_all", lambda n, k, s: "locked down", enforce=True)
    with pytest.raises(ToolExecutionError) as ei:
        await hub.execute("whatever_skill", {})
    assert "denied by policy 'deny_all'" in str(ei.value)
    tail = global_tool_guard.trail()[-1]
    assert tail["decision"] == "deny"
    assert tail["source"] == "skill_hub"


# ── 5. async 策略经 acheck (execute 路径) 生效 ───────────────────────
@pytest.mark.asyncio
async def test_async_enforce_policy(registry):
    async def deny_async(name, kw, src):
        return "async says no" if name == "echo" else None

    global_tool_guard.register_policy("async_deny", deny_async, enforce=True)
    with pytest.raises(ToolExecutionError) as ei:
        await registry.execute("echo", {"x": "hi"})
    assert "async says no" in str(ei.value)


# ── 6. 策略异常 fail-open, 工具照常执行 ───────────────────────────────
@pytest.mark.asyncio
async def test_policy_exception_fails_open(registry):
    def boom(name, kw, src):
        raise RuntimeError("policy has a bug")

    global_tool_guard.register_policy("boom", boom, enforce=True)
    result = await registry.execute("echo", {"x": "ok"})
    assert result == "got ok"  # 守卫 bug 不拦截工具
    assert global_tool_guard.trail()[-1]["decision"] == "allow"


# ── 7. ToolGuard 单元: 多策略, 第一条 enforce 拒绝即短路 ──────────────
def test_guard_unit_first_deny_short_circuits():
    g = ToolGuard()
    seen: list[str] = []
    g.register_policy("a", lambda n, k, s: (seen.append("a"), None)[1])
    g.register_policy("b", lambda n, k, s: (seen.append("b"), "nope")[1], enforce=True)
    g.register_policy("c", lambda n, k, s: (seen.append("c"), None)[1])
    with pytest.raises(ToolDenied):
        g.check("t", {}, source="unit")
    assert seen == ["a", "b"]  # c 未被执行 (短路)

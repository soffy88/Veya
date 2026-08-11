"""默认工具守卫策略 (server.tool_guard_policies) — terminal 动作闸门。

- 只按工具名分类 (delete_product 命中, read_file 不命中, url 参数不误伤);
- allowlist 豁免 (业务工具名含 terminal 词但合法);
- 安装缺省 observe (VEYA_TOOL_GATE_ENFORCE=1 翻 enforce)。
"""

from __future__ import annotations

import pytest

from server.tool_guard import ToolGuard
from server.tool_guard_policies import (
    _TERMINAL_POLICY,
    install_default_tool_policies,
    terminal_action_policy,
)


@pytest.mark.asyncio
async def test_terminal_classified_by_name():
    assert await terminal_action_policy("delete_product", {}, "master_tool") is not None
    assert (
        await terminal_action_policy("publish_products_to_channel", {}, "master_tool") is not None
    )
    assert await terminal_action_policy("read_file", {}, "master_tool") is None


@pytest.mark.asyncio
async def test_benign_arg_does_not_false_positive():
    # url 里含 'delete' 字样不应把良性 fetch 判为 terminal (只看工具名)
    assert await terminal_action_policy("fetch_url", {"url": "https://x/delete-guide"}, "s") is None


@pytest.mark.asyncio
async def test_allowlist_exempts(monkeypatch):
    monkeypatch.setenv("VEYA_TOOL_GATE_ALLOWLIST", "delete_product, cancel_order")
    assert await terminal_action_policy("delete_product", {}, "master_tool") is None
    assert await terminal_action_policy("cancel_order", {}, "master_tool") is None
    # 不在 allowlist 的仍命中
    assert await terminal_action_policy("deploy_app", {}, "master_tool") is not None


def test_install_defaults_observe_mode(monkeypatch):
    monkeypatch.delenv("VEYA_TOOL_GATE_ENFORCE", raising=False)
    g = ToolGuard()
    install_default_tool_policies(g)
    assert g.has_policy(_TERMINAL_POLICY)
    # 幂等: 二次安装不重复
    install_default_tool_policies(g)
    assert g.policy_names.count(_TERMINAL_POLICY) == 1
    # 缺省 observe → 命中不拦截 (check 不抛)
    g.clear_policies()


def test_install_enforce_via_env(monkeypatch):
    monkeypatch.setenv("VEYA_TOOL_GATE_ENFORCE", "1")
    g = ToolGuard()
    install_default_tool_policies(g)
    # enforce 模式下, terminal 工具应被拦 (通过 acheck)
    import asyncio

    from server.tool_guard import ToolDenied

    with pytest.raises(ToolDenied):
        asyncio.run(g.acheck("deploy_app", {}, source="master_tool"))

"""Omni-Channel Gateway 测试 — 适配器规范 / RPA 降级 / 网关扇出 / 主脑接线。"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.channels.adapters import ChannelAdapter, FeishuAdapter, SocialMediaRPAAdapter
from server.omni_gateway import DISPATCH_TOOL_NAME, OmniChannelGateway

# =========================================================================
# 一、适配器规范 (Adapter Pattern)
# =========================================================================


def test_adapter_contract():
    """ChannelAdapter 抽象接口: push 必须由子类实现。"""
    assert ChannelAdapter.__abstractmethods__ == {"push"}


@pytest.mark.asyncio
async def test_feishu_adapter_push_format(monkeypatch):
    """FeishuAdapter: 官方 API 渠道, 组装飞书卡片消息。"""
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, **kw):
            captured["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    adapter = FeishuAdapter("https://open.feishu.cn/hook/x")
    out = await adapter.push("今天 AI 板块异动", payload={"title": "复盘日报"})

    assert captured["url"] == "https://open.feishu.cn/hook/x"
    assert captured["json"]["msg_type"] == "post"
    zh = captured["json"]["content"]["post"]["zh_cn"]
    assert zh["title"] == "复盘日报"
    assert zh["content"][0][0]["text"] == "今天 AI 板块异动"
    assert "飞书" in out


@pytest.mark.asyncio
async def test_feishu_adapter_missing_webhook():
    """未配置 webhook → 明确报错(不静默吞掉)。"""
    adapter = FeishuAdapter(None)
    with pytest.raises(RuntimeError, match="FEISHU_WEBHOOK"):
        await adapter.push("x")


@pytest.mark.asyncio
async def test_rpa_adapter_fallback(monkeypatch):
    """RPA 降级: 无 API 平台 → 拉起真实 Playwright 引擎执行发布指令。"""
    captured = {}

    class FakeResult:
        success = True
        steps = 3
        error = ""

    class FakeAgent:
        def __init__(self, cfg):
            captured["cfg"] = cfg

        async def run_task(self, url, instruction):
            captured["url"] = url
            captured["instruction"] = instruction
            return FakeResult()

    monkeypatch.setattr("veya.omodul.browser_agent.BrowserAgent", FakeAgent)
    monkeypatch.setattr("veya.omodul.browser_agent.BrowserTaskConfig", lambda: "CFG")

    adapter = SocialMediaRPAAdapter(
        "小红书", "https://creator.xiaohongshu.com/publish/publish"
    )
    out = await adapter.push("复盘内容", payload={"title": "t", "image_path": "/tmp/pic.png"})

    assert captured["cfg"] == "CFG"
    assert captured["url"] == "https://creator.xiaohongshu.com/publish/publish"
    assert "复盘内容" in captured["instruction"]
    assert "/tmp/pic.png" in captured["instruction"]  # 图片路径注入发布指令
    assert "小红书" in out
    assert "3" in out  # steps 回显


@pytest.mark.asyncio
async def test_rpa_adapter_failure_raises(monkeypatch):
    """RPA 执行失败 → 抛错让网关捕获上报(不伪装成功)。"""

    class FakeResult:
        success = False
        steps = 0
        error = "login timeout"

    class FakeAgent:
        def __init__(self, cfg):
            pass

        async def run_task(self, url, instruction):
            return FakeResult()

    monkeypatch.setattr("veya.omodul.browser_agent.BrowserAgent", FakeAgent)
    monkeypatch.setattr("veya.omodul.browser_agent.BrowserTaskConfig", lambda: "CFG")

    adapter = SocialMediaRPAAdapter("X (Twitter)", "https://x.com")
    with pytest.raises(RuntimeError, match="login timeout"):
        await adapter.push("x")


# =========================================================================
# 二、全渠道网关 (Omni-Gateway Core)
# =========================================================================


def test_gateway_channels_and_schema():
    """预注册渠道 + 统一 LLM schema(目标枚举与渠道一致)。"""
    gw = OmniChannelGateway()
    assert set(gw.channels) == {"feishu_workgroup", "xiaohongshu_official", "x_twitter"}

    schema = gw.get_llm_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == DISPATCH_TOOL_NAME
    enum = fn["parameters"]["properties"]["targets"]["items"]["enum"]
    assert set(enum) == set(gw.channels)
    assert fn["parameters"]["required"] == ["targets", "title", "content"]


@pytest.mark.asyncio
async def test_gateway_dispatch_fanout(monkeypatch):
    """并发扇出: 每个渠道都收到内容, 报告含逐渠道结果。"""
    gw = OmniChannelGateway()
    calls = []

    async def fake_push(content, payload=None):
        await asyncio.sleep(0.01)  # 模拟 IO, 验证并发不串扰
        calls.append((content, payload))
        return "OK"

    for adapter in gw.channels.values():
        monkeypatch.setattr(adapter, "push", fake_push)

    report = await gw.execute_dispatch(list(gw.channels), "标题", "内容")

    assert len(calls) == 3
    assert all(c[0] == "内容" and c[1]["title"] == "标题" for c in calls)
    for name in gw.channels:
        assert f"[{name}]" in report


@pytest.mark.asyncio
async def test_gateway_unregistered_channel(monkeypatch):
    """未注册渠道: 不中断其他渠道, 报告标记失败。"""
    gw = OmniChannelGateway()
    for adapter in gw.channels.values():
        monkeypatch.setattr(adapter, "push", _fake_ok)

    report = await gw.execute_dispatch(["feishu_workgroup", "ghost_platform"], "t", "c")
    assert "[feishu_workgroup]" in report
    assert "ghost_platform" in report
    assert "❌" in report


async def _fake_ok(content, payload=None):
    return "OK"


# =========================================================================
# 三、主脑接线 (缺口 A: coordinator_master → system_dispatch_omni_channel)
# =========================================================================


def test_master_brain_has_dispatch_tool():
    """主脑工具清单含 system_dispatch_omni_channel, 且网关已注入。"""
    from server.coordinator_master import MasterCoordinator

    coord = MasterCoordinator(llm_fn=lambda **kw: {})
    names = [s["function"]["name"] for s in coord.get_all_tool_schemas()]
    assert DISPATCH_TOOL_NAME in names
    assert coord._agent.omni_gateway is not None


def test_master_prompt_has_distribution():
    """SOP 含 DISTRIBUTION 指令: 模型知道何时调用分发工具。"""
    from server.coordinator_master import MASTER_SYSTEM_PROMPT

    assert "# DISTRIBUTION" in MASTER_SYSTEM_PROMPT
    assert "system_dispatch_omni_channel" in MASTER_SYSTEM_PROMPT
    assert "RPA browser automation" in MASTER_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_master_dispatch_via_handle_tool_call():
    """handle_tool_call 拦截分发: 意图参数透传网关。"""
    from server.coordinator_master import MasterCoordinator

    coord = MasterCoordinator(llm_fn=lambda **kw: {})
    captured = {}

    class FakeGateway:
        def get_llm_schema(self):
            return {"type": "function", "function": {"name": DISPATCH_TOOL_NAME}}

        async def execute_dispatch(self, targets, title, content):
            captured.update(targets=targets, title=title, content=content)
            return "DISPATCHED"

    coord._agent.omni_gateway = FakeGateway()  # 测试注入替身
    out = await coord._agent.handle_tool_call(
        DISPATCH_TOOL_NAME,
        {"targets": ["feishu_workgroup", "x_twitter"], "title": "t", "content": "c"},
    )
    assert out == "DISPATCHED"
    assert captured == {"targets": ["feishu_workgroup", "x_twitter"], "title": "t", "content": "c"}


@pytest.mark.asyncio
async def test_master_dispatch_without_gateway():
    """网关未装配: 明确报错而非崩溃。"""
    from server.coordinator_master import MasterCoordinator

    coord = MasterCoordinator(llm_fn=lambda **kw: {})
    coord._agent.omni_gateway = None
    out = await coord._agent.handle_tool_call(DISPATCH_TOOL_NAME, {"targets": []})
    assert "未装配" in out


@pytest.mark.asyncio
async def test_full_loop_master_dispatch(tmp_path, monkeypatch):
    """完整 ReAct 闭环: 模型决定分发 → 工具执行 → 结果回喂 → 最终回答。"""
    from server.coordinator_master import MasterCoordinator
    from server.memory_bank import VeyaMemoryBank

    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_dispatch",
                                    "type": "function",
                                    "function": {
                                        "name": DISPATCH_TOOL_NAME,
                                        "arguments": json.dumps(
                                            {"targets": ["feishu_workgroup"], "title": "复盘", "content": "AI 板块异动"}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            }
        return {
            "choices": [{"message": {"role": "assistant", "content": "已分发到飞书。"}}],
            "usage": {},
        }

    coord = MasterCoordinator(memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"), llm_fn=fake_llm, max_rounds=3)

    captured = {}

    class FakeGateway:
        def get_llm_schema(self):
            return {"type": "function", "function": {"name": DISPATCH_TOOL_NAME}}

        async def execute_dispatch(self, targets, title, content):
            captured.update(targets=targets, title=title, content=content)
            return "Omni-Channel Dispatch Report:\n- [feishu_workgroup]: ✅ 已成功分发至飞书群组。"

    coord._agent.omni_gateway = FakeGateway()
    result = await coord.chat_stream("把复盘发到飞书", session_id="omni_loop")

    assert result["status"] == "success"
    assert result["tool_calls"] == [{"tool": DISPATCH_TOOL_NAME, "status": "success"}]
    assert captured == {"targets": ["feishu_workgroup"], "title": "复盘", "content": "AI 板块异动"}
    # 网关报告回喂给模型(第二轮消息含渠道结果)
    assert "飞书群组" in calls[1][-1]["content"]
    assert "已分发到飞书" in result["final_answer"]

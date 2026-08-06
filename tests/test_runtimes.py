"""三框架运行时门禁 — AgentRuntime 协议 / 三适配器 / 注册 / 探活。

PRD: docs/prd/AGENT_RUNTIMES_PRD.md (L1→L3 独立可交付)。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform" / "3O" / "obase"))

from obase.agent_registry import AgentRegistry

from server.runtimes import (
    agentscope_bridge,
    pi_bridge,
    prime_agent_runtime,
    register_all_runtimes,
    runtime_status,
)

# =========================================================================
# 注册 (runtime 类型 + 幂等)
# =========================================================================

def test_runtime_type_in_agent_registry():
    """agent_registry 已扩展 runtime 类型 (主库改动)。"""
    from obase.agent_registry import REGISTRY_TYPES

    assert "runtime" in REGISTRY_TYPES


def test_register_all_runtimes_idempotent():
    reg = AgentRegistry()
    r1 = register_all_runtimes(reg)
    assert set(r1["registered"]) == {"prime_agent_runtime", "pi_bridge", "agentscope_bridge"}

    r2 = register_all_runtimes(reg)
    assert r2["registered"] == []
    assert set(r2["skipped"]) == set(r1["registered"])

    # 可从账本查询
    entry = reg.get("runtime", "pi_bridge")
    assert entry is not None and hasattr(entry["func"], "dispatch")  # 协议实例


def test_runtime_status_shape():
    """探活: 每项含 name/ok; 不崩溃。"""
    statuses = runtime_status()
    assert len(statuses) == 3
    names = {s["name"] for s in statuses}
    assert names == {"prime_agent_runtime", "pi_bridge", "agentscope_bridge"}


# =========================================================================
# L1 — prime-agent (未接入 → 结构化)
# =========================================================================

@pytest.mark.asyncio
async def test_prime_agent_unavailable_structured(monkeypatch):
    monkeypatch.delenv("PRIME_AGENT_MODULE", raising=False)
    r = await prime_agent_runtime.init()
    assert r["ok"] is False and "prime-agent 未接入" in r["error"]

    d = await prime_agent_runtime.dispatch("task")
    assert d["ok"] is False

    h = await prime_agent_runtime.health()
    assert h["ok"] is False


# =========================================================================
# L2 — pi bridge (真实 CLI 探测)
# =========================================================================

@pytest.mark.asyncio
async def test_pi_bridge_init_and_health():
    import shutil

    if shutil.which("pi") is None:
        pytest.skip("pi CLI 未安装")

    r = await pi_bridge.init()
    assert r["ok"] is True
    assert r["version"]

    h = await pi_bridge.health()
    assert h["ok"] is True
    assert h["bin"] and "pi" in h["bin"]


@pytest.mark.asyncio
async def test_pi_bridge_dispatch_without_init_structured():
    """未 init → 结构化错误 (不崩溃)。"""
    fresh = type(pi_bridge)()
    d = await fresh.dispatch("hi")
    assert d["ok"] is False
    assert "未初始化" in d["error"]


# =========================================================================
# L3 — agentscope (已装则真实, 未装结构化; 事件映射翻译)
# =========================================================================

@pytest.mark.asyncio
async def test_agentscope_bridge_init():
    try:
        import agentscope  # noqa: F401
    except ImportError:
        r = await agentscope_bridge.init()
        assert r["ok"] is False and "pip install agentscope" in r["error"]
        return
    r = await agentscope_bridge.init()
    assert r["ok"] is True
    assert r["version"]


def test_agentscope_event_translation():
    """事件映射表 (PRD §5): agentscope → veya event_bus 主题。"""
    topic, payload = agentscope_bridge._translate_event(
        {"type": "end", "taskId": "t1"})
    assert topic == "agent.end"
    assert payload["source"] == "agentscope"
    assert payload["taskId"] == "t1"

    topic2, _ = agentscope_bridge._translate_event({"type": "start"})
    assert topic2 == "agent.start"


# =========================================================================
# 端点
# =========================================================================

def test_operators_endpoint_runtime_health():
    from fastapi.testclient import TestClient

    from server.app import app

    r = TestClient(app).get("/api/v1/operators")
    assert r.status_code == 200
    healths = r.json()["runtime_health"]
    assert len(healths) == 3
    by_name = {h["runtime"]: h for h in healths}
    assert "pi_bridge" in by_name
    # 至少一个真实探测成功 (pi 本机可用 或 agentscope 已装)
    assert any(h["ok"] for h in healths)

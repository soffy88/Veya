"""引擎路由门禁 — 缺失 CLI 必须返回结构化错误 (不能 500/520)。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_stream_engine_missing_cli_yields_error():
    """engine CLI 缺失 → engine_error 事件, 不抛异常 (远端 520 根因)。"""
    from server.engine_runner import stream_engine

    events = [evt async for evt in stream_engine("no-such-cli-binary", "hi", timeout_s=10)]
    assert events, "必须产出事件"
    assert events[0]["type"] == "engine_error"
    assert "不可用" in events[0]["error"] or "not" in events[0]["error"].lower()


@pytest.mark.asyncio
async def test_run_engine_missing_cli_returns_ok_false():
    """run 契约: CLI 缺失返回 ok=False + 错误信息。"""
    from server.engine_runner import run_engine

    result = await run_engine("no-such-cli-binary", "hi", timeout_s=10)
    assert result["ok"] is False
    assert "不可用" in result["error"]


def test_available_engines_always_has_master():
    """master 恒可用 (builtin), 其余按本机 CLI 探测。"""
    from server.engine_runner import available_engines

    engines = available_engines()
    assert engines.get("master") == "builtin"


def test_engines_endpoint():
    """GET /api/v1/engines 返回引擎清单 (前端禁用依据)。"""
    from fastapi.testclient import TestClient

    from server.app import app

    res = TestClient(app).get("/api/v1/engines")
    assert res.status_code == 200
    data = res.json()
    assert "engines" in data
    assert data["engines"]["master"] == "builtin"

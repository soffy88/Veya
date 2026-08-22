"""browser-use + Agent-Reach 集成门禁 — 技能包热载 + browser_run 引擎升级。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

SKILLS_DIR = Path.home() / ".veya" / "skills"


def test_skill_pack_manifests_valid():
    """两个技能包 manifest 存在且满足 skill_hub 校验 (parameters.properties)。"""
    for name in ("browser_use", "agent_reach"):
        manifest = json.loads((SKILLS_DIR / name / "manifest.json").read_text())
        assert manifest["name"] == name
        assert manifest.get("description")
        assert "properties" in manifest["parameters"]
        if name == "browser_use":
            assert manifest["type"] == "python"
            assert (SKILLS_DIR / name / "run.py").exists()
        else:
            assert manifest["type"] == "mcp"
            assert manifest["endpoint"].startswith("http://")


def test_skill_hub_loads_both_packs(tmp_path, monkeypatch):
    """skill_hub 热载: 两个技能包全部挂载 + schema 面向 LLM。

    officecli 合法用 subprocess (它的职责就是跑 CLI 工具), 是信任名单机制的
    典型场景——2026-08-22 起默认 strict, 不显式信任就会被拒载。

    用隔离的 tmp_path (只拷贝这三个技能), 不直接指向真实 ~/.veya/skills——
    真实目录里的技能数量会持续增长, run_skill 目录有 VEYA_SKILL_CATALOG_CAP
    (默认30) 硬顶, 数量涨过阈值后 browser_use 会被挤出常驻目录, 跟本测试要
    验证的"两个包能不能正常热载+暴露 schema"这件事本身无关。
    """
    monkeypatch.setenv("VEYA_SKILL_TRUSTED_NAMES", "officecli")
    import shutil

    isolated_dir = tmp_path / "skills"
    isolated_dir.mkdir()
    for name in ("browser_use", "agent_reach", "officecli"):
        shutil.copytree(SKILLS_DIR / name, isolated_dir / name)

    from server.skill_hub import VeyaSkillHub

    hub = VeyaSkillHub(skills_dir=str(isolated_dir))
    result = hub.reload_skills()
    assert result["loaded"] >= 3  # browser_use + agent_reach + officecli
    assert result["errors"] == 0
    assert {"browser_use", "agent_reach", "officecli"} <= set(hub._all_skill_names())

    # ②-A dispatcher: LLM 看到的是 list_skills + run_skill 调度器
    names = {s["function"]["name"] for s in hub.get_all_schemas()}
    assert {"list_skills", "run_skill"} <= names
    run = next(s for s in hub.get_all_schemas() if s["function"]["name"] == "run_skill")
    assert "skill_name" in run["function"]["parameters"]["properties"]
    assert "browser_use" in run["function"]["description"]  # 目录进 catalog


def test_agent_reach_mcp_executor_unreachable_is_structured():
    """agent_reach sidecar 未启动 → 结构化错误 (不可达), 不崩溃。"""
    from server.skill_hub import VeyaSkillHub

    hub = VeyaSkillHub(skills_dir=str(SKILLS_DIR))
    hub.reload_skills()
    executor = hub._executors["agent_reach"]

    import asyncio

    async def _run() -> str:
        try:
            return await executor(channel="youtube_transcript", url="https://example.com/v")
        except Exception as e:
            return f"TOOL_ERROR: {type(e).__name__}: {e}"

    out = asyncio.run(_run())
    assert out.startswith("TOOL_ERROR:")  # 结构化失败而非裸 500
    assert "不可达" in out or "HTTP" in out


def test_browser_run_engine_field_schema():
    """BrowserRunRequest 接受 engine 字段 (browser_use 默认 | omodul)。"""
    from server.app import app

    client = TestClient(app)
    # engine 合法值
    r = client.post(
        "/api/v1/browser/run",
        json={
            "url": "https://example.com",
            "instruction": "hi",
            "engine": "omodul",
        },
    )
    assert r.status_code in (200, 400, 500)  # 触发真实执行前 schema 已通过

    # 非法 engine → 422 (Literal 校验)
    r = client.post(
        "/api/v1/browser/run",
        json={
            "url": "https://example.com",
            "instruction": "hi",
            "engine": "nonsense",
        },
    )
    assert r.status_code == 422

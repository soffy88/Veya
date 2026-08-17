"""Veya SkillHub 测试 — 技能扫描 / 动态加载 / 热重载 / 主脑闭环。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.coordinator_master import MasterCoordinator
from server.skill_hub import VeyaSkillHub, create_skill_package
from server.tool_registry import ToolExecutionError


def _write_skill(
    skills_dir: Path,
    name: str,
    *,
    code: str,
    description: str = "Test skill",
    parameters: dict | None = None,
    entrypoint: str = "run.py",
) -> Path:
    pkg = skills_dir / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": description,
                "type": "python",
                "entrypoint": entrypoint,
                "parameters": parameters
                or {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (pkg / entrypoint).write_text(code, encoding="utf-8")
    return pkg


# ---------------------------------------------------------------------------
# 1. 技能加载与执行
# ---------------------------------------------------------------------------


def test_hub_loads_python_skill(tmp_path):
    """扫描目录 → 解析 manifest → 挂载执行器 → schema 可见。"""
    _write_skill(
        tmp_path,
        "greeter",
        code="def main(x):\n    return {'hello': x}\n",
        description="Greet a user by name",
    )
    hub = VeyaSkillHub(skills_dir=tmp_path)

    assert hub._all_skill_names() == ["greeter"]
    schemas = hub.get_all_schemas()
    # ②-A dispatcher 收口: N 个 per-skill 工具 → list_skills + run_skill
    assert {s["function"]["name"] for s in schemas} == {"list_skills", "run_skill"}
    run = next(s for s in schemas if s["function"]["name"] == "run_skill")
    assert "skill_name" in run["function"]["parameters"]["properties"]
    assert "greeter" in run["function"]["description"]  # 目录进 catalog
    assert hub.get_stats()["loaded"] == 1


@pytest.mark.asyncio
async def test_hub_executes_python_skill(tmp_path):
    _write_skill(
        tmp_path,
        "adder",
        code="def main(a, b):\n    return a + b\n",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    )
    hub = VeyaSkillHub(skills_dir=tmp_path)

    out = await hub.execute("adder", {"a": 2, "b": 3})
    assert out == "5"


@pytest.mark.asyncio
async def test_hub_executes_async_skill(tmp_path):
    _write_skill(
        tmp_path,
        "async_skill",
        code="import asyncio\nasync def main(x):\n    await asyncio.sleep(0)\n    return {'x': x * 2}\n",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    )
    hub = VeyaSkillHub(skills_dir=tmp_path)
    out = await hub.execute("async_skill", {"x": 21})
    assert json.loads(out) == {"x": 42}


@pytest.mark.asyncio
async def test_hub_skill_errors(tmp_path):
    # 缺失 main 函数
    _write_skill(
        tmp_path,
        "no_main",
        code="def helper():\n    return 1\n",
        parameters={"type": "object", "properties": {}},
    )
    # 入口文件缺失: manifest 指向不存在的 run.py
    pkg = tmp_path / "no_file"
    pkg.mkdir()
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "name": "no_file",
                "description": "d",
                "type": "python",
                "entrypoint": "missing.py",
                "parameters": {"type": "object", "properties": {}},
            }
        ),
        encoding="utf-8",
    )
    # 技能内部抛异常
    _write_skill(
        tmp_path,
        "boom",
        code="def main():\n    raise ValueError('skill exploded')\n",
        parameters={"type": "object", "properties": {}},
    )
    hub = VeyaSkillHub(skills_dir=tmp_path)

    with pytest.raises(ToolExecutionError, match="main"):
        await hub.execute("no_main", {})
    with pytest.raises(ToolExecutionError, match="missing"):
        await hub.execute("no_file", {})
    with pytest.raises(ToolExecutionError, match="skill exploded"):
        await hub.execute("boom", {})

    with pytest.raises(ToolExecutionError, match="not loaded"):
        await hub.execute("ghost", {})


def test_hub_skips_invalid_manifests(tmp_path):
    # 无 manifest 的目录 → 跳过
    (tmp_path / "no_manifest").mkdir()
    # 非法 JSON
    pkg = tmp_path / "bad_json"
    pkg.mkdir()
    (pkg / "manifest.json").write_text("{not json", encoding="utf-8")
    # 未知 type
    pkg2 = tmp_path / "bad_type"
    pkg2.mkdir()
    (pkg2 / "manifest.json").write_text(
        json.dumps(
            {
                "name": "bad_type",
                "description": "d",
                "type": "docker",
                "parameters": {"type": "object", "properties": {}},
            }
        ),
        encoding="utf-8",
    )
    # MCP 缺 endpoint
    pkg3 = tmp_path / "mcp_no_ep"
    pkg3.mkdir()
    (pkg3 / "manifest.json").write_text(
        json.dumps(
            {
                "name": "mcp_no_ep",
                "description": "d",
                "type": "mcp",
                "parameters": {"type": "object", "properties": {}},
            }
        ),
        encoding="utf-8",
    )
    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert hub.list_skills() == []
    assert hub.get_stats()["loaded"] == 0  # 全部非法包被跳过


def test_hub_duplicate_name_last_wins(tmp_path):
    _write_skill(tmp_path, "dup", code="def main():\n    return 'v1'\n")
    _write_skill(tmp_path, "dup2", code="def main():\n    return 'v2'\n")
    # 制造重名: 两个技能目录同名不可能, 直接用 manifest name 碰撞
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "manifest.json").write_text(
        json.dumps(
            {
                "name": "dup",
                "description": "second",
                "type": "python",
                "entrypoint": "run.py",
                "parameters": {"type": "object", "properties": {}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "other" / "run.py").write_text("def main():\n    return 'override'\n", encoding="utf-8")
    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert hub.has("dup")
    # ②-A dispatcher: 真实技能名走 _all_skill_names, catalog 反映后加载覆盖
    assert "dup" in hub._all_skill_names()
    assert "dup2" in hub._all_skill_names()
    run = next(s for s in hub.get_all_schemas()
               if s["function"]["name"] == "run_skill")
    assert "second" in run["function"]["description"]  # 后加载者覆盖生效


# ---------------------------------------------------------------------------
# 2. 热重载 (Hot Reload)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hot_reload_add_and_remove(tmp_path):
    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert hub.list_skills() == []

    # 新技能包落地(模拟 Genesis 交付) → 热重载 → 秒学会
    _write_skill(
        tmp_path,
        "btc_price",
        code="def main():\n    return {'btc': 97000}\n",
        parameters={"type": "object", "properties": {}},
    )
    stats = hub.reload_skills()
    assert stats["loaded"] == 1
    assert hub.has("btc_price")
    assert "btc_price" in hub.describe("btc_price")
    out = await hub.execute("btc_price", {})
    assert "97000" in out

    # 删除技能包 → 热重载 → 消失
    import shutil

    shutil.rmtree(tmp_path / "btc_price")
    hub.reload_skills()
    assert not hub.has("btc_price")


def test_create_skill_package_helper(tmp_path):
    """create_skill_package: Genesis 写技能的统一交付规范。"""
    pkg = create_skill_package(
        "crypto_tracker",
        "Fetch real-time BTC price",
        {"type": "object", "properties": {"currency": {"type": "string"}}, "required": ["currency"]},
        skills_dir=tmp_path,
        code="def main(currency='usd'):\n    return {'currency': currency}\n",
    )
    assert (pkg / "manifest.json").exists()
    assert (pkg / "run.py").exists()

    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert hub.has("crypto_tracker")


# ---------------------------------------------------------------------------
# 3. MCP 技能桥接
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_skill_http_bridge(tmp_path, monkeypatch):
    """MCP 技能: HTTP 桥接调用外部服务。"""
    pkg = tmp_path / "jira_mcp"
    pkg.mkdir()
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "name": "jira_mcp",
                "description": "Query internal JIRA",
                "type": "mcp",
                "endpoint": "http://127.0.0.1:9999",
                "parameters": {"type": "object", "properties": {"issue": {"type": "string"}}, "required": ["issue"]},
            }
        ),
        encoding="utf-8",
    )
    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert hub.get_stats()["types"]["mcp"] == 1

    import httpx

    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"key": "PROJ-1"}'

        def raise_for_status(self):
            pass

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            captured["body"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())
    out = await hub.execute("jira_mcp", {"issue": "PROJ-1"})
    assert captured["url"] == "http://127.0.0.1:9999/v1/tools/jira_mcp/execute"
    assert captured["body"] == {"issue": "PROJ-1"}
    assert "PROJ-1" in out


@pytest.mark.asyncio
async def test_mcp_skill_unreachable(tmp_path, monkeypatch):
    pkg = tmp_path / "down_mcp"
    pkg.mkdir()
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "name": "down_mcp",
                "description": "d",
                "type": "mcp",
                "endpoint": "http://127.0.0.1:1",
                "parameters": {"type": "object", "properties": {}},
            }
        ),
        encoding="utf-8",
    )
    hub = VeyaSkillHub(skills_dir=tmp_path)
    with pytest.raises(ToolExecutionError, match=r"不可达|HTTP"):
        await hub.execute("down_mcp", {})


# ---------------------------------------------------------------------------
# 4. 主脑闭环: 系统热重载 + 动态技能调用
# ---------------------------------------------------------------------------


def _tool_response(name: str, args: dict, content: str = "", tc_id: str = "call_1") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _text_response(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {}}


@pytest.mark.asyncio
async def test_master_system_reload_tool(tmp_path):
    """主脑拦截 system_reload_skills → 热重载 → 新技能立即可用。"""
    hub = VeyaSkillHub(skills_dir=tmp_path)
    calls = []
    tools_seen = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        tools_seen.append(kwargs["tools"])
        turn = len(calls)
        # 第一轮: 模型决定先热重载
        if turn == 1:
            return _tool_response("system_reload_skills", {})
        # 第二轮: 新技能已可见, 模型通过 run_skill 调度器调用它
        if turn == 2:
            return _tool_response("run_skill", {"skill_name": "btc_price"})
        return _text_response("比特币价格查询完成: 97000 USD")

    # 技能包在 reload 前写入磁盘(模拟 Genesis 交付)
    _write_skill(
        tmp_path,
        "btc_price",
        code="def main():\n    return {'btc': 97000, 'usd': 97000}\n",
        parameters={"type": "object", "properties": {}},
    )

    coord = MasterCoordinator(llm_fn=fake_llm, skill_hub=hub, max_rounds=4)
    result = await coord.chat_stream("查一下比特币价格", session_id="s1")

    assert result["status"] == "success"
    assert result["rounds"] == 3
    assert result["tool_calls"] == [
        {"tool": "system_reload_skills", "status": "success"},
        {"tool": "run_skill", "status": "success"},
    ]
    # 热重载结果回喂 (数字与 skill 计数环境相关, 只验证成功语义)
    assert "reloaded successfully" in calls[1][-1]["content"]
    assert "dynamic skills" in calls[1][-1]["content"]
    # 技能执行结果回喂
    assert "97000" in calls[2][-1]["content"]
    # 第二轮起, 动态技能通过 run_skill 调度器出现在喂给模型的 tools 中
    tool_names = {t["function"]["name"] for t in tools_seen[1]}
    assert "run_skill" in tool_names
    assert "system_reload_skills" in tool_names


@pytest.mark.asyncio
async def test_master_routes_to_skill_hub(tmp_path):
    """动态技能执行失败 → FAILED 回喂 → 主脑反思。"""
    _write_skill(
        tmp_path,
        "flaky",
        code="def main(x):\n    raise RuntimeError(f'bad input {x}')\n",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    )
    hub = VeyaSkillHub(skills_dir=tmp_path)
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return _tool_response("flaky", {"x": 1})
        return _text_response("技能报错了, 我换一个方案。")

    coord = MasterCoordinator(llm_fn=fake_llm, skill_hub=hub, max_rounds=3)
    result = await coord.chat_stream("测试", session_id="s2")
    assert result["status"] == "success"
    assert result["tool_calls"][0] == {"tool": "flaky", "status": "failed", "error": "Skill 'flaky' main() failed: bad input 1"}
    assert "[Tool flaky FAILED]" in calls[1][-1]["content"]


def test_master_inventory_includes_skills(tmp_path):
    """②-A dispatcher: 技能不再逐条进系统提示 (精简意图), 只保留 system_reload_skills。"""
    _write_skill(tmp_path, "greeter", code="def main(x):\n    return x\n")
    hub = VeyaSkillHub(skills_dir=tmp_path)
    coord = MasterCoordinator(skill_hub=hub)
    prompt = coord.get_system_prompt()
    assert "- greeter —" not in prompt      # dispatcher: 技能不进 system 提示
    assert "- system_reload_skills —" in prompt

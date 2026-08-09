"""主脑 (Master) 测试 — 全局能力注册表 / SOP 潜意识注入 / ReAct 无缝组装。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from server.coordinator_master import MASTER_SYSTEM_PROMPT, MasterCoordinator
from server.tool_registry import (
    MasterToolRegistry,
    ToolExecutionError,
    master_tools,
    set_genesis_factory,
)

# ---------------------------------------------------------------------------
# 1. 全局能力注册表 (MasterToolRegistry)
# ---------------------------------------------------------------------------


def test_registry_register_and_schemas():
    reg = MasterToolRegistry()

    def _add(a: int, b: int) -> int:
        return a + b

    reg.register(
        "add_numbers",
        "Add two integers. Use for arithmetic requests.",
        {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]},
        _add,
    )

    schemas = reg.get_all_schemas()
    assert len(schemas) == 1
    assert schemas[0]["type"] == "function"
    fn = schemas[0]["function"]
    assert fn["name"] == "add_numbers"
    assert fn["parameters"]["required"] == ["a", "b"]
    assert "Add two integers" in fn["description"]

    assert reg.has("add_numbers")
    assert reg.list_tools() == ["add_numbers"]
    assert "add_numbers — Add two integers" in reg.describe("add_numbers")


@pytest.mark.asyncio
async def test_registry_execute_sync_and_async():
    reg = MasterToolRegistry()

    def _sync(x: int) -> int:
        return x * 2

    async def _async(x: int) -> int:
        return x * 3

    reg.register("sync_double", "sync", {"type": "object", "properties": {"x": {"type": "integer"}}}, _sync)
    reg.register("async_triple", "async", {"type": "object", "properties": {"x": {"type": "integer"}}}, _async)

    assert await reg.execute("sync_double", {"x": 2}) == "4"
    assert await reg.execute("async_triple", {"x": 2}) == "6"


@pytest.mark.asyncio
async def test_registry_errors():
    reg = MasterToolRegistry()

    def _f():
        return "ok"

    with pytest.raises(ValueError, match="already registered"):
        reg.register("dup", "d", {"type": "object", "properties": {}}, _f)
        reg.register("dup", "d", {"type": "object", "properties": {}}, _f)

    with pytest.raises(ValueError, match="properties"):
        reg.register("bad", "d", {"type": "object"}, _f)

    with pytest.raises(ToolExecutionError, match="not found"):
        await reg.execute("nope", {})

    reg.register("boom", "d", {"type": "object", "properties": {}}, lambda: 1 / 0)
    with pytest.raises(ToolExecutionError, match="boom"):
        await reg.execute("boom", {})


def test_master_tools_mounted_capabilities():
    """全局注册表已挂载 Veya 后端能力。"""
    names = set(master_tools.list_tools())
    for expected in (
        "browser_run",
        "delegate_to_genesis",
        "read_file_ast",
        "grep",
        "list_files",
        "run_in_sandbox",
        "search_genesis_ledger",
    ):
        assert expected in names, f"missing {expected}"
    # 全部 schema 均为 OpenAI function-calling 格式
    for schema in master_tools.get_all_schemas():
        assert schema["type"] == "function"
        assert schema["function"]["name"]
        assert schema["function"]["description"]
        assert "properties" in schema["function"]["parameters"]


# ---------------------------------------------------------------------------
# 2. 真实工具执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_ast_real(tmp_path, monkeypatch):
    """AST 骨架: 真实文件 → 签名/行号, 无函数体。"""
    src = "def greet(name: str) -> str:\n    \"\"\"Say hello.\"\"\"\n    return f'Hi {name}'\n\nclass Calc:\n    def add(self, a, b):\n        return a + b\n"
    (tmp_path / "mod.py").write_text(src)
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))

    result = await master_tools.execute("read_file_ast", {"filepath": "mod.py"})
    assert "def greet(" in result
    assert "class Calc" in result
    assert "def add(" in result
    assert "return f'Hi" not in result  # 函数体被压缩


@pytest.mark.asyncio
async def test_read_file_ast_path_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    with pytest.raises(ToolExecutionError, match="escapes workspace"):
        await master_tools.execute("read_file_ast", {"filepath": "../outside.py"})


@pytest.mark.asyncio
async def test_run_in_sandbox_real(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    out = await master_tools.execute("run_in_sandbox", {"code": "print(6 * 7)"})
    assert "exit_code=0" in out and "42" in out

    with pytest.raises(ToolExecutionError, match="TypeError"):
        await master_tools.execute("run_in_sandbox", {"code": "raise TypeError('boom')"})


@pytest.mark.asyncio
async def test_grep_real(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def master_fn():\n    pass\n")
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    out = await master_tools.execute("grep", {"pattern": "master_fn"})
    assert "a.py:1" in out


@pytest.mark.asyncio
async def test_delegate_to_genesis_routes_mission(tmp_path):
    """delegate_to_genesis: 验证 requirement 解析 + Genesis 唤醒 + 结果/账本回传。"""
    fake_agent = AsyncMock()
    fake_agent.handle_mission.return_value = {"status": "success", "response": "已锻造", "steps": 3}
    fake_agent.memory = type("M", (), {"memory": {"element_ledger": {"oskill/ema.py": {"version": 1}}}})()
    set_genesis_factory(lambda: fake_agent)
    try:
        out = await master_tools.execute(
            "delegate_to_genesis",
            {"requirement_json": json.dumps({"layer": "oskill", "element": "ema"})},
        )
    finally:
        set_genesis_factory(None)
    assert "已锻造" in out
    assert "oskill/ema.py" in out
    fake_agent.handle_mission.assert_awaited_once()


@pytest.mark.asyncio
async def test_delegate_to_genesis_bad_json_and_failure(tmp_path):
    with pytest.raises(ToolExecutionError, match="不是合法 JSON"):
        await master_tools.execute("delegate_to_genesis", {"requirement_json": "{not json"})

    fake_agent = AsyncMock()
    fake_agent.handle_mission.return_value = {"status": "failed", "error": "Max steps"}
    fake_agent.memory = type("M", (), {"memory": {"element_ledger": {}}})()
    set_genesis_factory(lambda: fake_agent)
    try:
        with pytest.raises(ToolExecutionError, match="Genesis mission failed"):
            await master_tools.execute(
                "delegate_to_genesis", {"requirement_json": "{}"}
            )
    finally:
        set_genesis_factory(None)


# ---------------------------------------------------------------------------
# 3. 潜意识注入 (SOP)
# ---------------------------------------------------------------------------


def test_system_prompt_routing_rules():
    assert "You are the Veya Master Coordinator" in MASTER_SYSTEM_PROMPT
    assert "DO NOT simulate actions" in MASTER_SYSTEM_PROMPT
    assert "INTENT ROUTING RULES" in MASTER_SYSTEM_PROMPT
    assert "browser_run" in MASTER_SYSTEM_PROMPT
    assert "run_in_sandbox" in MASTER_SYSTEM_PROMPT
    assert "delegate_to_genesis" in MASTER_SYSTEM_PROMPT
    assert "read_file_ast" in MASTER_SYSTEM_PROMPT


def test_system_prompt_includes_dynamic_tool_inventory():
    coord = MasterCoordinator()
    prompt = coord.get_system_prompt()
    assert "# AVAILABLE TOOLS" in prompt
    assert "- browser_run —" in prompt
    assert "- delegate_to_genesis —" in prompt
    assert "- read_file_ast —" in prompt


# ---------------------------------------------------------------------------
# 4. 无缝组装 (ReAct 循环)
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
async def test_chat_stream_tool_loop_success(tmp_path, monkeypatch):
    """模型调工具 → 物理执行 → 结果回喂 → 最终回答。"""
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        # 主脑必须把三层 JSON Schema 喂给模型: 系统 + 静态能力 + 动态技能
        tool_names = {t["function"]["name"] for t in kwargs["tools"]}
        assert "system_reload_skills" in tool_names
        assert "list_files" in tool_names
        assert "browser_run" in tool_names
        turn = len(calls)
        if turn == 1:
            return _tool_response("list_files", {"path": "."})
        return _text_response("总结: 项目文件已列出。")

    coord = MasterCoordinator(llm_fn=fake_llm, max_rounds=3)
    result = await coord.chat_stream("项目里有什么?", session_id="m1")

    assert result["status"] == "success"
    assert "总结" in result["final_answer"]
    assert result["rounds"] == 2
    assert result["tool_calls"] == [{"tool": "list_files", "status": "success"}]
    # 工具结果已回喂(第二轮 LLM 看到 list_files 的成功观察)
    assert "[Tool list_files SUCCESS]" in calls[1][-1]["content"]
    # 潜意识注入: system prompt 含 SOP + 工具清单
    assert "INTENT ROUTING RULES" in calls[0][0]["content"]
    assert "# AVAILABLE TOOLS" in calls[0][0]["content"]


@pytest.mark.asyncio
async def test_chat_stream_failure_reflection(tmp_path, monkeypatch):
    """工具失败 → FAILED 回喂 → 模型换方法重试成功。"""
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        turn = len(calls)
        if turn == 1:
            return _tool_response("read_file_ast", {"filepath": "missing.py"})
        return _text_response("文件不存在, 我改用 grep 查询。")

    coord = MasterCoordinator(llm_fn=fake_llm, max_rounds=3)
    result = await coord.chat_stream("看看 missing.py", session_id="m2")

    assert result["status"] == "success"
    assert result["tool_calls"][0]["status"] == "failed"
    # 失败详情已回喂给模型反思
    assert "[Tool read_file_ast FAILED]" in calls[1][-1]["content"]
    assert "file not found" in calls[1][-1]["content"]


@pytest.mark.asyncio
async def test_chat_stream_no_tool_direct_answer():
    """一般知识/闲聊 → 模型直接回答, 不调工具。"""
    async def fake_llm(messages, **kwargs):
        return _text_response("你好! 我是 Veya 主脑。")

    coord = MasterCoordinator(llm_fn=fake_llm, max_rounds=3)
    result = await coord.chat_stream("你好", session_id="m3")
    assert result["status"] == "success"
    assert result["rounds"] == 1
    assert result["tool_calls"] == []


@pytest.mark.asyncio
async def test_lightweight_chat_injects_system_prompt():
    """轻量单轮 chat() 必须注入主脑 system prompt — 否则模型自报本体人格。"""
    from oservi.master_agent import MasterAgent

    seen: list[list] = []

    async def fake_llm(messages, **kwargs):
        seen.append(list(messages))
        return _text_response("我是 Veya。")

    agent = MasterAgent(llm_caller=fake_llm, max_rounds=1, temperature=0.3,
                        tools={}, skill_hub=None, memory=None, swarm=None,
                        vault=None)
    agent.get_system_prompt = lambda: "你是 Veya 主脑。"
    result = await agent.chat("你是谁")
    assert result["final_answer"] == "我是 Veya。"
    # 首条必须是 system (非 system 前缀 = 人格丢失回归)
    assert seen and seen[0][0]["role"] == "system"
    assert "Veya 主脑" in seen[0][0]["content"]
    assert seen[0][1]["role"] == "user"


@pytest.mark.asyncio
async def test_chat_stream_hitl_after_max_rounds():
    """模型循环调工具 → 超过最大轮次 → HITL。"""
    async def looping_llm(messages, **kwargs):
        return _tool_response("list_files", {"path": "."})

    coord = MasterCoordinator(llm_fn=looping_llm, max_rounds=2)
    result = await coord.chat_stream("循环任务", session_id="m4")
    # 行为变更 (oservi a90e91d): 轮次用尽但有工具执行 → 摘要 success (不静默 failed)
    assert result["status"] == "success"
    assert "已执行" in result["final_answer"]
    assert "list_files" in result["final_answer"]
    assert result["rounds"] == 2


@pytest.mark.asyncio
async def test_chat_stream_user_api_key_isolated():
    """用户侧 Key 只注入实例 config, 不污染全局环境。"""
    async def fake_llm(messages, **kwargs):
        assert kwargs["config"] == {"providers": {"openai": {"api_key": "sk-user-1"}}}
        return _text_response("ok")

    coord = MasterCoordinator(user_api_key="sk-user-1", provider="openai", llm_fn=fake_llm)
    result = await coord.chat("hi")
    assert result["status"] == "success"


def test_master_router_endpoints_reachable():
    """主脑路由已挂载: /master/tools 真实可访问。"""
    from fastapi.testclient import TestClient

    from server.app import app

    client = TestClient(app)
    resp = client.get("/master/tools")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    names = {t["name"] for t in tools}
    assert "browser_run" in names
    assert "delegate_to_genesis" in names
    assert "read_file_ast" in names
    client.close()


# =========================================================================
# 连续对话历史 (session_id 持久化)
# =========================================================================

def test_conversation_history_persists_across_turns():
    """同 session 第二轮: 模型应看到第一轮的对话 (含 assistant 回答)。"""
    import asyncio

    from veya_loop import _assembly

    oservi = _assembly.load("oservi")
    MasterAgent = oservi.MasterAgent

    class Tools:
        def get_all_tool_schemas(self):
            return []

        def get_all_schemas(self):
            return []

        def list_tools(self):
            return []

        def describe(self, name):
            return ""

        def has(self, name):
            return False

        async def execute(self, name, args=None):
            return ""

    class SkillHub:
        def list_skills(self):
            return []

        def get_all_schemas(self):
            return []

        def describe(self, name):
            return ""

    class Memory:
        def inject_subconscious(self):
            return ""

    seen_messages = []

    async def llm(messages, **kwargs):
        seen_messages.append(list(messages))
        return {"choices": [{"message": {"role": "assistant", "content": "first reply"}}],
                "usage": {}}

    agent = MasterAgent(
        llm_caller=llm, tools=Tools(), skill_hub=SkillHub(), memory=Memory(),
        swarm=None, vault=None, max_rounds=8, notify=lambda _e: None,
    )

    r1 = asyncio.run(agent.chat_stream("你好", session_id="conv1"))
    assert r1["status"] == "success" and r1["final_answer"] == "first reply"

    r2 = asyncio.run(agent.chat_stream("还记得我上句说了什么吗", session_id="conv1"))
    assert r2["status"] == "success"
    # 第二轮模型看到的 messages 含第一轮的 user + assistant
    msgs2 = seen_messages[1]
    roles = [(m.get("role"), m.get("content")) for m in msgs2]
    assert ("user", "你好") in roles                 # 第一轮 user 仍在
    assert ("assistant", "first reply") in roles     # 第一轮 assistant 仍在
    assert msgs2[0]["role"] == "system"              # system 常驻首位
    assert roles[-1] == ("user", "还记得我上句说了什么吗")


def test_conversation_history_scoped_by_session():
    """不同 session_id 历史隔离; 新会话从 system 起步。"""
    import asyncio

    from veya_loop import _assembly

    oservi = _assembly.load("oservi")
    MasterAgent = oservi.MasterAgent

    class Tools:
        def get_all_tool_schemas(self): return []
        def get_all_schemas(self): return []
        def list_tools(self): return []
        def describe(self, name): return ""
        def has(self, name): return False
        async def execute(self, name, args=None): return ""

    class SkillHub:
        def list_skills(self): return []
        def get_all_schemas(self): return []
        def describe(self, name): return ""

    class Memory:
        def inject_subconscious(self): return ""

    seen = []

    async def llm(messages, **kwargs):
        seen.append(list(messages))
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}}

    agent = MasterAgent(llm_caller=llm, tools=Tools(), skill_hub=SkillHub(),
                        memory=Memory(), swarm=None, vault=None,
                        max_rounds=8, notify=lambda _e: None)
    asyncio.run(agent.chat_stream("A 会话", session_id="sa"))
    asyncio.run(agent.chat_stream("B 会话", session_id="sb"))
    asyncio.run(agent.chat_stream("A 又来了", session_id="sa"))

    assert ("user", "A 会话") in [(m.get("role"), m.get("content")) for m in seen[2]]
    assert ("user", "B 会话") not in [(m.get("role"), m.get("content")) for m in seen[2]]


class TestToolSurfaceLayering:
    """原生智能优先 (2026-08) 的工具面分层: 路由决策权在模型,
    但工具面做「诱惑管理」— free 池模型在全量 173 工具下会被领域工具
    带偏 (设计任务去查行情 → 死循环 → 空回复)。

    核心执行工具恒保留; mcp/技能按领域意图召入; 量化数据工具仅**操作**
    意图召回 (指标名 MACD 等不触发 — 设计任务不该被带进查行情)。
    """

    def _tools(self):
        def mk(name):
            return {"type": "function", "function": {"name": name}}
        return [
            mk("fetch_url"),
            mk("browser_run"),
            mk("reasonix_run"),
            mk("run_in_sandbox"),
            mk("get_market_data_schema"),
            mk("run_backtest_coprocessor"),
            mk("mcp_stratum_search_knowledge"),
            mk("mcp_hevi_generate_longvideo"),
            mk("mcp_codebase_search_code"),
            mk("system_ping"),
            mk("ecc_code_reviewer"),
        ]

    @staticmethod
    def _layered(tools, user_text):
        from server.coordinator_master import MasterCoordinator

        return MasterCoordinator._layer_tools(
            tools, [{"role": "user", "content": user_text}])

    def test_design_task_excludes_quant_tools(self):
        """设计任务 (含 MACD 指标名) 不召回行情数据工具 — 防乱调死循环。"""
        names = {t["function"]["name"] for t in self._layered(
            self._tools(), "写一个基于 MACD 的风险控制拦截器设计")}
        assert "fetch_url" in names and "browser_run" in names  # 核心恒在
        assert "system_ping" in names
        assert "get_market_data_schema" not in names, "设计任务不该看到行情工具"
        assert "run_backtest_coprocessor" not in names

    def test_quant_operation_recalls_data_tools(self):
        """真实回测操作 (明确操作词) 才召回行情数据工具。"""
        names = {t["function"]["name"] for t in self._layered(
            self._tools(), "用 MACD 回测 BTCUSDT 并输出回测指标")}
        assert "get_market_data_schema" in names
        assert "run_backtest_coprocessor" in names

    def test_domain_recall_by_intent(self):
        """领域工具按意图召入: 知识→stratum, 视频→hevi, 代码→codebase。"""
        know = {t["function"]["name"] for t in self._layered(
            self._tools(), "帮我检索知识库里的文章")}
        assert "mcp_stratum_search_knowledge" in know
        assert "mcp_hevi_generate_longvideo" not in know
        vid = {t["function"]["name"] for t in self._layered(
            self._tools(), "生成一个动画视频")}
        assert "mcp_hevi_generate_longvideo" in vid
        code = {t["function"]["name"] for t in self._layered(
            self._tools(), "帮我审查这段代码")}
        assert "mcp_codebase_search_code" in code
        assert "ecc_code_reviewer" in code

    def test_core_tools_always_present(self):
        """核心执行工具 (fetch/reasonix/browser/sandbox) 任何任务恒可见。"""
        names = {t["function"]["name"] for t in self._layered(
            self._tools(), "你好")}
        assert {"fetch_url", "browser_run", "reasonix_run",
                "run_in_sandbox", "system_ping"} <= names

    def test_bound_llm_applies_layering(self):
        """_bound_llm 对工具面做分层 (非全量透传)。"""
        import asyncio

        from server.coordinator_master import MasterCoordinator

        captured: dict = {}

        async def fake_llm(messages, **kwargs):
            captured["tools"] = kwargs.get("tools")
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {}}

        coord = MasterCoordinator(llm_fn=fake_llm)
        asyncio.run(coord._bound_llm(
            [{"role": "user", "content": "写一个基于 MACD 的风险控制拦截器设计"}],
            tools=self._tools()))
        names = {(t.get("function") or {}).get("name") for t in captured["tools"]}
        assert "get_market_data_schema" not in names, "设计任务不该看到行情工具"
        assert "fetch_url" in names

    def test_bound_llm_no_retry_for_mock_llm(self):
        """非生产 llm (mock) 不走重试分支 — 测试注入语义保持。"""
        import asyncio

        from server.coordinator_master import MasterCoordinator

        calls: list[dict] = []

        async def flaky_llm(messages, **kw):
            calls.append({**kw, "_n": len(calls) + 1})
            return {"choices": [{"message": {"role": "assistant",
                                               "content": "None"}}],
                    "usage": {}}

        coord = MasterCoordinator(llm_fn=flaky_llm)
        asyncio.run(coord._bound_llm([{"role": "user", "content": "hi"}]))
        assert len(calls) == 1  # mock 不重试

    def test_bound_llm_empty_retry_production(self, monkeypatch):
        """生产 llm (llm_call) 时 _bound_llm 对空响应带提示重试 (退避后 3 次)。"""
        import asyncio

        from server.coordinator_master import MasterCoordinator

        calls: list[list] = []

        async def flaky(messages, **kw):
            calls.append(list(messages))
            if len(calls) == 1:
                return {"choices": [{"message": {"role": "assistant",
                                                   "content": "None"}}],
                        "usage": {}}
            return {"choices": [{"message": {"role": "assistant",
                                               "content": "重试成功。"}}],
                    "usage": {}}

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)  # 免退避等待
        import server.coordinator_master as cm

        # 同时替换模块全局 llm_call 与实例 _llm_fn (两者必须同一对象,
        # _bound_llm 的 `self._llm_fn is llm_call` 闸门才命中重试分支)
        orig = cm.llm_call
        cm.llm_call = flaky  # type: ignore[assignment]
        try:
            coord = MasterCoordinator(llm_fn=flaky)
            resp = asyncio.run(coord._bound_llm(
                [{"role": "user", "content": "hi"}]))
        finally:
            cm.llm_call = orig
        assert len(calls) == 2, "空响应必须重试 (1 次空 + 1 次成功)"
        # 重试调用带温和提示
        assert "空/无效内容" in calls[1][-1]["content"]
        msg = ((resp.get("choices") or [{}])[0].get("message") or {})
        assert msg.get("content") == "重试成功。"

    def test_bound_llm_persistent_empty_returns_visible(self, monkeypatch):
        """连续空响应 (3 次全空) → 返回可见提示而非空白。"""
        import asyncio

        from server.coordinator_master import MasterCoordinator

        calls: list[int] = []

        async def always_empty(messages, **kw):
            calls.append(1)
            return {"choices": [{"message": {"role": "assistant",
                                               "content": ""}}],
                    "usage": {}}

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        import server.coordinator_master as cm

        orig = cm.llm_call
        cm.llm_call = always_empty  # type: ignore[assignment]
        try:
            coord = MasterCoordinator(llm_fn=always_empty)
            resp = asyncio.run(coord._bound_llm(
                [{"role": "user", "content": "hi"}]))
        finally:
            cm.llm_call = orig
        assert len(calls) == 3, "3 次退避重试后放弃"
        msg = ((resp.get("choices") or [{}])[0].get("message") or {})
        assert "网关抖动" in (msg.get("content") or "")
        assert (msg.get("content") or "").strip()

    def test_chat_stream_routes_natively_without_preempting(self):
        """任何请求都进原生 ReAct 循环 — 编程/视频/URL 关键词不再被程序截走。"""
        import asyncio

        from server.coordinator_master import MasterCoordinator

        seen: list[dict] = []

        async def fake_llm(messages, **kwargs):
            seen.append(kwargs)
            return {"choices": [{"message": {"role": "assistant",
                                               "content": "已原生理解并回答。"}}],
                    "usage": {}}

        coord = MasterCoordinator(llm_fn=fake_llm, max_rounds=3)
        r = asyncio.run(coord.chat_stream(
            "https://github.com/user/repo 帮我看看这个项目实现"))
        assert r["status"] == "success"
        assert "原生理解并回答" in r["final_answer"]
        # 工具面仍然注入 (模型可自主选择), 只是没有被强制 preempt
        assert seen and seen[0].get("tools") is not None

    def test_code_task_empty_result_hands_to_reasonix(self, monkeypatch):
        """收尾兜底 (非前置拦截): 编程强信号 + 模型空回复/未调 reasonix_run
        → 自动交 reasonix 执行, 保证任务不落空。"""
        import asyncio

        from server import reasonix_agent as ra_mod
        from server.coordinator_master import MasterCoordinator

        captured: dict = {}

        async def fake_reasonix(task, **kw):
            captured["task"] = task
            captured["on_event"] = kw.get("on_event")
            return "FAKE_EXEC: hello.py 已写入并运行通过"

        monkeypatch.setattr(ra_mod, "reasonix_run", fake_reasonix)

        async def empty_llm(messages, **kw):
            return {"choices": [{"message": {"role": "assistant", "content": ""}}],
                    "usage": {}}

        import server.coordinator_master as cm

        # 模拟生产身份: 模块全局 llm_call 与实例 _llm_fn 同一对象,
        # 收尾兜底仅生产默认 llm 启用 (mock 注入时保持工具循环语义)
        orig = cm.llm_call
        cm.llm_call = empty_llm  # type: ignore[assignment]
        try:
            coord = MasterCoordinator(llm_fn=empty_llm, max_rounds=2)
            r = asyncio.run(coord.chat_stream(
                "写一个 python 脚本读取 csv", session_id="s-fb"))
        finally:
            cm.llm_call = orig
        assert "FAKE_EXEC" in r["final_answer"]
        assert r.get("reasonix_execution")
        assert captured["task"] == "写一个 python 脚本读取 csv"
        assert captured["on_event"] is not None  # 进度事件桥接 SSE

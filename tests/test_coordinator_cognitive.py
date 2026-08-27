"""Veya Core 认知引擎测试 — 状态机 / 反思回路 / 上下文切片 / HITL。

覆盖三大认知组件:
1. 强约束系统提示词 (CORE RULES)
2. 带反思的执行循环 (沙箱报错回喂自纠,不回传前端)
3. 动态上下文切片 (旧轮次压缩为工作日志)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from server.coordinator import (
    COGNITIVE_SYSTEM_PROMPT,
    CognitivePhase,
    Coordinator,
    SquadPlan,
    ToolExecutionError,
    VeyaCoordinator,
)

# ---------------------------------------------------------------------------
# LLM 响应构造器(OpenAI 格式)
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
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


# ---------------------------------------------------------------------------
# 1. 强约束系统提示词
# ---------------------------------------------------------------------------


def test_system_prompt_core_rules():
    """CORE RULES 全部落地。"""
    assert "You are Veya Core" in COGNITIVE_SYSTEM_PROMPT
    assert "THINK BEFORE YOU ACT" in COGNITIVE_SYSTEM_PROMPT
    assert "BE PARSIMONIOUS" in COGNITIVE_SYSTEM_PROMPT
    assert "VERIFY EVERYTHING" in COGNITIVE_SYSTEM_PROMPT
    assert "SELF-CORRECTION" in COGNITIVE_SYSTEM_PROMPT
    assert "NO HALLUCINATION" in COGNITIVE_SYSTEM_PROMPT
    assert (
        "Analyze Request -> Discover Context -> Formulate Plan -> Execute -> Test in Sandbox -> Finish"
        in COGNITIVE_SYSTEM_PROMPT
    )
    assert "patch_file" in COGNITIVE_SYSTEM_PROMPT
    assert "run_in_sandbox" in COGNITIVE_SYSTEM_PROMPT


def test_veya_coordinator_injects_system_prompt():
    engine = VeyaCoordinator()
    assert engine._load_system_prompt() == COGNITIVE_SYSTEM_PROMPT
    custom = "You are a custom brain."
    assert VeyaCoordinator(system_prompt=custom)._load_system_prompt() == custom


# ---------------------------------------------------------------------------
# 2. 带反思的执行循环 (ReAct Loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_cognitive_loop(tmp_path):
    """Discovery → Planning → Execution → Finish 全链路。"""
    (tmp_path / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"
    )
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))  # 快照,避免后续轮次污染断言
        turn = len(calls)
        if turn == 1:
            return _tool_response("ast_search", {"query": "add"})
        if turn == 2:
            return _tool_response(
                "submit_plan",
                {
                    "steps_json": json.dumps(
                        [
                            {"step": 1, "action": "locate add()"},
                            {"step": 2, "action": "verify in sandbox"},
                        ]
                    )
                },
            )
        if turn == 3:
            return _tool_response("run_in_sandbox", {"code": "print(1 + 1)"})
        return _tool_response("finish", {"answer": "task complete: add() verified"})

    engine = VeyaCoordinator(max_retries=5, llm_fn=fake_llm)
    result = await engine.execute_task(
        "verify the add function", session_id="s1", project_path=str(tmp_path)
    )

    assert result["status"] == "success"
    assert result["final_answer"] == "task complete: add() verified"
    assert result["rounds"] == 4
    assert result["decision_trail"]["steps"][0]["step"] == 1
    assert engine._phase == CognitivePhase.DONE
    # 成功观察已回喂(第二轮 LLM 看到的最后一条是 ast_search 结果)
    assert "[Tool ast_search SUCCESS]" in calls[1][-1]["content"]
    assert "def add(a, b)" in calls[1][-1]["content"]


@pytest.mark.asyncio
async def test_reflection_loop_on_sandbox_failure(tmp_path):
    """沙箱报错被拦截 → 包装成反思提示词回喂模型 → 模型重试成功。"""
    (tmp_path / "app.py").write_text("def main():\n    return 42\n")
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        turn = len(calls)
        if turn == 1:
            return _tool_response("read_skeleton", {"filepath": "app.py"})
        if turn == 2:
            return _tool_response("run_in_sandbox", {"code": "import app\nprint(app.main())"})
        if turn == 3:
            return _tool_response("run_in_sandbox", {"code": "print('fixed')"})
        return _tool_response("finish", {"answer": "fixed and verified"})

    engine = VeyaCoordinator(max_retries=5, llm_fn=fake_llm)
    real_sandbox = engine._tool_run_sandbox
    state = {"failed": False}

    async def flaky(code=None, command=None, timeout=None):
        if not state["failed"]:
            state["failed"] = True
            raise ToolExecutionError(
                "exit_code=1\nstdout:\n\nstderr:\nModuleNotFoundError: No module named 'app'"
            )
        return await real_sandbox(code=code, command=command, timeout=timeout)

    engine._tool_run_sandbox = flaky  # type: ignore[method-assign]
    result = await engine.execute_task("run app.py", session_id="s2", project_path=str(tmp_path))

    assert result["status"] == "success"
    # 反思消息已回喂给模型(第 3 次调用的最后一条 tool 消息)
    assert "[Tool run_in_sandbox FAILED]" in calls[2][-1]["content"]
    assert "ModuleNotFoundError" in calls[2][-1]["content"]
    assert "请仔细分析上述报错" in calls[2][-1]["content"]
    assert engine._phase == CognitivePhase.DONE


@pytest.mark.asyncio
async def test_hitl_after_max_retries(tmp_path):
    """超过最大重试次数 → 判定死胡同,向前端抛出 HITL(人工介入)。"""
    calls = []

    async def failing_llm(messages, **kwargs):
        calls.append(list(messages))
        return _tool_response("run_in_sandbox", {"code": "raise RuntimeError('boom')"})

    engine = VeyaCoordinator(max_retries=3, llm_fn=failing_llm)

    async def always_fail(code=None, command=None, timeout=None):
        raise ToolExecutionError("exit_code=1\nstderr:\nRuntimeError: boom")

    engine._tool_run_sandbox = always_fail  # type: ignore[method-assign]
    result = await engine.execute_task(
        "do the impossible", session_id="s3", project_path=str(tmp_path)
    )

    assert result["status"] == "failed"
    assert result["hitl"] is True
    assert "人工介入" in result["error"]
    assert "死胡同" in result["error"]
    assert result["rounds"] == 3
    assert result["max_rounds"] == 3
    assert len(result["last_messages"]) == 3
    assert engine._phase == CognitivePhase.REFLECTION
    # 每一轮失败都以反思提示词回喂
    for messages in calls[1:]:
        assert "请仔细分析上述报错" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_planning_transition(tmp_path):
    """submit_plan → PLANNING 阶段 + Decision Trail 落库。"""

    async def fake_llm(messages, **kwargs):
        turn = len([m for m in messages if m.get("role") == "assistant"])
        if turn == 0:
            return _tool_response(
                "submit_plan",
                {"steps_json": '[{"step":1,"action":"a"},{"step":2,"action":"b"}]'},
            )
        return _text_response("plan is ready")

    engine = VeyaCoordinator(max_retries=3, llm_fn=fake_llm)
    result = await engine.execute_task(
        "plan something", session_id="p1", project_path=str(tmp_path)
    )

    assert result["status"] == "success"
    assert engine._phase == CognitivePhase.DONE
    assert engine.decision_trail.steps[0]["action"] == "a"
    assert result["decision_trail"]["steps"][1]["step"] == 2


# ---------------------------------------------------------------------------
# 3. 动态上下文切片
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_slicing_compresses_old_rounds(tmp_path):
    """超 token 预算时,旧工具轮次被压缩为 WORK LOG,消息数受控。"""
    big = "\n".join(f"padding line {i} " + "x" * 60 for i in range(400))
    (tmp_path / "big.py").write_text(big)
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        turn = len(calls)
        if turn < 4:
            return _tool_response("read_file", {"filepath": "big.py"})
        return _tool_response("finish", {"answer": "done"})

    engine = VeyaCoordinator(max_retries=5, llm_fn=fake_llm, max_context_tokens=400)
    result = await engine.execute_task("task", session_id="s4", project_path=str(tmp_path))

    assert result["status"] == "success"
    assert engine._work_log, "expected work log entries after context slicing"
    assert "called: read_file" in engine._work_log[0]
    # 系统消息携带压缩后的工作日志
    sys_msg = calls[-1][0]["content"]
    assert "WORK LOG" in sys_msg
    # 消息数受控(系统 + 首条 user + 当前轮 + 收尾)
    assert len(calls[-1]) <= 6


def test_slice_context_preserves_message_pairing():
    """整轮压缩: assistant tool_calls 与其 tool 结果必须成对移除。"""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "[Tool read_file SUCCESS]\nResult:\n" + "z" * 2000,
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "finish", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "[Tool finish SUCCESS]"},
    ]
    engine = VeyaCoordinator(max_context_tokens=100)
    engine._slice_context(messages)
    # 第一轮(索引2-3)被移除;第二轮保持完整(否则违反配对约束)
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"
    assert messages[2]["tool_calls"][0]["function"]["name"] == "finish"
    assert messages[3]["role"] == "tool"
    assert engine._work_log and "read_file" in engine._work_log[0]
    assert "WORK LOG" in messages[0]["content"]


# ---------------------------------------------------------------------------
# 武器库工具(真实执行)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_skeleton_compression(tmp_path):
    """AST 骨架压缩: 只保留签名/行号/docstring 首行,函数体被丢弃。"""
    src = (
        "import os\n\n"
        "def greet(name: str, punctuation: str = '!') -> str:\n"
        '    """Greet a user by name."""\n'
        "    return f'Hello, {name}{punctuation}'\n\n"
        "class Calculator:\n"
        "    def add(self, a, b):\n"
        "        return a + b\n"
    )
    (tmp_path / "calc.py").write_text(src)
    engine = VeyaCoordinator()
    engine._project_path = str(tmp_path)

    skel = await engine._tool_read_skeleton("calc.py")
    assert "def greet(" in skel
    assert "class Calculator" in skel
    assert "def add(" in skel
    assert "Greet a user" in skel  # docstring 首行保留
    assert "return f'Hello" not in skel  # 函数体被压缩


@pytest.mark.asyncio
async def test_ast_search_discovers_symbols(tmp_path):
    (tmp_path / "service.py").write_text(
        'def fetch_user(user_id):\n    """Fetch a user by id."""\n    return {}\n'
    )
    engine = VeyaCoordinator()
    engine._project_path = str(tmp_path)

    out = await engine._tool_ast_search("fetch_user")
    assert "function fetch_user" in out
    assert "service.py" in out

    out2 = await engine._tool_ast_search("Fetch a user")
    assert "fetch_user" in out2  # docstring 关键词命中


@pytest.mark.asyncio
async def test_grep_tool(tmp_path):
    (tmp_path / "a.py").write_text("def target_fn():\n    pass\n")
    engine = VeyaCoordinator()
    engine._project_path = str(tmp_path)

    out = await engine._tool_grep("target_fn")
    assert "a.py:1" in out

    out2 = await engine._tool_grep("no_such_thing_xyz")
    assert "no matches" in out2


@pytest.mark.asyncio
async def test_patch_file_parsimony_and_path_guard(tmp_path):
    """patch_file 精确替换 + 歧义拒绝 + 路径逃逸拒绝。"""
    target = tmp_path / "app.py"
    target.write_text("def hello():\n    return 'old'\n")
    engine = VeyaCoordinator()
    engine._project_path = str(tmp_path)

    out = await engine._tool_patch_file("app.py", "return 'old'", "return 'new'")
    assert "patched app.py" in out
    assert "return 'new'" in target.read_text()

    # old_text 不存在 → 反思异常
    with pytest.raises(ToolExecutionError, match="not found"):
        await engine._tool_patch_file("app.py", "def missing():", "x")

    # old_text 歧义 → 反思异常
    target.write_text("x = 1\nx = 1\n")
    with pytest.raises(ToolExecutionError, match="ambiguous"):
        await engine._tool_patch_file("app.py", "x = 1", "x = 2")

    # 路径逃逸项目根 → 拒绝 (NO HALLUCINATION)
    with pytest.raises(ToolExecutionError, match="escapes project root"):
        await engine._tool_patch_file(str(tmp_path.parent / "outside.py"), "a", "b")


@pytest.mark.asyncio
async def test_write_file_and_list_files(tmp_path):
    engine = VeyaCoordinator()
    engine._project_path = str(tmp_path)

    out = await engine._tool_write_file("pkg/mod.py", "VALUE = 1\n")
    assert "wrote pkg/mod.py" in out

    listing = await engine._tool_list_files()
    assert "pkg/mod.py" in listing


@pytest.mark.asyncio
async def test_real_sandbox_execution(tmp_path):
    """真实 3O 沙箱: 成功返回 stdout;TypeError 触发反思异常。"""
    engine = VeyaCoordinator()
    engine._project_path = str(tmp_path)

    out = await engine._tool_run_sandbox(code="print(1 + 1)")
    assert "exit_code=0" in out
    assert "2" in out

    with pytest.raises(ToolExecutionError) as exc_info:
        await engine._tool_run_sandbox(code="raise TypeError('bad')")
    assert "TypeError: bad" in str(exc_info.value)


@pytest.mark.asyncio
async def test_sandbox_oom_triggers_reflection(tmp_path):
    """OOM(内存超限)同样被拦截为反思异常。"""
    engine = VeyaCoordinator(sandbox_memory_limit=16 * 1024 * 1024)
    engine._project_path = str(tmp_path)

    with pytest.raises(ToolExecutionError):
        await engine._tool_run_sandbox(code="x = 'a' * 64 * 1024 * 1024")


# ---------------------------------------------------------------------------
# Coordinator 集成(向后兼容)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_handle_cognitive_routing():
    """mode=\"cognitive\" 命令路由进认知引擎;普通命令不受影响。"""
    coordinator = Coordinator()
    fake = AsyncMock()
    fake.execute_task.return_value = {"status": "success", "final_answer": "ok", "rounds": 2}
    with patch.object(coordinator, "_make_cognitive_engine", return_value=fake):
        result = await coordinator.handle({"text": "hello", "mode": "cognitive"}, session_id="sess")
    assert result["status"] == "success"
    assert result["session_id"] == "sess"
    fake.execute_task.assert_awaited_once_with(
        "hello",
        session_id="sess",
        project_path=".",
        config={"model": None, "provider": None},
    )

    # 普通命令不进入认知模式
    fake.reset_mock()
    with patch.object(coordinator, "_make_cognitive_engine", return_value=fake) as m:
        coordinator.ast_analyzer = _FakeAnalyzer()

        async def fake_decompose(command, *, cost):
            return SquadPlan(squads=[], schedule="parallel")

        async def fake_run_squads(orchestrator, plan, *, session_id=None, command=None):
            return []

        with (
            patch.object(coordinator, "_decompose", new=fake_decompose),
            patch.object(coordinator, "_run_squads", new=fake_run_squads),
        ):
            await coordinator.handle({"text": "hi"}, session_id="s2")
        m.assert_not_called()


class _FakeAnalyzer:
    """替换 AST 分析器,避免测试扫描整个仓库。"""

    def __init__(self):
        self.symbols: dict = {}

    def analyze_project(self, project_path):
        return {
            "symbol_count": 0,
            "dependency_count": 0,
            "modules_count": 0,
            "scan_time": 0.0,
            "cache_valid": False,
        }

    def predict_relevant_files(self, query, all_files, max_files=5):
        return []


@pytest.mark.asyncio
async def test_coordinator_handle_cognitive_direct():
    """handle_cognitive 直接调用,返回认知引擎结构化结果。"""
    coordinator = Coordinator(settings={"cognitive_max_retries": 3})
    engine = coordinator._make_cognitive_engine()
    assert engine.max_retries == 3

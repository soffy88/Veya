"""阶段 4 回归: omodul 注入式流程控制核心 (session_tree / tool_pipeline /
agent_loop / evidence_refine + 双轨 bridge)。

核心断言:
- session_tree: branching / 时空回溯 (path/fork) / 快照恢复
- tool_pipeline: 五步管道 (解析→校验→权限→执行→包装) + 幻觉拦截 (坏 JSON/
  参数不合格拒绝) + 全步骤 audit
- agent_loop: 端到端 (假 LLM 生成→工具→停止) / 熔断退避 / 无效回复
- evidence_refine: 静态检查 + 沙箱执行证据 + 修复提示
- bridge: feature flag 默认关 + run_strict 端到端
"""

from __future__ import annotations

from typing import Any

import pytest

from veya.omodul.agent_loop import AgentLoop
from veya.omodul.evidence_refine import EvidenceRefine
from veya.omodul.session_tree import SessionTreeMgr
from veya.omodul.tool_pipeline import ToolPipeline
from server.agent_loop_bridge import run_strict, strict_loop_enabled


class FakeLlm:
    """按轮次剧本返回的假 LLM（complete 实现）。"""

    def __init__(self, script: list[dict]) -> None:
        self._script = script
        self._calls = 0

    async def complete(self, messages: list[dict], **kwargs: Any) -> dict:
        assert self._calls < len(self._script), "剧本耗尽"
        reply = self._script[self._calls]
        self._calls += 1
        return {"choices": [{"message": reply}]}

    async def close(self) -> None:
        pass

    @property
    def calls(self) -> int:
        return self._calls


def _tool_call_msg(name: str, args: dict, content: str = "thinking") -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        ],
    }


# ---------------------------------------------------------------------------
# session_tree
# ---------------------------------------------------------------------------


def test_tree_append_path_and_leaf():
    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv, id_fn=_seq_id())
    sid = tree.create_session(system="sys")
    tree.append(sid, role="user", content="hello")
    tree.append(sid, role="assistant", content="hi there")
    leaf = tree.leaf(sid)
    assert leaf["role"] == "assistant"
    roles = [n["role"] for n in tree.path(sid)]
    assert roles == ["system", "user", "assistant"]
    msgs = tree.messages(sid)
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[-1]["content"] == "hi there"


def test_tree_branch_changes_leaf_only():
    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv, id_fn=_seq_id())
    sid = tree.create_session(system="sys")
    n1 = tree.append(sid, role="user", content="q1")
    n2 = tree.append(sid, role="assistant", content="a1")
    # 从 n1 分支出另一条回答
    nb = tree.branch(sid, at_node_id=n1, role="assistant", content="a1-alt")
    assert tree.leaf(sid)["id"] == nb
    assert tree.leaf(sid)["content"] == "a1-alt"
    # 原路径仍可达
    roles = [n["role"] for n in tree.path(sid, node_id=n2)]
    assert [n["content"] for n in tree.path(sid, node_id=n2)] == ["sys", "q1", "a1"]
    assert roles == ["system", "user", "assistant"]


def test_tree_fork_spacetime_rewind():
    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv, id_fn=_seq_id())
    sid = tree.create_session(system="sys")
    n1 = tree.append(sid, role="user", content="q1")
    tree.append(sid, role="assistant", content="a1")
    tree.append(sid, role="user", content="q2")
    # 回溯到 n1 分叉新会话
    sid2 = tree.fork(sid, at_node_id=n1)
    assert sid2 != sid
    contents = [n["content"] for n in tree.path(sid2)]
    assert contents == ["sys", "q1"]
    assert tree.leaf(sid2)["content"] == "q1"
    # 原会话不受影响
    assert len(tree.path(sid)) == 4


def test_tree_snapshot_restore_and_errors():
    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv, id_fn=_seq_id())
    sid = tree.create_session(system="sys")
    tree.append(sid, role="user", content="q1")
    snap = tree.snapshot(sid)
    tree.append(sid, role="assistant", content="a1")
    tree.restore(sid, snap)
    assert [n["content"] for n in tree.path(sid)] == ["sys", "q1"]
    with pytest.raises(ValueError):
        tree.append(sid, role="user", content="x", parent_id="no-such-node")
    with pytest.raises(ValueError):
        tree.append(sid, role="bogus", content="x")


# ---------------------------------------------------------------------------
# tool_pipeline — 五步管道
# ---------------------------------------------------------------------------


def _mem_kv():
    from veya.obase.adapters import SqliteKvStore

    return SqliteKvStore()


def _seq_id():
    counter = [0]

    def _next() -> str:
        counter[0] += 1
        return f"id{counter[0]:04d}"

    return _next


@pytest.fixture
def pipeline():
    return ToolPipeline()


def test_pipeline_executes_valid_call(pipeline):
    calls = {"echo": lambda text: f"echo:{text}"}
    for name, fn in calls.items():
        pipeline.register(name, fn, schema={"type": "object", "properties": {"text": {"type": "string"}}})
    res = _run(pipeline, _tool_call_msg("echo", {"text": "hi"}))[0]
    assert res.ok
    assert res.output == "echo:hi"
    assert res.error == ""
    stages = [a["stage"] for a in res.audit]
    assert stages == ["parse", "validate", "authorize", "exec", "wrap"]


def test_pipeline_rejects_bad_json(pipeline):
    """幻觉拦截: 坏 JSON arguments → parse 阶段拒绝。"""
    msg = {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "echo", "arguments": "{broken"}}]}
    res = _run(pipeline, msg)[0]
    assert not res.ok
    assert res.rejected and res.reject_stage == "parse"


def test_pipeline_rejects_schema_violation(pipeline):
    """幻觉拦截: 参数不合格 → validate 阶段拒绝, 工具绝不执行。"""
    executed = []

    def boom(**kw):
        executed.append(kw)
        return "should-not-run"

    pipeline.register("boom", boom, schema={
        "type": "object",
        "required": ["x"],
        "properties": {"x": {"type": "integer", "minimum": 1}},
    })
    res = _run(pipeline, _tool_call_msg("boom", {"x": "not-int"}))[0]
    assert not res.ok
    assert res.rejected and res.reject_stage == "validate"
    assert executed == []  # 未执行


def test_pipeline_rejects_unknown_tool(pipeline):
    res = _run(pipeline, _tool_call_msg("nope", {}))[0]
    assert not res.ok
    assert res.rejected and res.reject_stage == "validate"
    assert "未注册" in res.error


def test_pipeline_permit_gate(pipeline):
    pipeline.register("secret", lambda: "data", schema={"type": "object"})
    pipeline2 = ToolPipeline(permit=lambda name, args: name != "secret")
    pipeline2.register("secret", lambda: "data", schema={"type": "object"})
    res = _run(pipeline2, _tool_call_msg("secret", {}))[0]
    assert not res.ok
    assert res.rejected and res.reject_stage == "authorize"
    # 允许侧: 默认 permit=None 放行
    res_ok = _run(pipeline, _tool_call_msg("secret", {}))[0]
    assert res_ok.ok


def test_pipeline_wraps_exception(pipeline):
    def explode(**kw):
        raise RuntimeError("kaboom")

    pipeline.register("explode", explode, schema={"type": "object"})
    res = _run(pipeline, _tool_call_msg("explode", {}))[0]
    assert not res.ok
    assert not res.rejected
    assert "kaboom" in res.error


def test_pipeline_async_tool(pipeline):
    async def slow(**kw):
        return "async-ok"

    pipeline.register("slow", slow, schema={"type": "object"})
    res = _run(pipeline, _tool_call_msg("slow", {}))[0]
    assert res.ok and res.output == "async-ok"


def test_pipeline_non_serializable_output_wrapped(pipeline):
    class Weird:
        pass

    pipeline.register("weird", lambda: Weird(), schema={"type": "object"})
    res = _run(pipeline, _tool_call_msg("weird", {}))[0]
    assert res.ok
    assert isinstance(res.output, str)


def _run(pipeline, tool_calls_or_message):
    """同步驱动管道: 接受 OpenAI tool_calls 列表或完整消息 dict。"""
    import asyncio

    if isinstance(tool_calls_or_message, dict) and "tool_calls" in tool_calls_or_message:
        message = tool_calls_or_message
    else:
        message = {"role": "assistant", "content": "", "tool_calls": tool_calls_or_message}
    return asyncio.run(pipeline.run_message(message))


# ---------------------------------------------------------------------------
# agent_loop — 注入式心脏
# ---------------------------------------------------------------------------


def _new_tree() -> SessionTreeMgr:
    return SessionTreeMgr(kv=_mem_kv())


@pytest.mark.asyncio
async def test_agent_loop_end_to_end():
    """剧本: 调工具 → 拿到结果 → 直接回答。"""
    llm = FakeLlm([
        _tool_call_msg("add", {"a": 1, "b": 2}),
        {"role": "assistant", "content": "结果是 3"},
    ])
    pipeline = ToolPipeline()
    pipeline.register("add", lambda a, b: a + b,
                      schema={"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}})
    tree = _new_tree()
    loop = AgentLoop(llm=llm, pipeline=pipeline, tree=tree, system_prompt="sys")
    result = await loop.run("1+2 等于多少?")
    assert result.stop_kind == "completed"
    assert result.final_answer == "结果是 3"
    assert result.rounds == 2
    assert result.tool_calls == 1
    assert result.tool_failures == 0
    # 会话树完整: system/user/assistant(tool_call)/tool/assistant
    roles = [n["role"] for n in tree.path(result.session_id)]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert result.snapshot is not None


@pytest.mark.asyncio
async def test_agent_loop_circuit_breaker():
    """工具连续失败 → 熔断提前停止 (fatal_error)。"""
    llm = FakeLlm([
        _tool_call_msg("bad", {}),
        _tool_call_msg("bad", {}),
        _tool_call_msg("bad", {}),
    ])
    pipeline = ToolPipeline()
    pipeline.register("bad", lambda: (_ for _ in ()).throw(RuntimeError("fail")),
                      schema={"type": "object"})
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    loop = AgentLoop(llm=llm, pipeline=pipeline, tree=_new_tree(),
                     max_consecutive_errors=3, backoff_sleep=0.5, sleep_fn=fake_sleep)
    result = await loop.run("触发熔断")
    assert result.stop_kind == "fatal_error"
    assert "熔断" in result.stop_reason
    assert result.tool_failures == 3
    assert slept == [0.5]  # 退避发生


@pytest.mark.asyncio
async def test_agent_loop_invalid_response():
    """模型返回空/疲劳回复 → invalid_response 停止。"""
    llm = FakeLlm([{"role": "assistant", "content": "none"}])
    loop = AgentLoop(llm=llm, pipeline=ToolPipeline(), tree=_new_tree())
    result = await loop.run("hi")
    assert result.stop_kind == "invalid_response"


@pytest.mark.asyncio
async def test_agent_loop_max_rounds():
    """模型每轮都调工具 → 达到轮次上限停止。"""
    llm = FakeLlm([_tool_call_msg("ping", {}) for _ in range(5)])
    pipeline = ToolPipeline()
    pipeline.register("ping", lambda: "pong", schema={"type": "object"})
    loop = AgentLoop(llm=llm, pipeline=pipeline, tree=_new_tree(), max_rounds=3)
    result = await loop.run("一直调用")
    assert result.stop_kind == "max_rounds"
    assert result.rounds == 3


@pytest.mark.asyncio
async def test_agent_loop_llm_failure():
    class BoomLlm:
        async def complete(self, messages, **kw):
            raise ConnectionError("network down")

    loop = AgentLoop(llm=BoomLlm(), pipeline=ToolPipeline(), tree=_new_tree())
    result = await loop.run("hi")
    assert result.stop_kind == "fatal_error"
    assert "network down" in result.error


# ---------------------------------------------------------------------------
# evidence_refine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_refine_good_code():
    er = EvidenceRefine()
    result = await er.verify("print('ok')")
    assert result.ok
    assert "ok" in result.output
    assert result.iterations == 1


@pytest.mark.asyncio
async def test_evidence_refine_syntax_error():
    er = EvidenceRefine()
    result = await er.verify("def broken(:\n    pass")
    assert not result.ok
    assert "语法检查失败" in result.error
    assert "SYNTAX ERROR" in result.evidence


@pytest.mark.asyncio
async def test_evidence_refine_runtime_evidence():
    er = EvidenceRefine()
    result = await er.verify("raise ValueError('boom-refine')")
    assert not result.ok
    assert "ValueError" in result.evidence
    hint = EvidenceRefine.build_fix_hint(result.evidence, code="raise ValueError('boom-refine')")
    assert "验证证据" in hint and "boom-refine" in hint


# ---------------------------------------------------------------------------
# bridge — 双轨
# ---------------------------------------------------------------------------


def test_strict_loop_flag_default_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VEYA_AGENT_LOOP", raising=False)
    assert strict_loop_enabled() is False
    monkeypatch.setenv("VEYA_AGENT_LOOP", "strict")
    assert strict_loop_enabled() is True


@pytest.mark.asyncio
async def test_run_strict_bridge_end_to_end():
    """bridge: 假 LLM + 工具注入, 新心脏跑通 (旧主链不受影响)。"""
    llm = FakeLlm([
        _tool_call_msg("greet", {"who": "world"}),
        {"role": "assistant", "content": "hello world"},
    ])
    result = await run_strict(
        "打招呼",
        tools={"greet": (lambda who: f"hello {who}", {
            "type": "object",
            "properties": {"who": {"type": "string"}},
        })},
        llm=llm,
        system_prompt="sys",
    )
    assert result.stop_kind == "completed"
    assert result.final_answer == "hello world"
    assert result.tool_calls == 1

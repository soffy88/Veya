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

from server.agent_loop_bridge import run_strict
from veya.omodul.agent_loop import AgentLoop
from veya.omodul.evidence_refine import EvidenceRefine
from veya.omodul.session_tree import SessionTreeMgr
from veya.omodul.tool_pipeline import ToolPipeline


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


def test_tree_list_branches_enumerates_all_leaves():
    """对标 Maka"事实源不可变"原则的差距审计(见 memory project_veya_pi_gap_audit):
    branch() 出的旧分支节点保留不删, 但 path()/leaf() 只看当前活跃叶——
    list_branches() 是找回其它分支(比如 Compaction 覆盖前的原文)的入口。
    """
    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv, id_fn=_seq_id())
    sid = tree.create_session(system="sys")
    n1 = tree.append(sid, role="user", content="q1")
    tree.append(sid, role="assistant", content="a1")
    tree.branch(sid, at_node_id=n1, role="assistant", content="a1-alt")

    branches = tree.list_branches(sid)
    contents = sorted([n["content"] for n in b] for b in branches)
    assert contents == [
        ["sys", "q1", "a1"],
        ["sys", "q1", "a1-alt"],
    ]


def test_tree_list_branches_single_leaf_is_one_branch():
    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv, id_fn=_seq_id())
    sid = tree.create_session(system="sys")
    tree.append(sid, role="user", content="q1")

    branches = tree.list_branches(sid)
    assert len(branches) == 1
    assert [n["content"] for n in branches[0]] == ["sys", "q1"]


def test_tree_list_branches_missing_session_returns_empty():
    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv, id_fn=_seq_id())
    assert tree.list_branches("nope") == []


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


# ── owner 归属校验回归 (2026-08-16): 此前 SessionTreeMgr 完全没有 user_id ──
# 概念, 拿到/猜到别人的 sid 就能续接读写会话树。


def test_create_session_records_owner():
    tree = SessionTreeMgr(kv=_mem_kv(), id_fn=_seq_id())
    sid = tree.create_session(system="sys", owner="alice")
    assert tree.owner_of(sid) == "alice"


def test_ensure_session_creates_with_owner_when_missing():
    tree = SessionTreeMgr(kv=_mem_kv(), id_fn=_seq_id())
    tree.ensure_session("ext-sid", owner="alice")
    assert tree.owner_of("ext-sid") == "alice"


def test_ensure_session_same_owner_reconnects_fine():
    tree = SessionTreeMgr(kv=_mem_kv(), id_fn=_seq_id())
    sid = tree.create_session(system="sys", owner="alice")
    tree.append(sid, role="user", content="q1")
    tree.ensure_session(sid, owner="alice")  # 同一账号续接, 不应报错/不应丢数据
    assert [n["content"] for n in tree.path(sid)] == ["sys", "q1"]


def test_ensure_session_rejects_mismatched_owner():
    """回归: 拿到别人的 sid, 传自己的 owner 续接, 必须被拒绝。"""
    tree = SessionTreeMgr(kv=_mem_kv(), id_fn=_seq_id())
    sid = tree.create_session(system="sys", owner="alice")
    with pytest.raises(PermissionError):
        tree.ensure_session(sid, owner="bob")


def test_ensure_session_owner_none_does_not_enforce():
    """未传 owner (旧调用方) 完全不受影响——向后兼容。"""
    tree = SessionTreeMgr(kv=_mem_kv(), id_fn=_seq_id())
    sid = tree.create_session(system="sys", owner="alice")
    tree.ensure_session(sid)  # owner=None, 不校验
    assert tree.owner_of(sid) == "alice"  # 也不会被覆盖成 None


def test_fork_inherits_owner():
    tree = SessionTreeMgr(kv=_mem_kv(), id_fn=_seq_id())
    sid = tree.create_session(system="sys", owner="alice")
    n1 = tree.append(sid, role="user", content="q1")
    new_sid = tree.fork(sid, at_node_id=n1)
    assert tree.owner_of(new_sid) == "alice"


def test_owner_of_missing_session_returns_none():
    tree = SessionTreeMgr(kv=_mem_kv(), id_fn=_seq_id())
    assert tree.owner_of("does-not-exist") is None


# ── list_sessions 回归 (2026-08-16): 多端同步接口的新数据源 ──────────────
# 此前 /api/v1/agent/sessions 读 history_store.py, 但新主链
# (VEYA_AGENT_LOOP=strict) 的对话只写进 session_tree.db, 两边从未打通。


def test_list_sessions_filters_by_owner():
    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv)
    sid_a = tree.create_session(system="sys", owner="alice")
    tree.append(sid_a, role="user", content="alice 的问题")
    sid_b = tree.create_session(system="sys", owner="bob")
    tree.append(sid_b, role="user", content="bob 的问题")

    alice_sessions = tree.list_sessions(owner="alice")
    assert {s["sid"] for s in alice_sessions} == {sid_a}
    bob_sessions = tree.list_sessions(owner="bob")
    assert {s["sid"] for s in bob_sessions} == {sid_b}


def test_list_sessions_ownerless_sessions_excluded_from_specific_owner():
    """回归核心: 没有 owner 记录的旧会话不该出现在任何具体账号的列表里。"""
    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv)
    tree.create_session(system="sys")  # owner=None, 早于归属修复的旧数据

    assert tree.list_sessions(owner="alice") == []


def test_list_sessions_title_from_first_user_message():
    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv)
    sid = tree.create_session(system="sys", owner="alice")
    tree.append(sid, role="user", content="帮我看看这段代码")
    tree.append(sid, role="assistant", content="好的")

    sessions = tree.list_sessions(owner="alice")
    assert sessions[0]["title"] == "帮我看看这段代码"
    assert sessions[0]["msg_count"] == 2  # user + assistant (不含 system)


def test_list_sessions_sorted_by_most_recently_updated():
    import time

    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv)
    older = tree.create_session(system="sys", owner="alice")
    tree.append(older, role="user", content="第一个会话")
    time.sleep(0.01)  # 保证 ts 有区分度, 避免同毫秒排序不稳
    newer = tree.create_session(system="sys", owner="alice")
    tree.append(newer, role="user", content="第二个会话")

    sessions = tree.list_sessions(owner="alice")
    assert [s["sid"] for s in sessions] == [newer, older]


def test_list_sessions_respects_limit():
    kv = _mem_kv()
    tree = SessionTreeMgr(kv=kv)
    for _ in range(5):
        tree.create_session(system="sys", owner="alice")
    assert len(tree.list_sessions(owner="alice", limit=2)) == 2


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
        pipeline.register(
            name, fn, schema={"type": "object", "properties": {"text": {"type": "string"}}}
        )
    res = _run(pipeline, _tool_call_msg("echo", {"text": "hi"}))[0]
    assert res.ok
    assert res.output == "echo:hi"
    assert res.error == ""
    stages = [a["stage"] for a in res.audit]
    assert stages == ["parse", "validate", "authorize", "exec", "wrap"]


def test_pipeline_rejects_bad_json(pipeline):
    """幻觉拦截: 坏 JSON arguments → parse 阶段拒绝。"""
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "echo", "arguments": "{broken"}}],
    }
    res = _run(pipeline, msg)[0]
    assert not res.ok
    assert res.rejected and res.reject_stage == "parse"


def test_pipeline_rejects_schema_violation(pipeline):
    """幻觉拦截: 参数不合格 → validate 阶段拒绝, 工具绝不执行。"""
    executed = []

    def boom(**kw):
        executed.append(kw)
        return "should-not-run"

    pipeline.register(
        "boom",
        boom,
        schema={
            "type": "object",
            "required": ["x"],
            "properties": {"x": {"type": "integer", "minimum": 1}},
        },
    )
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
async def test_agent_loop_sends_tools_to_llm():
    """AgentLoop 调 LLM 时必须携带 OpenAI 格式 tools 声明（否则模型不会返回
    结构化 tool_calls，只会用 XML 文本模拟）。"""
    from veya.omodul.agent_loop import AgentLoop
    from veya.omodul.session_tree import SessionTreeMgr
    from veya.omodul.tool_pipeline import ToolPipeline

    seen: dict = {}

    class CaptureLlm:
        async def complete(self, messages, **kw):
            seen.update(kw)
            return {"choices": [{"message": {"role": "assistant", "content": "直接回答完成"}}]}

        async def close(self):
            pass

    pipeline = ToolPipeline()
    pipeline.register(
        "greet",
        lambda who: f"hi {who}",
        schema={"type": "object", "properties": {"who": {"type": "string"}}},
        description="打招呼",
    )
    loop = AgentLoop(llm=CaptureLlm(), pipeline=pipeline, tree=SessionTreeMgr(kv=_mem_kv()))
    result = await loop.run("hi")
    assert result.final_answer == "直接回答完成"
    assert "tools" in seen
    tools = seen["tools"]
    assert tools[0]["function"]["name"] == "greet"
    assert tools[0]["function"]["description"] == "打招呼"
    assert tools[0]["function"]["parameters"]["properties"]["who"]["type"] == "string"


def _mem_kv():
    from veya.obase.adapters import SqliteKvStore

    return SqliteKvStore()


@pytest.mark.asyncio
async def test_agent_loop_context_providers_and_on_finish():
    """context_providers 每轮注入 + on_finish 结束回调（记忆/蒸馏桥）。"""
    from veya.omodul.agent_loop import AgentLoop
    from veya.omodul.session_tree import SessionTreeMgr
    from veya.omodul.tool_pipeline import ToolPipeline

    injected: list[str] = []
    finished: list[tuple[str, int]] = []

    class FakeLlm2:
        def __init__(self):
            self._calls = 0

        async def complete(self, messages, **kw):
            self._calls += 1
            for m in messages:
                if m.get("role") == "system" and m.get("content", "").startswith("# MEMORY"):
                    injected.append(m["content"])
            return {
                "choices": [{"message": {"role": "assistant", "content": f"第{self._calls}轮完成"}}]
            }

        async def close(self):
            pass

    async def mem_provider(sid: str, query: str) -> str:
        return "# MEMORY (关于用户): 用户喜欢简洁回答"

    async def finish_cb(sid: str, msgs: list[dict]) -> None:
        finished.append((sid, len(msgs)))

    llm = FakeLlm2()
    loop = AgentLoop(
        llm=llm,
        pipeline=ToolPipeline(),
        tree=SessionTreeMgr(kv=_mem_kv()),
        context_providers=[mem_provider],
        on_finish=finish_cb,
    )
    result = await loop.run("hi")
    assert result.final_answer == "第1轮完成"
    # 每轮都注入了记忆块
    assert injected == ["# MEMORY (关于用户): 用户喜欢简洁回答"]
    # 结束回调拿到 (sid, 消息数)
    assert len(finished) == 1
    assert finished[0][0] == result.session_id
    assert finished[0][1] >= 2  # system + user + assistant


@pytest.mark.asyncio
async def test_run_strict_chat_kv_persist(tmp_path: pytest.MonkeyPatch):
    """会话树 KV 落盘：同路径重开可见（跨重启续做基础）。"""
    from server.agent_loop_bridge import run_strict_chat

    class OneShotLlm:
        async def complete(self, messages, **kw):
            return {"choices": [{"message": {"role": "assistant", "content": "完成"}}]}

        async def close(self):
            pass

    kv_file = str(tmp_path / "session.db")
    r1 = await run_strict_chat(
        "你好",
        session_id="persist-sid",
        system_prompt="sys",
        max_rounds=2,
        llm=OneShotLlm(),
        kv_path=kv_file,
    )
    assert r1["session_id"] == "persist-sid"
    # 同路径新实例（模拟重启）→ 树仍在
    from veya.obase.adapters import SqliteKvStore
    from veya.omodul.session_tree import SessionTreeMgr

    tree2 = SessionTreeMgr(kv=SqliteKvStore(kv_file))
    roles = [n["role"] for n in tree2.path("persist-sid")]
    assert "system" in roles and "user" in roles


@pytest.mark.asyncio
async def test_run_strict_chat_rejects_session_owned_by_other_user(tmp_path):
    """回归 (2026-08-16): run_strict_chat 此前完全不管 sid 归属, 只要拿到

    别人的 session_id 就能续接读写——现在会从 auth.current_user() 取当前
    登录用户, 归属不符时干净拒绝而不是裸抛异常。用同一个 kv_path 让手工建
    的 session 和 run_strict_chat 内部实例化的 SessionTreeMgr 共享同一份
    落盘存储 (否则各查各的、看不到彼此写入)。
    """
    from server import auth as auth_mod
    from server.agent_loop_bridge import run_strict_chat
    from veya.obase.adapters import SqliteKvStore
    from veya.omodul.session_tree import SessionTreeMgr

    class OneShotLlm:
        async def complete(self, messages, **kw):
            return {"choices": [{"message": {"role": "assistant", "content": "不该跑到这里"}}]}

        async def close(self):
            pass

    kv_file = str(tmp_path / "session.db")
    tree = SessionTreeMgr(kv=SqliteKvStore(kv_file))
    sid = tree.create_session(system="sys", owner="alice")

    token = auth_mod._user_ctx.set({"user_id": "bob", "username": "bob"})
    try:
        r = await run_strict_chat(
            "hi", session_id=sid, llm=OneShotLlm(), max_rounds=2, kv_path=kv_file
        )
    finally:
        auth_mod._user_ctx.reset(token)

    assert r["status"] == "failed"
    assert "不属于" in r["final_answer"]


@pytest.mark.asyncio
async def test_run_strict_chat_propagates_error_on_llm_failure():
    """回归 (2026-08-16): run_strict_chat 此前丢弃 AgentLoop 捕获到的 result.error,

    coordinator_master 的"绝不静默"兜底只对旧路径生效, 新主链的具体失败原因
    (如网络/鉴权错误) 会在这一层被静默吞掉, 上层只能显示通用文案。
    """
    from server.agent_loop_bridge import run_strict_chat

    class BoomLlm:
        async def complete(self, messages, **kw):
            raise ConnectionError("gateway timeout")

        async def close(self):
            pass

    r = await run_strict_chat("hi", llm=BoomLlm(), max_rounds=2)
    assert r["status"] == "failed"
    assert "gateway timeout" in r["error"]


@pytest.mark.asyncio
async def test_agent_loop_end_to_end():
    """剧本: 调工具 → 拿到结果 → 直接回答。"""
    llm = FakeLlm(
        [
            _tool_call_msg("add", {"a": 1, "b": 2}),
            {"role": "assistant", "content": "结果是 3"},
        ]
    )
    pipeline = ToolPipeline()
    pipeline.register(
        "add",
        lambda a, b: a + b,
        schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        },
    )
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
    llm = FakeLlm(
        [
            _tool_call_msg("bad", {}),
            _tool_call_msg("bad", {}),
            _tool_call_msg("bad", {}),
        ]
    )
    pipeline = ToolPipeline()
    pipeline.register(
        "bad", lambda: (_ for _ in ()).throw(RuntimeError("fail")), schema={"type": "object"}
    )
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    loop = AgentLoop(
        llm=llm,
        pipeline=pipeline,
        tree=_new_tree(),
        max_consecutive_errors=3,
        backoff_sleep=0.5,
        sleep_fn=fake_sleep,
    )
    result = await loop.run("触发熔断")
    assert result.stop_kind == "fatal_error"
    assert "熔断" in result.stop_reason
    assert result.tool_failures == 3
    assert slept == [0.5]  # 退避发生
    # 回归 (2026-08-16): final_answer 此前在这条分支被留空, 上层只能显示
    # 一句不带任何诊断信息的"网关抖动"通用文案, 真实原因 (工具连续失败) 丢失。
    assert result.final_answer.strip()
    assert "熔断" in result.final_answer
    assert "fail" in result.final_answer  # 最近一次工具错误原文带出来了


@pytest.mark.asyncio
async def test_agent_loop_invalid_response():
    """模型返回空/疲劳回复 → invalid_response 停止。"""
    llm = FakeLlm([{"role": "assistant", "content": "none"}])
    loop = AgentLoop(llm=llm, pipeline=ToolPipeline(), tree=_new_tree())
    result = await loop.run("hi")
    assert result.stop_kind == "invalid_response"
    assert result.final_answer.strip()  # 已经安全 (evaluate_stop_condition 兜底), 防回归


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
    # 回归 (2026-08-16): 自然耗尽轮次此前从未走过任何设置 final_answer 的
    # break 分支, 留空交还调用方——这里每轮 assistant 消息都带 tool_calls
    # (没有真正的文本收尾), 应退化为工具执行摘要而不是抓到占位 "thinking"。
    assert result.final_answer.strip()
    assert "3" in result.final_answer  # 3 次工具调用
    assert "thinking" not in result.final_answer


@pytest.mark.asyncio
async def test_agent_loop_owner_mismatch_returns_clean_error_not_raised():
    """回归 (2026-08-16): 拿别人的 session_id + 自己的 owner 续接, AgentLoop

    必须干净拒绝 (走"绝不留空"的 final_answer 兜底), 不是让 PermissionError
    裸抛到调用方 (那样会变成 500, 而不是一句用户能看懂的解释)。
    """
    llm = FakeLlm([{"role": "assistant", "content": "不该跑到这里"}])
    tree = _new_tree()
    sid = tree.create_session(system="sys", owner="alice")

    loop = AgentLoop(llm=llm, pipeline=ToolPipeline(), tree=tree)
    result = await loop.run("hi", session_id=sid, owner="bob")

    assert result.stop_kind == "fatal_error"
    assert result.final_answer.strip()
    assert "不属于" in result.final_answer
    assert llm.calls == 0  # 归属校验在 LLM 调用之前就该拦下


@pytest.mark.asyncio
async def test_agent_loop_same_owner_reconnects_normally():
    llm = FakeLlm([{"role": "assistant", "content": "继续"}])
    tree = _new_tree()
    sid = tree.create_session(system="sys", owner="alice")

    loop = AgentLoop(llm=llm, pipeline=ToolPipeline(), tree=tree)
    result = await loop.run("hi", session_id=sid, owner="alice")

    assert result.stop_kind == "completed"
    assert result.final_answer == "继续"


@pytest.mark.asyncio
async def test_agent_loop_llm_failure():
    class BoomLlm:
        async def complete(self, messages, **kw):
            raise ConnectionError("network down")

    loop = AgentLoop(llm=BoomLlm(), pipeline=ToolPipeline(), tree=_new_tree())
    result = await loop.run("hi")
    assert result.stop_kind == "fatal_error"
    assert "network down" in result.error
    # 回归 (2026-08-16): 此前 final_answer 留空, result.error 又不被
    # run_strict_chat 转发 (见下面 bridge 测试) → 用户只看到空白/"网关抖动"。
    assert result.final_answer.strip()
    assert "network down" in result.final_answer


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
# bridge — omodul.AgentLoop 执行原语 (agent_loop_run 工具用)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_strict_bridge_end_to_end():
    """bridge: 假 LLM + 工具注入, 新心脏跑通 (旧主链不受影响)。"""
    llm = FakeLlm(
        [
            _tool_call_msg("greet", {"who": "world"}),
            {"role": "assistant", "content": "hello world"},
        ]
    )
    result = await run_strict(
        "打招呼",
        tools={
            "greet": (
                lambda who: f"hello {who}",
                {
                    "type": "object",
                    "properties": {"who": {"type": "string"}},
                },
            )
        },
        llm=llm,
        system_prompt="sys",
    )
    assert result.stop_kind == "completed"
    assert result.final_answer == "hello world"
    assert result.tool_calls == 1

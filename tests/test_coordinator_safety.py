"""回归：主协调器的工具守卫、会话隔离、图片上下文与取消语义。"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import Any

import pytest

from server import auth, user_control
from server import coordinator_master as cm
from server.coordinator_master import MasterCoordinator
from server.tool_guard import global_tool_guard
from server.tool_registry import ToolExecutionError, master_tools


class _HistoryStore:
    async def load(self, *_args: Any, **_kwargs: Any) -> list[dict]:
        return []

    async def save(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _MemoryStore:
    async def retrieve(self, *_args: Any, **_kwargs: Any) -> list[dict]:
        return []

    async def add(self, *_args: Any, **_kwargs: Any) -> str:
        return ""


@pytest.fixture(autouse=True)
def _isolated_controls(monkeypatch):
    monkeypatch.setenv("VEYA_MEMORY", "0")
    monkeypatch.setenv("VEYA_GRAFT_CONTEXT", "0")
    policies = list(global_tool_guard._policies)
    trail = list(global_tool_guard._trail)
    global_tool_guard.clear_policies()
    global_tool_guard._trail.clear()
    user_control._pending.clear()
    user_control._pending_questions.clear()
    yield
    global_tool_guard._policies[:] = policies
    global_tool_guard._trail[:] = trail
    user_control._pending.clear()
    user_control._pending_questions.clear()


def _coordinator(**kwargs: Any) -> MasterCoordinator:
    return MasterCoordinator(
        history_store=_HistoryStore(),
        memory_store=_MemoryStore(),
        **kwargs,
    )


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _skip_context_io(coord: MasterCoordinator) -> None:
    coord._restore_history = _noop
    coord._inject_memory = _noop
    coord._inject_graft_context = _noop
    coord._persist_history = _noop


@pytest.mark.asyncio
async def test_system_tool_obeys_plan_guard() -> None:
    class Automata:
        called = False

        def register_cron_task(self, **_kwargs: Any) -> str:
            self.called = True
            return "scheduled"

    automata = Automata()
    coord = _coordinator(automata=automata, llm_fn=lambda *_a, **_k: None)
    tokens = user_control.activate(mode="plan", require_approval=True, session_id="plan")
    try:
        with pytest.raises(ToolExecutionError, match="plan mode"):
            await coord.handle_tool_call(
                "system_create_automation",
                {"cron_expr": "0 9 * * *", "task_prompt": "proof"},
            )
    finally:
        user_control.deactivate(tokens)
    assert automata.called is False


@pytest.mark.asyncio
async def test_sync_system_tool_runs_off_loop_and_obeys_timeout(monkeypatch) -> None:
    loop_thread = threading.get_ident()

    class Automata:
        def register_cron_task(self, **_kwargs: Any) -> int:
            return threading.get_ident()

    coord = _coordinator(automata=Automata(), llm_fn=lambda *_a, **_k: None)
    result = await coord.handle_tool_call(
        "system_create_automation",
        {"cron_expr": "0 9 * * *", "task_prompt": "proof"},
    )
    assert result != loop_thread

    class SlowAutomata:
        def register_cron_task(self, **_kwargs: Any) -> str:
            time.sleep(0.05)
            return "late"

    monkeypatch.setenv("VEYA_TOOL_TIMEOUT_S", "0.005")
    timed = _coordinator(automata=SlowAutomata(), llm_fn=lambda *_a, **_k: None)
    with pytest.raises(ToolExecutionError, match=r"timed out after 0\.005s"):
        await timed.handle_tool_call(
            "system_create_automation",
            {"cron_expr": "0 9 * * *", "task_prompt": "proof"},
        )


@pytest.mark.asyncio
async def test_chat_stream_sees_full_tool_surface_not_resident_subset() -> None:
    """冻结架构回归: MasterAgent ReAct 主链看到全量工具面 (write_file 等未被裁剪),

    get_resident_schemas 的分组裁剪只服务 agent_loop_run 的隔离子任务执行,
    不应该影响主链本身看到的工具集。
    """
    coord = _coordinator(llm_fn=lambda *_a, **_k: None)
    names = {spec["function"]["name"] for spec in coord.get_all_tool_schemas()}
    assert {"write_file", "grep", "agent_loop_run", "ask_user"} <= names


@pytest.mark.asyncio
async def test_agent_loop_run_tool_scopes_isolated_subtask(monkeypatch) -> None:
    """agent_loop_run 工具: 隔离子任务用临时 session + resident/分组过滤后的工具面

    调 run_strict_chat, 不复用主链 session_id, 完成后把 final_answer 带回。
    """
    import json

    import server.agent_loop_bridge as bridge

    seen: dict[str, Any] = {}

    async def fake_run(task: str, **kwargs: Any) -> dict:
        seen["task"] = task
        seen.update(kwargs)
        return {"status": "success", "final_answer": "sub-task done", "tool_calls": [], "error": ""}

    monkeypatch.setattr(bridge, "run_strict_chat", fake_run)

    out = await master_tools.execute(
        "agent_loop_run", {"task": "grep for TODO", "tool_group": "code_exec"}
    )
    delegate = json.loads(out)
    assert delegate["summary"] == "sub-task done"
    assert delegate["status"] == "partial"  # missing stop_reason is conservative
    assert seen["task"] == "grep for TODO"
    assert seen["session_id"].startswith("agent-loop-tool-")  # 独立临时会话, 不是主链 sid
    names = {spec["function"]["name"] for spec in seen["tool_schemas"]}
    assert {"ask_user", "grep", "write_file"} <= names  # resident + code_exec 组
    assert "vision_glance" not in names  # 未请求的组不该出现


@pytest.mark.asyncio
async def test_hot_history_is_evicted_when_sid_changes_owner(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_AGENT_LOOP", "off")
    observed: list[list[dict]] = []

    async def fake_llm(messages: list[dict], **_kwargs: Any) -> dict:
        observed.append([dict(message) for message in messages])
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    coord = _coordinator(llm_fn=fake_llm, max_rounds=1)
    _skip_context_io(coord)
    alice = auth._user_ctx.set({"user_id": "alice", "username": "alice"})
    try:
        await coord.chat_stream("alice-private", session_id="shared")
    finally:
        auth._user_ctx.reset(alice)
    bob = auth._user_ctx.set({"user_id": "bob", "username": "bob"})
    try:
        await coord.chat_stream("bob-request", session_id="shared")
    finally:
        auth._user_ctx.reset(bob)

    bob_context = "\n".join(str(message.get("content")) for message in observed[1])
    assert "alice-private" not in bob_context
    assert "bob-request" in bob_context


@pytest.mark.asyncio
async def test_same_session_requests_are_serialized(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_AGENT_LOOP", "off")
    active = 0
    max_active = 0

    async def fake_llm(_messages: list[dict], **_kwargs: Any) -> dict:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    coord = _coordinator(llm_fn=fake_llm, max_rounds=1)
    _skip_context_io(coord)
    await asyncio.gather(
        coord.chat_stream("A", session_id="same"),
        coord.chat_stream("B", session_id="same"),
    )
    assert max_active == 1


@pytest.mark.asyncio
async def test_legacy_malformed_json_never_executes_no_arg_tool(monkeypatch) -> None:
    from server.tool_registry import master_tools

    monkeypatch.setenv("VEYA_AGENT_LOOP", "off")
    called = False
    rounds = 0

    def physical() -> str:
        nonlocal called
        called = True
        return "executed"

    async def fake_llm(_messages: list[dict], **_kwargs: Any) -> dict:
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "bad-json",
                                    "type": "function",
                                    "function": {
                                        "name": "malformed_no_arg_test_tool",
                                        "arguments": "{bad-json",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": "handled"}}]}

    master_tools.register(
        "malformed_no_arg_test_tool",
        "test malformed JSON handling",
        {"type": "object", "properties": {}},
        physical,
    )
    try:
        coord = _coordinator(llm_fn=fake_llm, max_rounds=2)
        _skip_context_io(coord)
        result = await coord.chat_stream("run malformed", session_id="malformed")
    finally:
        master_tools.unregister("malformed_no_arg_test_tool")

    assert called is False
    assert result["tool_calls"] == [
        {
            "tool": "malformed_no_arg_test_tool",
            "status": "failed",
            "error": "malformed JSON arguments: Expecting property name enclosed in double quotes",
        }
    ]


@pytest.mark.asyncio
async def test_images_are_request_local_and_do_not_mutate_history(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_AGENT_LOOP", "off")
    entered = 0
    gate = asyncio.Event()
    seen: dict[str, list[str]] = {}

    async def restore(_sid: str) -> None:
        nonlocal entered
        entered += 1
        if entered == 2:
            gate.set()
        await gate.wait()

    async def fake_llm(messages: list[dict], **_kwargs: Any) -> dict:
        user = next(message for message in reversed(messages) if message.get("role") == "user")
        blocks = user["content"]
        text = blocks[0]["text"]
        seen[text] = [
            block["image_url"]["url"] for block in blocks if block.get("type") == "image_url"
        ]
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    coord = _coordinator(llm_fn=fake_llm, max_rounds=1)
    coord._restore_history = restore
    coord._inject_memory = _noop
    coord._inject_graft_context = _noop
    coord._persist_history = _noop
    await asyncio.gather(
        coord.chat_stream("A", session_id="image-a", images=["image-A"]),
        coord.chat_stream("B", session_id="image-b", images=["image-B"]),
    )

    assert seen == {"A": ["image-A"], "B": ["image-B"]}
    for sid in ("image-a", "image-b"):
        user = next(
            message for message in coord._agent._histories[sid] if message.get("role") == "user"
        )
        assert isinstance(user["content"], str)


@pytest.mark.asyncio
async def test_cancel_error_does_not_mask_chat_cancellation(monkeypatch) -> None:
    from server.hicode_queue import hicode_task_queue

    async def failing_stop(_task_id: str) -> bool:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(hicode_task_queue, "stop", failing_stop)
    chat_task = asyncio.create_task(asyncio.sleep(30))
    cm._active_streams["cancel-proof"] = chat_task
    cm._session_task["cancel-proof"] = "hicode-proof"
    try:
        result = await cm.cancel_session("cancel-proof")
        assert "chat_stream" in result["cancelled"]
        assert chat_task.cancelling() > 0
    finally:
        cm._active_streams.pop("cancel-proof", None)
        cm._session_task.pop("cancel-proof", None)
        chat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await chat_task


@pytest.mark.asyncio
async def test_cancelled_approval_is_removed() -> None:
    tokens = user_control.activate(mode="agent", require_approval=True, session_id="approval")
    try:
        task = asyncio.create_task(
            user_control.user_control_policy("write_file", {"filepath": "x"}, "test")
        )
        await asyncio.sleep(0)
        assert user_control._pending
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert not user_control._pending
    finally:
        user_control.deactivate(tokens)

"""W11-C: side-effect approval (S11) and friendly failures (S12).

S11 — with ``VEYA_APPROVAL_GATES=1`` a bot turn whose model selects a side-effect
tool (``write_file``) must create a pending approval through the existing
``server.approvals`` path, write nothing until the human approves, and then run
the action.  The canonical (GoalRun -> ActionGateway) path never consulted the
user-control approval gate, which is why the file used to be written with no
approval anywhere and the verifier's raw "Missing required evidence" string was
echoed back as the bot's reply.

S12 — whatever goes wrong inside a bot turn, the user-visible reply is a friendly
Chinese sentence: never "Missing required evidence", never
"ac-canonical_action_observed", never a traceback, never the GoalRun report.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.app import app as root_app

#: Scenario prompt used by the S11 live harness (kept identical on purpose).
S11_PROMPT = "在 {path} 写一个文件，内容是 hello"

#: The raw internal strings a user must never see.
RAW_LEAKS = (
    "Missing required evidence",
    "Traceback (most recent call last)",
    "ac-canonical_action_observed",
)

PROD_FLAGS: dict[str, str] = {
    "VEYA_APPROVAL_GATES": "1",
    "VEYA_TURN_QUEUE": "1",
    "VEYA_AUTO_REVIEW": "1",
    "VEYA_CONTEXT_STEWARD": "1",
    "VEYA_MEMORY_REVIEW": "1",
}


@pytest.fixture
def test_user():
    username = f"u_w11c_{uuid.uuid4().hex[:8]}"
    user = auth_mod.create_user(username, "pass_test_123")
    token = auth_mod.issue_token(user["user_id"])
    return {
        "user_id": user["user_id"],
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest.fixture
def client(test_user):
    c = TestClient(root_app)
    c.headers.update(test_user["headers"])
    return c


@pytest.fixture
def gate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Approval gates ON, on an isolated approval store."""

    import server.approvals as approvals_mod
    from veya.obase.approvals import ApprovalStore

    for flag, value in PROD_FLAGS.items():
        monkeypatch.setenv(flag, value)
    monkeypatch.setenv("VEYA_APPROVAL_TIMEOUT_S", "5")
    previous = approvals_mod._service
    approvals_mod.configure_service(store=ApprovalStore(tmp_path / "approvals.db"))
    yield
    approvals_mod._service = previous


# ── fakes ───────────────────────────────────────────────────────────────────


def _write_file_tool_call(path: str) -> dict[str, Any]:
    """An OpenAI-format response whose only tool call is ``write_file``."""

    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_write_1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"filepath": path, "content": "hello"}),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {},
    }


class _FakeLLM:
    """A stand-in model that always asks for the same ``write_file`` call."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.calls: list[str] = []

    async def __call__(self, messages: list, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("llm")
        if len(self.calls) == 1:
            return _write_file_tool_call(self.path)
        # After the write, the model reports and stops asking for tools.
        return {"choices": [{"message": {"role": "assistant", "content": "已写入。"}}]}


def _run_turn(client: TestClient, bot_id: str, text: str) -> dict[str, Any]:
    response = client.post(f"/api/v1/bots/{bot_id}/messages", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def _pending_approvals(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/api/v1/approvals")
    assert response.status_code == 200, response.text
    return list((response.json() or {}).get("approvals") or [])


def _resolve(client: TestClient, approval_id: str, decision: str = "approve") -> Any:
    response = client.post(f"/api/v1/approvals/{approval_id}/resolve", json={"decision": decision})
    return response


# ── S11 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_side_effect_action_creates_pending_approval_and_writes_nothing(
    gate_env, test_user, tmp_path, monkeypatch
):
    """S11: a write in a bot turn opens a pending approval on the existing path,
    the chat call answers at once with a friendly "waiting" reply, nothing is
    written until the user approves, and the approved action then runs."""

    import server.coordinator_master as cm
    from server import auth as auth_mod
    from server.bot_team import turns as turns_mod
    from server.bot_team.conversations import ConversationStore
    from server.goal_run.bot_identity import BotSpec

    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots"))
    monkeypatch.setenv("VEYA_WRITE_ROOT", str(tmp_path))
    (tmp_path / "bots").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "approval_target.txt"

    bot = BotSpec(
        bot_id=f"bot_s11_{uuid.uuid4().hex[:6]}",
        owner_id=test_user["user_id"],
        name="S11",
        runner_id="local",
    )
    fake_llm = _FakeLLM(str(target))
    coordinator = cm.MasterCoordinator(
        llm_fn=fake_llm, model="fake-model", provider="fake-provider"
    )
    auth_mod.set_user(test_user)
    conversations = (
        ConversationStore(root=tmp_path / "conv")
        if "root" in ConversationStore.__init__.__code__.co_varnames
        else None
    )

    from server.routes import product as product_routes

    async def fake_open(**kwargs: Any) -> tuple[str, str]:
        return f"s_{uuid.uuid4().hex[:8]}", f"t_{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(product_routes, "_open_task_projection", fake_open)
    original = cm.master_coordinator
    cm.master_coordinator = coordinator
    try:
        result = await asyncio.wait_for(
            turns_mod.run_bot_turn(
                bot=bot,
                text=S11_PROMPT.format(path=str(target)),
                user=test_user,
                workspace_id=str(tmp_path),
                conversations=conversations,
            ),
            timeout=60.0,
        )
        assert result.status == "awaiting_approval", result
        assert "批准" in result.reply
        for marker in ("Missing required evidence", "ac-canonical_action_observed", "Traceback"):
            assert marker not in result.reply
        pending = _approval_service_records()
        assert pending, "no pending approval was created for the side-effect action"
        assert not target.exists(), "file was written before the approval was granted"

        for row in pending:
            await _resolve_approval(str(row["request_id"]), approved=True)
        background = list(turns_mod._BACKGROUND_TURNS)
        assert background, "the paused turn must keep running in the background"
        try:
            await asyncio.wait_for(
                asyncio.gather(*background, return_exceptions=True), timeout=60.0
            )
        except TimeoutError:
            print(
                "AFTER",
                target.exists(),
                fake_llm.calls,
                [(r["request_id"], r["tool"]) for r in _approval_service_records()],
            )
            raise
    finally:
        cm.master_coordinator = original

    assert target.exists(), "approved side-effect action never ran"
    assert target.read_text(encoding="utf-8") == "hello"


# ── S12 ─────────────────────────────────────────────────────────────────────


def test_reply_never_leaks_raw_internal_strings(gate_env):
    """A blocked turn's user-visible reply is friendly, never a raw reason."""

    from server.goal_run.canonical_worker import _user_facing_block_reply

    state = SimpleNamespace(budget={})
    for raw in (
        "Missing required evidence: ['ac-canonical_action_observed']",
        "approval denied or unavailable",
        "ActionNotAuthorized",
        "semantic_step did not return an executable action",
        "RuntimeError: boom",
    ):
        reply = _user_facing_block_reply(state, raw)
        assert reply.strip(), raw
        for leak in RAW_LEAKS:
            assert leak not in reply, (raw, reply)


def test_reply_says_waiting_for_approval(gate_env):
    """A turn paused on approval answers in friendly Chinese, not with a reason."""

    from server.goal_run.canonical_worker import _user_facing_block_reply

    state = SimpleNamespace(budget={"pending_approval": {"approval_id": "a1", "decided": False}})
    reply = _user_facing_block_reply(state, "approval denied or unavailable")
    assert "批准" in reply
    assert "Missing" not in reply
    for leak in RAW_LEAKS:
        assert leak not in reply


def test_extract_reply_never_surfaces_verifier_report():
    """``extract_reply`` shows an answer, not the GoalRun/verifier report card."""

    from server.bot_team.turns import extract_reply

    bot = SimpleNamespace(name="E2E", bot_id="e2e")
    # A blocked run: no reply, only a raw verifier summary.
    blocked = {
        "status": "blocked",
        "reply": None,
        "summary": None,
        "block_reason": "Missing required evidence: ['ac-canonical_action_observed']",
    }
    reply = extract_reply(blocked, bot=bot)
    for leak in RAW_LEAKS:
        assert leak not in reply, (leak, reply)
    assert not reply.startswith("Goal:"), reply

    # A completed run keeps the model's own answer.
    ok = {
        "status": "completed",
        "reply": "写好了。",
        "summary": "Goal: x\nStatus: completed\nTasks: 1/1 completed",
    }
    assert extract_reply(ok, bot=bot) == "写好了。"


def test_reply_after_successful_approval_is_not_a_block_message(gate_env):
    """An approved action reports success, never "waiting for approval"."""

    from server.goal_run.canonical_worker import _user_facing_block_reply

    state = SimpleNamespace(budget={"pending_approval": {"approval_id": "a1", "decided": True}})
    reply = _user_facing_block_reply(state, "approval denied or unavailable")
    assert "批准" not in reply or "通过" in reply


# ── helpers ─────────────────────────────────────────────────────────────────


def _approval_service_records() -> list[dict[str, Any]]:
    from server.approvals import get_approval_service

    service = get_approval_service()
    return [row for row in service.list_pending() if row.get("status") == "pending"]


async def _resolve_approval(approval_id: str, *, approved: bool) -> None:
    from server.approvals import get_approval_service

    service = get_approval_service()
    await service.resolve(
        approval_id=approval_id,
        decision="once" if approved else "deny",
        principal="human",
    )


async def pending_approval_exists() -> bool:
    return bool(_approval_service_records())


async def _wait_for(predicate, *, timeout: float) -> None:
    """Poll ``predicate`` until it is true or ``timeout`` elapses."""

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("condition not met in time")


async def _run_bot_turn(
    bot_id: str,
    target: Path,
    user: dict[str, Any],
    *,
    workspace: Path,
    bot: Any = None,
):
    """Run one bot turn synchronously through the product path."""

    from server.bot_team.turns import BotTurnStart, finish_bot_turn
    from server.routes import product as product_routes

    start = BotTurnStart(
        bot_id=bot_id,
        task_id=f"t_{uuid.uuid4().hex[:8]}",
        session_id=f"s_{uuid.uuid4().hex[:8]}",
        trace_id=f"tr_{uuid.uuid4().hex[:8]}",
    )

    async def fake_open(**kwargs: Any) -> tuple[str, str]:
        return start.session_id, start.task_id

    product_routes._open_task_projection = fake_open
    return await finish_bot_turn(
        bot=bot,
        start=start,
        text=S11_PROMPT.format(path=str(target)),
        user=user,
        workspace_id=str(workspace),
    )

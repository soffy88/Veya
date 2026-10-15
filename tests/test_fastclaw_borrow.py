"""Tests for FastClaw Clean-Room Borrowed Features.

Feature 1: IM Sandbox Enforcement (flag: VEYA_IM_SANDBOX_ENFORCE, default OFF)
Feature 2: Bot Fork + Public Chat Link (flag: VEYA_BOT_PUBLIC_SHARE, default OFF)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import feature_flags
from server.channels.sandbox_enforce import (
    bind_im_turn,
    check_im_sandbox_tool,
    is_im_non_owner_turn,
    reset_im_turn,
)
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.routes.bot_team import router
from server.tool_guard import ToolDenied, global_tool_guard
from veya.remote.policy_resolver import (
    LayerOutcome,
    OperationContext,
    PolicyRequest,
    SecurityPolicy,
)


@pytest.fixture(autouse=True)
def clean_flags(monkeypatch):
    """Ensure clean feature flag states across tests."""
    monkeypatch.delenv("VEYA_IM_SANDBOX_ENFORCE", raising=False)
    monkeypatch.delenv("VEYA_BOT_PUBLIC_SHARE", raising=False)
    yield


# ==============================================================================
# FEATURE 1: IM Sandbox Enforcement Tests
# ==============================================================================


def test_feature1_flag_default_off():
    """Verify VEYA_IM_SANDBOX_ENFORCE defaults to False."""
    assert not feature_flags.enabled("VEYA_IM_SANDBOX_ENFORCE")


def test_feature1_flag_off_no_denial():
    """When flag is OFF, non-owner IM turn is not denied."""
    assert not feature_flags.enabled("VEYA_IM_SANDBOX_ENFORCE")

    tok = bind_im_turn(is_im=True, is_owner=False, platform="telegram", bot_id="bot1")
    try:
        assert not is_im_non_owner_turn()
        # Even sensitive tools return None (allowed) when flag is OFF
        assert check_im_sandbox_tool("system_shell_exec", {"command": "ls"}) is None
        assert check_im_sandbox_tool("edit_hashline", {"path": "foo.py"}) is None
        assert check_im_sandbox_tool("request_secret", {"name": "KEY"}) is None
    finally:
        reset_im_turn(tok)


def test_feature1_flag_on_owner_turn_allowed(monkeypatch):
    """When flag is ON, owner IM turn is permitted normal execution."""
    monkeypatch.setenv("VEYA_IM_SANDBOX_ENFORCE", "1")
    assert feature_flags.enabled("VEYA_IM_SANDBOX_ENFORCE")

    tok = bind_im_turn(is_im=True, is_owner=True, platform="feishu", bot_id="bot1")
    try:
        assert not is_im_non_owner_turn()
        assert check_im_sandbox_tool("system_shell_exec", {"command": "ls"}) is None
        assert check_im_sandbox_tool("write_file", {"filepath": "main.py"}) is None
        assert check_im_sandbox_tool("run_in_sandbox", {"command": "ls", "host": "h1"}) is None
    finally:
        reset_im_turn(tok)


def test_feature1_flag_on_non_owner_sandbox_enforcement(monkeypatch, tmp_path):
    """When flag is ON, non-owner IM turn enforces sandbox routing or denial."""
    monkeypatch.setenv("VEYA_IM_SANDBOX_ENFORCE", "1")
    assert feature_flags.enabled("VEYA_IM_SANDBOX_ENFORCE")

    tok = bind_im_turn(is_im=True, is_owner=False, platform="telegram", bot_id="bot1")
    try:
        assert is_im_non_owner_turn()

        # 1. Unsandboxed exec tools denied
        denial = check_im_sandbox_tool("system_shell_exec", {"command": "rm -rf /"})
        assert denial is not None
        assert "unsandboxed" in denial

        assert check_im_sandbox_tool("system_secure_exec", {"command": "ls"}) is not None
        assert check_im_sandbox_tool("ssh_run", {"command": "ls"}) is not None
        assert check_im_sandbox_tool("agent_loop_run", {"goal": "test"}) is not None

        # 2. Local run_in_sandbox is permitted (it is the existing canonical sandbox path)
        assert check_im_sandbox_tool("run_in_sandbox", {"command": "python -V"}) is None
        assert check_im_sandbox_tool("run_backtest_coprocessor", {}) is None

        # 3. Remote host run_in_sandbox is denied
        assert (
            check_im_sandbox_tool("run_in_sandbox", {"command": "ls", "host": "prod1"}) is not None
        )

        # 4. MCP host tools denied
        assert check_im_sandbox_tool("shell.exec", {"execution_target": "HOST"}) is not None
        assert check_im_sandbox_tool("shell.exec", {"profile": "l0_host"}) is not None

        # 5. File mutation and secret tools denied
        assert check_im_sandbox_tool("edit_hashline", {"path": "server.py"}) is not None
        assert check_im_sandbox_tool("ast_grep_rewrite", {}) is not None
        assert check_im_sandbox_tool("request_secret", {"name": "API_KEY"}) is not None
        assert check_im_sandbox_tool("list_secrets", {}) is not None

        # 6. write_file to remote host or protected config denied
        assert (
            check_im_sandbox_tool("write_file", {"filepath": "test.txt", "host": "h1"}) is not None
        )
        assert (
            check_im_sandbox_tool(
                "write_file", {"filepath": str(Path.home() / ".veya" / "config.json")}
            )
            is not None
        )

        # 7. write_file inside bound sandbox root is permitted
        from server.tool_registry import bind_write_root, reset_write_root

        sbox_tok = bind_write_root(tmp_path)
        try:
            assert (
                check_im_sandbox_tool("write_file", {"filepath": str(tmp_path / "out.txt")}) is None
            )
        finally:
            reset_write_root(sbox_tok)
    finally:
        reset_im_turn(tok)


def test_feature1_policy_resolver_security_layer(monkeypatch):
    """Verify PolicyResolver's SecurityPolicy denies non-owner unsandboxed tools."""
    policy = SecurityPolicy()
    req = PolicyRequest(goal_id="g1", tool="system_shell_exec", args={"command": "ls"})
    ctx = OperationContext()

    # Default (flag OFF): abstain
    verdict = policy.evaluate(req, ctx)
    assert verdict.outcome == LayerOutcome.ABSTAIN

    # Flag ON, non-owner turn: DENY
    monkeypatch.setenv("VEYA_IM_SANDBOX_ENFORCE", "1")
    tok = bind_im_turn(is_im=True, is_owner=False, platform="discord")
    try:
        verdict = policy.evaluate(req, ctx)
        assert verdict.outcome == LayerOutcome.DENY
        assert "security.im_sandbox_denied" in verdict.rule
    finally:
        reset_im_turn(tok)


@pytest.mark.asyncio
async def test_feature1_tool_guard_denial(monkeypatch):
    """Verify ToolGuard raises ToolDenied for non-owner IM turn."""
    monkeypatch.setenv("VEYA_IM_SANDBOX_ENFORCE", "1")
    tok = bind_im_turn(is_im=True, is_owner=False, platform="wechat")
    try:
        guard = global_tool_guard
        with pytest.raises(ToolDenied) as exc_info:
            await guard.acheck("system_shell_exec", {"command": "ls"}, source="master_tool")
        assert "unsandboxed" in str(exc_info.value)

        from server.tool_registry import MasterToolRegistry, ToolExecutionError

        reg = MasterToolRegistry()
        reg.register(
            "system_shell_exec",
            "exec command",
            {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            lambda command: f"ran {command}",
        )
        with pytest.raises(ToolExecutionError) as exc_info2:
            await reg.execute("system_shell_exec", {"command": "ls"})
        assert "unsandboxed" in str(exc_info2.value)
    finally:
        reset_im_turn(tok)


# ==============================================================================
# FEATURE 2: Bot Fork + Public Chat Link Tests
# ==============================================================================


@pytest.fixture
def test_app(tmp_path, monkeypatch):
    """FastAPI test app with temporary bot registry and auth."""
    from fastapi import FastAPI

    from server import auth as auth_mod

    bots_root = tmp_path / "bots"
    bots_root.mkdir()
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(bots_root))

    app = FastAPI()
    app.include_router(router)

    # Set up mock auth user
    current_test_user = {"user_id": "owner_alice", "username": "alice"}

    def _get_test_user():
        return current_test_user

    app.dependency_overrides[auth_mod.require_user] = _get_test_user

    # Reset bot registry for isolation
    registry = BotRegistry(storage_path=tmp_path / "bots.json")
    monkeypatch.setattr("server.routes.bot_team.bot_registry", lambda: registry)

    return app, registry, lambda u: current_test_user.update(u)


def test_feature2_flag_default_off():
    """Verify VEYA_BOT_PUBLIC_SHARE defaults to False."""
    assert not feature_flags.enabled("VEYA_BOT_PUBLIC_SHARE")


def test_feature2_routes_404_when_flag_off(test_app):
    """When VEYA_BOT_PUBLIC_SHARE is OFF, fork, toggle, and public chat return 404."""
    app, registry, _set_user = test_app
    client = TestClient(app)

    # Create a bot first
    spec = BotSpec(bot_id="helper_bot", owner_id="owner_alice", name="Helper")
    registry.register(spec)

    # 1. Fork -> 404
    resp = client.post("/api/v1/bots/helper_bot/fork", json={})
    assert resp.status_code == 404

    # 2. Public share toggle -> 404
    resp = client.post("/api/v1/bots/helper_bot/public_share", json={"enabled": True})
    assert resp.status_code == 404

    # 3. Public get -> 404
    resp = client.get("/api/v1/bots/helper_bot/public")
    assert resp.status_code == 404

    # 4. Public messages get & post -> 404
    resp = client.get("/api/v1/bots/helper_bot/public/messages")
    assert resp.status_code == 404

    resp = client.post("/api/v1/bots/helper_bot/public/messages", json={"text": "hello"})
    assert resp.status_code == 404


def test_feature2_fork_bot_owner_only_and_isolation(test_app, monkeypatch):
    """Owner can fork bot; non-owner gets 404; memory/secrets/sessions are NOT copied."""
    monkeypatch.setenv("VEYA_BOT_PUBLIC_SHARE", "1")
    app, registry, set_user = test_app
    client = TestClient(app)

    # Register original bot under Alice
    spec = BotSpec(
        bot_id="alice_bot",
        owner_id="owner_alice",
        name="Alice Bot",
        description="A specialized research bot",
        instructions="Always be analytical",
        provider="anthropic",
        model="claude-3-opus",
        available_skill_ids=["skill_search", "skill_eval"],
        public_share=True,
    )
    registry.register(spec)

    # Write some conversation and memory for original bot
    from server.bot_team.conversations import conversation_store
    from server.bot_team.memory import BotMemoryStore

    conv_store = conversation_store()
    conv_store.append("alice_bot", role="user", text="original secret question")
    conv_store.append("alice_bot", role="bot", text="original answer")

    mem_store = BotMemoryStore("alice_bot")
    mem_store.write("MEMORY.md", "Alice's secret memory notes")

    # Non-owner Bob tries to fork Alice's bot -> 404
    set_user({"user_id": "chatter_bob", "username": "bob"})
    resp = client.post("/api/v1/bots/alice_bot/fork", json={})
    assert resp.status_code == 404

    # Owner Alice forks her bot
    set_user({"user_id": "owner_alice", "username": "alice"})
    resp = client.post(
        "/api/v1/bots/alice_bot/fork",
        json={"new_bot_id": "alice_forked", "name": "Alice Bot Forked"},
    )
    assert resp.status_code == 200
    fork_data = resp.json()["bot"]

    # Verify copied configuration
    assert fork_data["bot_id"] == "alice_forked"
    assert fork_data["owner_id"] == "owner_alice"
    assert fork_data["name"] == "Alice Bot Forked"
    assert fork_data["description"] == "A specialized research bot"
    assert fork_data["instructions"] == "Always be analytical"
    assert fork_data["provider"] == "anthropic"
    assert fork_data["model"] == "claude-3-opus"
    assert fork_data["available_skill_ids"] == ["skill_search", "skill_eval"]
    assert fork_data["public_share"] is False  # New fork is private by default

    # Verify ISOLATION: conversations NOT copied
    assert conv_store.list("alice_forked") == []

    # Verify ISOLATION: memory NOT copied
    fork_mem_store = BotMemoryStore("alice_forked")
    assert fork_mem_store.read("MEMORY.md") == ""


def test_feature2_public_toggle_owner_only(test_app, monkeypatch):
    """Owner can toggle public access; non-owner gets 404."""
    monkeypatch.setenv("VEYA_BOT_PUBLIC_SHARE", "1")
    app, registry, set_user = test_app
    client = TestClient(app)

    spec = BotSpec(
        bot_id="toggle_bot", owner_id="owner_alice", name="Toggle Bot", public_share=False
    )
    registry.register(spec)

    # Bob tries to toggle -> 404
    set_user({"user_id": "chatter_bob", "username": "bob"})
    resp = client.post("/api/v1/bots/toggle_bot/public_share", json={"enabled": True})
    assert resp.status_code == 404

    # Alice toggles ON
    set_user({"user_id": "owner_alice", "username": "alice"})
    resp = client.post("/api/v1/bots/toggle_bot/public_share", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["public_share"] is True
    assert registry.get("toggle_bot").public_share is True

    # Alice toggles OFF
    resp = client.post("/api/v1/bots/toggle_bot/public_share", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["public_share"] is False
    assert registry.get("toggle_bot").public_share is False


def test_feature2_public_chat_isolation_and_sandboxing(test_app, monkeypatch):
    """Public chatters have isolated sessions/memory, and non-owner tools are sandboxed."""
    monkeypatch.setenv("VEYA_BOT_PUBLIC_SHARE", "1")
    app, registry, set_user = test_app
    client = TestClient(app)

    spec = BotSpec(
        bot_id="public_bot",
        owner_id="owner_alice",
        name="Public Bot",
        instructions="Help users safely",
        public_share=True,
    )
    registry.register(spec)

    # Mock run_bot_turn so we can inspect isolation and arguments passed
    captured_turns = []

    async def _mock_run_bot_turn(*, bot, text, user, conversations, **kwargs):
        captured_turns.append(
            {"bot": bot, "text": text, "user": user, "conversations": conversations}
        )
        # Record user & bot message in chatter's conversation
        conversations.append(bot.bot_id, role="user", text=text)
        conversations.append(bot.bot_id, role="bot", text=f"Echo: {text}")
        from server.bot_team.turns import BotTurnResult

        return BotTurnResult(
            bot_id=bot.bot_id,
            task_id="task_mock_1",
            session_id="session_mock_1",
            reply=f"Echo: {text}",
            status="completed",
            trace_id="trace_mock_1",
        )

    monkeypatch.setattr("server.routes.bot_team.run_bot_turn", _mock_run_bot_turn)

    # Bob chats with public bot
    set_user({"user_id": "chatter_bob", "username": "bob"})
    resp_bob = client.post(
        "/api/v1/bots/public_bot/public/messages", json={"text": "Hello from Bob"}
    )
    assert resp_bob.status_code == 200
    assert resp_bob.json()["reply"] == "Echo: Hello from Bob"

    # Charlie chats with public bot
    set_user({"user_id": "chatter_charlie", "username": "charlie"})
    resp_charlie = client.post(
        "/api/v1/bots/public_bot/public/messages", json={"text": "Hello from Charlie"}
    )
    assert resp_charlie.status_code == 200
    assert resp_charlie.json()["reply"] == "Echo: Hello from Charlie"

    # 1. Check isolation between Bob and Charlie
    set_user({"user_id": "chatter_bob", "username": "bob"})
    bob_msgs = client.get("/api/v1/bots/public_bot/public/messages").json()["messages"]
    assert len(bob_msgs) == 2
    assert bob_msgs[0]["text"] == "Hello from Bob"

    set_user({"user_id": "chatter_charlie", "username": "charlie"})
    charlie_msgs = client.get("/api/v1/bots/public_bot/public/messages").json()["messages"]
    assert len(charlie_msgs) == 2
    assert charlie_msgs[0]["text"] == "Hello from Charlie"

    # 2. Check that owner's direct conversation has ZERO chatter messages
    set_user({"user_id": "owner_alice", "username": "alice"})
    owner_msgs = client.get("/api/v1/bots/public_bot/messages").json()["messages"]
    assert len(owner_msgs) == 0

    # 3. Check that non-owner user context was passed with is_owner=False, is_public_chatter=True
    assert captured_turns[0]["user"]["is_owner"] is False
    assert captured_turns[0]["user"]["is_public_chatter"] is True
    # The proxy bot has scoped bot_id, not owner's bot_id
    assert captured_turns[0]["bot"].bot_id.startswith("pub_chatter_bob_")

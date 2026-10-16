"""tests/test_lorca_p3_memory.py — P3 per-bot Markdown memory (Lorca-style).

Covers:
    CRUD            — write / read / delete / list_files
    Traversal       — path traversal names are rejected (ValueError / HTTP 422)
    Isolation       — require_same_bot enforced when caller_bot_id is provided
    Render          — render_for_prompt truncation and empty-store behaviour
    REST            — all four endpoints via FastAPI TestClient
    Injection       — VEYA_BOT_MEMORY_MD semantic-context injection (on/off)
    Tool            — bot_memory_note scoped to the running bot
    Worker wiring   — canonical worker injects + binds the bot scope
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from runtime import bot_scope
from runtime.bot_scope import CrossBotAccessDenied
from server.bot_team.memory import (
    CORE_FILE,
    MAX_FILE_BYTES,
    BotMemoryStore,
    inject_memory_context,
)

if TYPE_CHECKING:
    from server.goal_run.canonical_worker import CanonicalWorkerAdapter

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path):
    """A BotMemoryStore backed by a tmp directory."""
    return BotMemoryStore("bot-alpha", bots_root=tmp_path)


@pytest.fixture()
def store_b(tmp_path):
    """A second store for a different bot (same bots_root)."""
    return BotMemoryStore("bot-beta", bots_root=tmp_path)


@pytest.fixture()
def rooted_store(tmp_path, monkeypatch):
    """Store that is also the module's default root.

    Helpers like :func:`inject_memory_context` build their own store from
    ``VEYA_BOTS_ROOT`` (the documented override), so the fixture points the
    environment at the same tmp directory the store writes to.
    """
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path))
    return BotMemoryStore("bot-alpha", bots_root=tmp_path)


# ── CRUD tests ────────────────────────────────────────────────────────────────


class TestCRUD:
    def test_write_and_read(self, store):
        store.write(CORE_FILE, "hello world")
        assert store.read(CORE_FILE) == "hello world"

    def test_read_absent_returns_empty(self, store):
        assert store.read(CORE_FILE) == ""

    def test_list_files_empty(self, store):
        assert store.list_files() == []

    def test_list_files_after_write(self, store):
        store.write(CORE_FILE, "x")
        store.write("notes.md", "y")
        assert store.list_files() == ["MEMORY.md", "notes.md"]

    def test_overwrite_file(self, store):
        store.write(CORE_FILE, "v1")
        store.write(CORE_FILE, "v2")
        assert store.read(CORE_FILE) == "v2"

    def test_delete_existing(self, store):
        store.write(CORE_FILE, "data")
        deleted = store.delete(CORE_FILE)
        assert deleted is True
        assert store.read(CORE_FILE) == ""

    def test_delete_absent_returns_false(self, store):
        assert store.delete(CORE_FILE) is False

    def test_append_note_creates_core_file(self, store):
        store.append_note("first note")
        content = store.read(CORE_FILE)
        assert "first note" in content
        assert "Note —" in content

    def test_append_note_multiple(self, store):
        store.append_note("note A")
        store.append_note("note B")
        content = store.read(CORE_FILE)
        assert "note A" in content
        assert "note B" in content
        assert "---" in content  # separator between notes

    def test_write_multiple_topic_files(self, store):
        store.write("python.md", "Python notes")
        store.write("golang.md", "Go notes")
        files = store.list_files()
        assert "golang.md" in files
        assert "python.md" in files

    def test_write_is_atomic(self, store, tmp_path):
        """After write the file content must be exactly what was written (no partial)."""
        store.write(CORE_FILE, "atomic content")
        path = tmp_path / "bot-alpha" / "memory" / CORE_FILE
        assert path.read_text() == "atomic content"

    def test_delete_removes_from_list(self, store):
        store.write("topic.md", "data")
        store.delete("topic.md")
        assert "topic.md" not in store.list_files()


# ── Path-traversal rejection ─────────────────────────────────────────────────


class TestTraversalRejection:
    @pytest.mark.parametrize(
        "bad_name",
        [
            "../evil.md",
            "../../etc/passwd",
            "sub/dir.md",
            "no_extension",
            "has space.md",
            ".hidden.md",
            "file.txt",
            "",
        ],
    )
    def test_invalid_names_raise_value_error(self, store, bad_name):
        with pytest.raises(ValueError):
            store.write(bad_name, "data")

    def test_non_md_extension_rejected(self, store):
        with pytest.raises(ValueError, match=r"\.md"):
            store.read("notes.txt")

    def test_traversal_rejected_on_delete(self, store):
        with pytest.raises(ValueError):
            store.delete("../evil.md")

    def test_traversal_rejected_on_read(self, store):
        with pytest.raises(ValueError):
            store.read("../../etc/shadow")


# ── Bot isolation ─────────────────────────────────────────────────────────────


class TestIsolation:
    def test_same_bot_write_allowed(self, store):
        store.write(CORE_FILE, "safe", caller_bot_id="bot-alpha")
        assert store.read(CORE_FILE) == "safe"

    def test_different_bot_write_denied(self, store):
        with pytest.raises(CrossBotAccessDenied):
            store.write(CORE_FILE, "hack", caller_bot_id="bot-intruder")

    def test_same_bot_delete_allowed(self, store):
        store.write(CORE_FILE, "x")
        store.delete(CORE_FILE, caller_bot_id="bot-alpha")
        assert CORE_FILE not in store.list_files()

    def test_different_bot_delete_denied(self, store):
        store.write(CORE_FILE, "x")
        with pytest.raises(CrossBotAccessDenied):
            store.delete(CORE_FILE, caller_bot_id="other-bot")

    def test_same_bot_append_note_allowed(self, store):
        store.append_note("ok", caller_bot_id="bot-alpha")
        assert "ok" in store.read(CORE_FILE)

    def test_different_bot_append_note_denied(self, store):
        with pytest.raises(CrossBotAccessDenied):
            store.append_note("inject", caller_bot_id="attacker")

    def test_two_bots_isolated_storage(self, store, store_b, tmp_path):
        """Each bot has its own memory directory; files do not bleed across."""
        store.write(CORE_FILE, "alpha memory")
        store_b.write(CORE_FILE, "beta memory")
        assert store.read(CORE_FILE) == "alpha memory"
        assert store_b.read(CORE_FILE) == "beta memory"

    def test_no_caller_id_skips_isolation_check(self, store):
        """When caller_bot_id is None, isolation is not enforced (opt-in)."""
        store.write(CORE_FILE, "unchecked", caller_bot_id=None)
        assert store.read(CORE_FILE) == "unchecked"


# ── Size caps ─────────────────────────────────────────────────────────────────


class TestSizeCaps:
    def test_file_too_large_rejected(self, store):
        big = "x" * (MAX_FILE_BYTES + 1)
        with pytest.raises(ValueError, match="size cap"):
            store.write(CORE_FILE, big)

    def test_total_store_cap_enforced(self, store):
        # Use chunks smaller than MAX_FILE_BYTES (512 KiB) individually
        # but enough combined to exceed MAX_TOTAL_BYTES (2 MiB).
        # 3 files × 400 KiB = 1.2 MiB < 2 MiB — writes succeed.
        # 4th file × 400 KiB = 1.6 MiB total < 2 MiB — also fine.
        # 5th file pushes us over; use different sizes to control precisely.
        # Simpler: write four 400 KiB files (1.6 MiB) then a 500 KiB file.
        chunk = "a" * (400 * 1024)
        store.write("part1.md", chunk)
        store.write("part2.md", chunk)
        store.write("part3.md", chunk)
        store.write("part4.md", chunk)  # 1.6 MiB total, still under cap
        # 5th file of 400 KiB would make 2.0 MiB (right at cap) — try 500 KiB
        overflow = "b" * (500 * 1024)  # < 512 KiB per-file cap, but pushes total over
        with pytest.raises(ValueError, match="total cap"):
            store.write("part5.md", overflow)

    def test_overwrite_does_not_double_count(self, store):
        """Overwriting the same file must not accumulate its old bytes."""
        chunk = "b" * (MAX_FILE_BYTES // 2)
        store.write(CORE_FILE, chunk)
        # Overwrite with equally large content: should succeed.
        store.write(CORE_FILE, chunk)
        assert len(store.read(CORE_FILE).encode()) == len(chunk.encode())


# ── render_for_prompt truncation ─────────────────────────────────────────────


class TestRenderForPrompt:
    def test_empty_store_returns_empty_string(self, store):
        assert store.render_for_prompt() == ""

    def test_basic_render(self, store):
        store.write(CORE_FILE, "core content")
        rendered = store.render_for_prompt()
        assert "bot-alpha" in rendered
        assert "MEMORY.md" in rendered
        assert "core content" in rendered

    def test_render_includes_all_files(self, store):
        store.write(CORE_FILE, "main")
        store.write("topic.md", "topic data")
        rendered = store.render_for_prompt()
        assert "topic.md" in rendered
        assert "topic data" in rendered

    def test_render_truncates_to_max_chars(self, store):
        store.write(CORE_FILE, "A" * 3000)
        store.write("big.md", "B" * 3000)
        rendered = store.render_for_prompt(max_chars=500)
        assert len(rendered) <= 500

    def test_render_shows_truncated_stub(self, store):
        store.write(CORE_FILE, "main note")
        store.write("huge.md", "Z" * 5000)
        # With a small cap, huge.md should appear only as a stub.
        rendered = store.render_for_prompt(max_chars=300)
        assert "truncated" in rendered or "huge.md" in rendered

    def test_render_does_not_exceed_max_chars(self, store):
        store.write(CORE_FILE, "x" * 100)
        for i in range(10):
            store.write(f"file{i:02d}.md", "y" * 200)
        for cap in [200, 500, 1000, 4000]:
            r = store.render_for_prompt(max_chars=cap)
            assert len(r) <= cap, f"exceeded cap={cap}: got {len(r)}"


# ── REST endpoints via TestClient ─────────────────────────────────────────────


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """TestClient with VEYA_BOTS_ROOT pointing to an isolated tmp directory."""
    bots_root = str(tmp_path_factory.mktemp("bots_root"))
    os.environ["VEYA_BOTS_ROOT"] = bots_root
    # Import the isolated app after patching the env so the store picks up the override.
    from fastapi import FastAPI
    from server import auth as auth_mod
    from server.auth import AuthGateMiddleware
    from server.routes.bot_memory import router as bot_memory_router

    # Each fixture owns its app. Never add middleware to server.app, which may
    # already have started in another test module.
    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)
    app.include_router(bot_memory_router)

    db = tmp_path_factory.mktemp("sec-auth-mod") / "auth.db"
    auth_mod._DB_PATH = db
    os.environ.pop("VEYA_LOCAL_SINGLE_USER", None)
    auth_mod._DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with auth_mod._lock, auth_mod._connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "  id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,"
            "  password_hash TEXT NOT NULL, created_at INTEGER NOT NULL,"
            "  is_owner INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tokens ("
            "  token TEXT PRIMARY KEY, user_id TEXT NOT NULL,"
            "  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
        )
    user = auth_mod.create_user("sec-test-owner", "a-very-long-password", is_owner=True)
    token = auth_mod.issue_token(user["user_id"])

    from server.goal_run.bot_identity import BotRegistry, BotSpec

    os.environ.setdefault(
        "VEYA_BOT_REGISTRY_PATH", str(tmp_path_factory.mktemp("reg") / "bot_registry.json")
    )
    BotRegistry().register(BotSpec(bot_id=BOT, owner_id=user["user_id"], name="rest"))
    with TestClient(app, raise_server_exceptions=True) as c:
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c
    os.environ.pop("VEYA_BOTS_ROOT", None)


BOT = "test-bot-rest"
FILE = "notes.md"


class TestRestEndpoints:
    def test_list_empty(self, client):
        r = client.get(f"/api/v1/bots/{BOT}/memory")
        assert r.status_code == 200
        data = r.json()
        assert data["bot_id"] == BOT
        assert data["files"] == []

    def test_put_and_get(self, client):
        r = client.put(
            f"/api/v1/bots/{BOT}/memory/{FILE}",
            content=b"hello REST",
            headers={"Content-Type": "text/plain"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

        r2 = client.get(f"/api/v1/bots/{BOT}/memory/{FILE}")
        assert r2.status_code == 200
        assert r2.json()["content"] == "hello REST"

    def test_put_json_body(self, client):
        r = client.put(
            f"/api/v1/bots/{BOT}/memory/json-written.md",
            json={"text": "from JSON"},
        )
        assert r.status_code == 200
        r2 = client.get(f"/api/v1/bots/{BOT}/memory/json-written.md")
        assert r2.json()["content"] == "from JSON"

    def test_list_after_writes(self, client):
        r = client.get(f"/api/v1/bots/{BOT}/memory")
        files = r.json()["files"]
        assert FILE in files
        assert "json-written.md" in files

    def test_delete_existing(self, client):
        r = client.delete(f"/api/v1/bots/{BOT}/memory/{FILE}")
        assert r.status_code == 200
        assert r.json()["deleted"] is True

    def test_delete_absent_returns_404(self, client):
        r = client.delete(f"/api/v1/bots/{BOT}/memory/gone.md")
        assert r.status_code == 404

    def test_read_absent_returns_404(self, client):
        r = client.get(f"/api/v1/bots/{BOT}/memory/never-written.md")
        assert r.status_code == 404

    def test_traversal_rejected_by_rest(self, client):
        """Path-traversal names must be rejected with 422 (not 500)."""
        # FastAPI encodes path params, so test via direct name validation
        r = client.put(
            f"/api/v1/bots/{BOT}/memory/noext",
            content=b"bad",
            headers={"Content-Type": "text/plain"},
        )
        assert r.status_code == 422

    def test_non_md_name_rejected_by_rest(self, client):
        r = client.put(
            f"/api/v1/bots/{BOT}/memory/bad.txt",
            content=b"x",
            headers={"Content-Type": "text/plain"},
        )
        assert r.status_code == 422


# ── Semantic context injection (VEYA_BOT_MEMORY_MD) ──────────────────────────


class _FakeSemanticAgent:
    """Minimal stand-in for the oservi MasterAgent semantic surface."""

    def __init__(self) -> None:
        self._histories: dict[str, list[dict]] = {}

    def get_system_prompt(self) -> str:
        return "SYSTEM PROMPT"


class TestSemanticContextInjection:
    def test_flag_off_is_a_noop(self, rooted_store, monkeypatch):
        monkeypatch.setenv("VEYA_BOT_MEMORY_MD", "0")
        rooted_store.write(CORE_FILE, "note that must stay out of the prompt")
        agent = _FakeSemanticAgent()
        assert inject_memory_context(agent, "s1", "bot-alpha") is False
        assert agent._histories == {}  # history untouched when disabled

    def test_unset_flag_defaults_to_off(self, rooted_store, monkeypatch):
        monkeypatch.delenv("VEYA_BOT_MEMORY_MD", raising=False)
        rooted_store.write(CORE_FILE, "kept private by default")
        agent = _FakeSemanticAgent()
        assert inject_memory_context(agent, "s1", "bot-alpha") is False
        assert agent._histories == {}

    def test_flag_on_injects_memory_after_system_prompt(self, rooted_store, monkeypatch):
        monkeypatch.setenv("VEYA_BOT_MEMORY_MD", "1")
        rooted_store.write(CORE_FILE, "alpha core note")
        agent = _FakeSemanticAgent()
        assert inject_memory_context(agent, "s1", "bot-alpha") is True
        messages = agent._histories["s1"]
        assert [m["role"] for m in messages] == ["system", "system"]
        assert messages[0]["content"] == "SYSTEM PROMPT"
        assert "alpha core note" in messages[1]["content"]

    def test_injection_is_refreshed_not_accumulated(self, rooted_store, monkeypatch):
        monkeypatch.setenv("VEYA_BOT_MEMORY_MD", "1")
        rooted_store.write(CORE_FILE, "v1")
        agent = _FakeSemanticAgent()
        inject_memory_context(agent, "s1", "bot-alpha")
        rooted_store.append_note("v2")
        inject_memory_context(agent, "s1", "bot-alpha")
        messages = agent._histories["s1"]
        assert len(messages) == 2  # system prompt + one memory block
        assert "v1" in messages[1]["content"] and "v2" in messages[1]["content"]

    def test_empty_store_injects_nothing(self, monkeypatch):
        monkeypatch.setenv("VEYA_BOT_MEMORY_MD", "1")
        agent = _FakeSemanticAgent()
        assert inject_memory_context(agent, "s1", "bot-alpha") is False
        # The session still gets its seeded system prompt only.
        assert [m["role"] for m in agent._histories["s1"]] == ["system"]

    def test_only_the_running_bots_memory_is_injected(self, rooted_store, store_b, monkeypatch):
        monkeypatch.setenv("VEYA_BOT_MEMORY_MD", "1")
        rooted_store.write(CORE_FILE, "alpha secret")
        store_b.write(CORE_FILE, "beta secret")
        agent = _FakeSemanticAgent()
        inject_memory_context(agent, "s1", "bot-beta")
        block = agent._histories["s1"][1]["content"]
        assert "beta secret" in block
        assert "alpha secret" not in block

    def test_render_truncation_carries_into_the_block(self, rooted_store, monkeypatch):
        monkeypatch.setenv("VEYA_BOT_MEMORY_MD", "1")
        rooted_store.write(CORE_FILE, "A" * 3000)
        rooted_store.write("big.md", "B" * 3000)
        agent = _FakeSemanticAgent()
        inject_memory_context(agent, "s1", "bot-alpha", max_chars=500)
        block = agent._histories["s1"][1]["content"]
        mark = "<!-- bot-memory-context bot_id=bot-alpha -->\n\n"
        # The mark header wraps the capped render; the render itself never
        # exceeds the requested budget.
        assert len(block) <= len(mark) + 500 + 1
        assert mark in block


# ── bot_memory_note tool ─────────────────────────────────────────────────────


class TestBotMemoryNoteTool:
    def test_requires_a_bound_bot_scope(self):
        from server.bot_team.memory import bot_memory_note

        token = bot_scope.bind_current_bot(None)  # explicitly unbound
        try:
            with pytest.raises(RuntimeError, match="bot scope"):
                bot_memory_note("orphan note")
        finally:
            bot_scope.reset_current_bot(token)

    def test_writes_to_the_running_bots_memory(self, tmp_path, monkeypatch):
        from server.bot_team.memory import bot_memory_note

        monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path))
        token = bot_scope.bind_current_bot("bot-tool")
        try:
            bot_memory_note("user prefers dark mode")
        finally:
            bot_scope.reset_current_bot(token)
        assert "user prefers dark mode" in BotMemoryStore("bot-tool").read(CORE_FILE)

    def test_scoped_per_running_bot(self, tmp_path, monkeypatch):
        from server.bot_team.memory import bot_memory_note

        monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path))
        token = bot_scope.bind_current_bot("bot-one")
        try:
            bot_memory_note("from bot one")
            bot_scope.reset_current_bot(token)
            token = bot_scope.bind_current_bot("bot-two")
            bot_memory_note("from bot two")
        finally:
            bot_scope.reset_current_bot(token)
        assert "from bot one" in BotMemoryStore("bot-one").read(CORE_FILE)
        assert "from bot two" in BotMemoryStore("bot-two").read(CORE_FILE)
        assert "from bot two" not in BotMemoryStore("bot-one").read(CORE_FILE)

    def test_register_tools_is_idempotent(self):
        from server.bot_team.memory import register_tools
        from server.tool_registry import master_tools

        assert master_tools.has("bot_memory_note")
        assert register_tools(master_tools) == 0  # already registered at import


# ── Canonical worker wiring ──────────────────────────────────────────────────


class _SemanticStub:
    def __init__(self) -> None:
        self._histories: dict[str, list[dict]] = {}
        self.observed: list[dict] = []

    def get_system_prompt(self) -> str:
        return "SYSTEM PROMPT"

    async def semantic_step(self, instruction, *, session_id, llm_kwargs=None, prior_result=None):
        return {
            "kind": "action",
            "tool": "bot_memory_note",
            "arguments": {"text": "note from the canonical step"},
            "action_id": "act-1",
        }

    async def observe_action_result(self, result, *, session_id):
        self.observed.append(result)


class _GatewayStub:
    def execute(self, tool, arguments, physical, **_kwargs):
        return physical()


def _state_with_task(bot_id: str = "bot-worker"):
    from server.goal_run.models import GoalRunState, TaskNode, TaskStatus

    state = GoalRunState(goal_id="goal-memory", goal_text="memory goal")
    state.bot_id = bot_id
    state.tasks["step"] = TaskNode(
        id="step",
        title="step",
        instruction="remember the preference",
        acceptance=["note stored"],
        depends_on=[],
        assignee="builtin",
        status=TaskStatus.ready,
    )
    return state


def _adapter(agent: _SemanticStub, seen: dict | None = None) -> CanonicalWorkerAdapter:
    from server.goal_run.canonical_worker import CanonicalWorkerAdapter

    def gateway_executor(request):
        if seen is not None:
            # The running-bot binding is visible at physical execution time.
            seen["bot"] = bot_scope.current_bot()
        return "done"

    adapter = CanonicalWorkerAdapter(
        task_id="task-memory",
        objective="memory goal",
        semantic_agent=agent,
        semantic_session_id="session-memory",
        gateway_executor=gateway_executor,
    )
    adapter.action_gateway = _GatewayStub()
    return adapter


class TestCanonicalWorkerWiring:
    @pytest.mark.asyncio
    async def test_semantic_task_injects_memory_when_flag_on(self, rooted_store, monkeypatch):
        monkeypatch.setenv("VEYA_BOT_MEMORY_MD", "1")
        rooted_store.write(CORE_FILE, "worker memory note")
        agent = _SemanticStub()
        adapter = _adapter(agent)
        state = _state_with_task("bot-alpha")
        leaf = await adapter.execute_semantic_task(state, state.tasks["step"])
        assert leaf.status == "completed"
        block = agent._histories["session-memory"][1]["content"]
        assert "worker memory note" in block

    @pytest.mark.asyncio
    async def test_semantic_task_does_not_inject_when_flag_off(self, rooted_store, monkeypatch):
        monkeypatch.setenv("VEYA_BOT_MEMORY_MD", "0")
        rooted_store.write(CORE_FILE, "must not leak into the prompt")
        agent = _SemanticStub()
        adapter = _adapter(agent)
        state = _state_with_task("bot-alpha")
        leaf = await adapter.execute_semantic_task(state, state.tasks["step"])
        assert leaf.status == "completed"
        messages = [m for m in agent._histories.get("session-memory", [])]
        assert not any(
            str(m.get("content", "")).startswith("<!-- bot-memory-context") for m in messages
        )

    @pytest.mark.asyncio
    async def test_canonical_action_binds_the_running_bot(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path))
        seen: dict = {}
        agent = _SemanticStub()
        adapter = _adapter(agent, seen)
        state = _state_with_task("bot-bound")
        assert bot_scope.current_bot() is None
        await adapter.execute_semantic_task(state, state.tasks["step"])
        assert seen["bot"] == "bot-bound"  # physical step saw the running bot
        assert bot_scope.current_bot() is None  # binding reset afterwards

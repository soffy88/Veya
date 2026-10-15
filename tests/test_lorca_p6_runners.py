"""Lorca P6: run a bot's turns on another machine (runners).

Covers the runner registry (CRUD, persistence, refs-not-secrets), the health
probe with a fake transport, hub-side routing (local vs remote, the offline
error turn, retry-once), the runner-facing turn endpoint, and the rule the
feature rests on: a resolved runner token never reaches a response or a log.

No test opens a socket: the transport is an injected fake, and every file the
registry touches lands in ``tmp_path`` through the documented env overrides.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team import runner_client as runner_client_module
from server.bot_team.conversations import ConversationStore
from server.bot_team.runner_client import (
    RunnerClient,
    TransportResponse,
    build_turn_payload,
    redact_secret,
)
from server.bot_team.runners import (
    LOCAL_RUNNER_ID,
    RunnerError,
    RunnerRegistry,
    is_token_ref,
    resolve_token_ref,
    token_ref_label,
)
from server.bot_team.turns import run_bot_turn
from server.events import EventStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.routes import product as product_routes
from server.routes import runners as runners_routes
from server.routes.bot_team import router as bots_router
from server.routes.runners import router as runners_router
from server.task_store import TaskStore

USER = {"user_id": "lorca-user", "username": "lorca"}
SECRET = "runner-secret-token-value-123"
SECRET_REF = "VEYA_TEST_RUNNER_TOKEN"
BASE_URL = "http://100.64.0.5:8765"


class FakeTransport:
    """Records every call and answers from a queue; never opens a socket."""

    def __init__(self, responses: list[TransportResponse]):
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)

    def _next(self) -> TransportResponse:
        if self._responses:
            return self._responses.pop(0)
        return TransportResponse(0, error="fake transport has no queued response")

    async def post(
        self, url: str, *, payload: dict[str, Any], timeout: float, headers: dict[str, str]
    ) -> TransportResponse:
        self.calls.append(
            {"kind": "post", "url": url, "payload": payload, "timeout": timeout, "headers": headers}
        )
        return self._next()

    async def get(self, url: str, *, timeout: float, headers: dict[str, str]) -> TransportResponse:
        self.calls.append({"kind": "get", "url": url, "timeout": timeout, "headers": headers})
        return self._next()


@pytest.fixture
def runner_env(tmp_path, monkeypatch):
    """Every runner/bot/conversation file lands in tmp_path; the secret is in env."""

    monkeypatch.setenv("VEYA_RUNNERS_PATH", str(tmp_path / "runners.json"))
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bots.json"))
    monkeypatch.setenv(SECRET_REF, SECRET)
    return tmp_path


@pytest.fixture
def product_env(runner_env, tmp_path, monkeypatch):
    """The product path with the stores pointed at tmp_path (as in P1/P2)."""

    events = EventStore(tmp_path / "events.jsonl")
    tasks = TaskStore(tmp_path / "tasks.json", event_store=events)
    monkeypatch.setattr(product_routes, "task_store", tasks)

    class _History:
        async def save(self, session_id: str, messages: list[Any], *, user_id: str) -> None:
            return None

    monkeypatch.setattr(product_routes, "default_history_store", lambda: _History())
    return tasks


@pytest.fixture
def fake_master(monkeypatch):
    """Fake the semantic authority: no model, no gateway, deterministic reply."""

    captured: list[Any] = []

    async def fake_execute(request: Any) -> GoalRunResponse:
        captured.append(request)
        objective = str(getattr(request, "objective", "") or "")
        return GoalRunResponse(
            goal_id="goal-lorca-1",
            status=GoalStatus.completed,
            phase="finalized",
            summary=f"done:{objective.splitlines()[-1][:24]}",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)
    return captured


@pytest.fixture
def client():
    app = FastAPI()
    # W11-AUTH: bot routes now require a signed-in user (D2).
    from server import auth as _w11_auth

    app.dependency_overrides[_w11_auth.require_user] = lambda: {
        "user_id": "anonymous",
        "username": "anonymous",
    }
    app.include_router(bots_router)
    app.include_router(runners_router)
    return TestClient(app)


def _add_runner(runner_id: str = "mac") -> Any:
    return RunnerRegistry().add(
        runner_id=runner_id,
        name="Mac studio",
        base_url=BASE_URL,
        token_ref=SECRET_REF,
    )


def _remote_bot() -> BotSpec:
    return BotSpec(
        bot_id="researcher",
        owner_id=USER["user_id"],
        name="Researcher",
        description="Finds and summarises sources.",
        instructions="Cite every claim.",
        provider="ollama",
        model="qwen-test",
        runner_id="mac",
    )


def _runners_file() -> Path:
    return Path(os.environ["VEYA_RUNNERS_PATH"])


# ── registry ───────────────────────────────────────────────────────────────


def test_registry_add_list_remove_roundtrip(runner_env):
    del runner_env
    record = _add_runner()
    assert record.to_view() == {
        "runner_id": "mac",
        "name": "Mac studio",
        "base_url": BASE_URL,
        "token_ref": SECRET_REF,
        "last_seen": None,
        "status": "unknown",
    }

    # The implicit local runner is always present, first, and has no record.
    views = RunnerRegistry().views()
    assert views[0] == {
        "runner_id": LOCAL_RUNNER_ID,
        "name": "this server",
        "base_url": "",
        "token_ref": "",
        "last_seen": None,
        "status": "local",
        "implicit": True,
    }
    assert [view["runner_id"] for view in views[1:]] == ["mac"]

    # Persistence: a fresh instance (another process) reads the same catalog.
    stored = json.loads(_runners_file().read_text(encoding="utf-8"))
    assert set(stored) == {"mac"}
    assert stored["mac"]["token_ref"] == SECRET_REF
    assert SECRET not in _runners_file().read_text(encoding="utf-8")

    assert RunnerRegistry().remove("mac") is True
    assert RunnerRegistry().get("mac") is None
    assert RunnerRegistry().remove("mac") is False
    assert RunnerRegistry().remove(LOCAL_RUNNER_ID) is False


def test_registry_refuses_bad_fields_and_keeps_its_file(runner_env):
    del runner_env
    registry = RunnerRegistry()
    for bad_id in ("Mac Studio", "-mac", "a" * 65, LOCAL_RUNNER_ID):
        with pytest.raises(RunnerError):
            registry.add(runner_id=bad_id, name="x", base_url=BASE_URL, token_ref=SECRET_REF)
    with pytest.raises(RunnerError):
        registry.add(runner_id="pc", name="x", base_url="100.64.0.5:8765", token_ref=SECRET_REF)
    with pytest.raises(RunnerError):
        registry.add(runner_id="pc", name="x", base_url="http://a b:8765", token_ref=SECRET_REF)
    # A raw token is refused as a token_ref: only a ref may be persisted.
    with pytest.raises(RunnerError):
        registry.add(runner_id="pc", name="x", base_url=BASE_URL, token_ref=SECRET)
    assert not _runners_file().exists()

    _add_runner()
    with pytest.raises(RunnerError):
        _add_runner()  # duplicate id
    assert RunnerRegistry().list()[0].runner_id == "mac"
    assert SECRET not in _runners_file().read_text(encoding="utf-8")


def test_registry_survives_a_corrupt_file(runner_env):
    del runner_env
    _runners_file().write_text("{not json", encoding="utf-8")
    registry = RunnerRegistry()
    assert registry.list() == []
    assert [view["runner_id"] for view in registry.views()] == [LOCAL_RUNNER_ID]


def test_registry_health_projection_keeps_offline_runners(runner_env):
    del runner_env
    _add_runner()
    online = RunnerRegistry().record_health("mac", status="online", seen_at=123.5)
    assert online is not None
    assert online.status == "online"
    assert online.last_seen == 123.5
    assert RunnerRegistry().get("mac").status == "online"

    # An unreachable runner keeps its record: forgetting it would silently
    # send its bots back to local.
    RunnerRegistry().record_health("mac", status="offline")
    kept = RunnerRegistry().get("mac")
    assert kept is not None
    assert kept.status == "offline"
    assert RunnerRegistry().record_health("ghost", status="online") is None


def test_token_ref_resolves_env_then_file_never_stored(runner_env, tmp_path, monkeypatch):
    del runner_env
    assert is_token_ref(SECRET_REF) is True
    assert is_token_ref("~/.veya/runner-tokens/mac") is True
    assert is_token_ref(SECRET) is False  # a raw secret is not a ref
    assert resolve_token_ref(SECRET_REF) == SECRET

    token_file = tmp_path / "mac.token"
    token_file.write_text(f"{SECRET}\n", encoding="utf-8")
    assert resolve_token_ref(str(token_file)) == SECRET

    # The environment wins over a file with the same name.
    monkeypatch.setenv("dual-ref", "from-env")
    (tmp_path / "dual-ref").write_text("from-file", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert resolve_token_ref("dual-ref") == "from-env"

    assert resolve_token_ref("VEYA_TEST_RUNNER_MISSING") == ""
    assert token_ref_label(SECRET_REF) == SECRET_REF
    assert token_ref_label(SECRET) == "<redacted>"  # never echoed
    assert token_ref_label("") == "<none>"


def test_redact_secret_scrubs_the_token(runner_env):
    del runner_env
    assert redact_secret(f"Bearer {SECRET} refused", SECRET) == "Bearer [redacted] refused"
    assert redact_secret("no secret here", "") == "no secret here"


# ── health probe ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_probe_reports_status_without_the_token(runner_env):
    del runner_env
    record = _add_runner()

    online = FakeTransport([TransportResponse(200, {"status": "ok", "version": "0.2.0"})])
    probe = await RunnerClient(transport=online).health(record)
    assert probe == {
        "runner_id": "mac",
        "status": "online",
        "detail": "ok",
        "version": "0.2.0",
    }
    # A health check only proves the port answers: no secret is resolved or sent.
    call = online.calls[0]
    assert call["url"] == f"{BASE_URL}/health"
    assert call["headers"] == {}
    assert call["timeout"] == 5.0

    unreachable = FakeTransport([TransportResponse(0, error="ConnectError: refused")])
    down = await RunnerClient(transport=unreachable).health(record)
    assert down["status"] == "offline"
    assert "ConnectError" in down["detail"]

    broken = FakeTransport([TransportResponse(503, None)])
    assert (await RunnerClient(transport=broken).health(record))["detail"] == "HTTP 503"


# ── forwarding client ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forward_turn_sends_bearer_and_the_whole_bot(runner_env):
    del runner_env
    record = _add_runner()
    bot = _remote_bot()
    fake = FakeTransport([TransportResponse(200, {"ok": True, "turn": {"reply": "hi"}})])
    outcome = await RunnerClient(transport=fake).forward_turn(
        record,
        build_turn_payload(bot=bot, text="find sources", user=USER, trace_id="tr-1"),
        token=SECRET,
    )

    assert outcome.ok is True
    assert outcome.retried is False
    call = fake.calls[0]
    assert call["url"] == f"{BASE_URL}/api/v1/runners/turn"
    assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert call["timeout"] == 300.0
    payload = call["payload"]
    assert payload["version"] == 1
    assert payload["runner_id"] == "mac"
    assert payload["trace_id"] == "tr-1"
    assert payload["text"] == "find sources"
    assert payload["user"] == {"user_id": USER["user_id"], "username": USER["username"]}
    # The hub owns the catalog, so the bot travels whole.
    assert payload["bot"] == {
        "bot_id": "researcher",
        "name": "Researcher",
        "description": "Finds and summarises sources.",
        "instructions": "Cite every claim.",
        "provider": "ollama",
        "model": "qwen-test",
        "owner_id": USER["user_id"],
    }
    assert SECRET not in json.dumps(payload)  # the header carries it, never the body


@pytest.mark.asyncio
async def test_forward_turn_retries_once_then_gives_up(runner_env):
    del runner_env
    record = _add_runner()
    payload = {"version": 1}

    flaky = FakeTransport(
        [
            TransportResponse(0, error="ConnectError: refused"),
            TransportResponse(200, {"ok": True, "turn": {"reply": "hi"}}),
        ]
    )
    outcome = await RunnerClient(transport=flaky).forward_turn(record, payload, token=SECRET)
    assert outcome.ok is True
    assert outcome.retried is True
    assert len(flaky.calls) == 2

    server_error = FakeTransport(
        [TransportResponse(500, None), TransportResponse(200, {"ok": True, "turn": {}})]
    )
    outcome = await RunnerClient(transport=server_error).forward_turn(record, payload, token=SECRET)
    assert outcome.ok is True
    assert outcome.retried is True

    dead = FakeTransport(
        [
            TransportResponse(0, error="ConnectError: refused"),
            TransportResponse(500, None),
        ]
    )
    outcome = await RunnerClient(transport=dead).forward_turn(record, payload, token=SECRET)
    assert outcome.ok is False
    assert "HTTP 500" in outcome.error
    assert len(dead.calls) == 2

    # A 4xx is a refusal: retrying it only doubles the wait.
    refused = FakeTransport(
        [
            TransportResponse(403, {"error": "no write permission"}),
            TransportResponse(200, {}),
        ]
    )
    outcome = await RunnerClient(transport=refused).forward_turn(record, payload, token=SECRET)
    assert outcome.ok is False
    assert outcome.retried is False
    assert outcome.error == "no write permission"
    assert len(refused.calls) == 1


# ── routing: local vs remote ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_turn_is_unchanged_by_lorca(product_env, fake_master):
    turn = await run_bot_turn(
        bot=BotSpec(
            bot_id="researcher",
            owner_id=USER["user_id"],
            name="Researcher",
            instructions="Cite every claim.",
        ),
        text="summarise three sources",
        user=USER,
        conversations=ConversationStore(),
    )

    assert turn.reply == "done:summarise three sources"
    assert len(fake_master) == 1
    assert turn.task_id and turn.session_id  # the local durable record
    messages = ConversationStore().list("researcher")
    assert [item["role"] for item in messages] == ["user", "bot"]


@pytest.mark.asyncio
async def test_remote_turn_is_forwarded_and_not_executed_here(
    product_env, fake_master, monkeypatch
):
    _add_runner()
    fake = FakeTransport(
        [
            TransportResponse(
                200,
                {
                    "ok": True,
                    "turn": {
                        "bot_id": "researcher",
                        "task_id": "task-runner-1",
                        "session_id": "sess-runner-1",
                        "reply": "found three sources",
                        "status": "completed",
                    },
                },
            )
        ]
    )
    monkeypatch.setattr(
        runner_client_module, "default_runner_client", lambda: RunnerClient(transport=fake)
    )
    store = ConversationStore()

    turn = await run_bot_turn(
        bot=_remote_bot(), text="find sources", user=USER, conversations=store
    )

    assert len(fake_master) == 0  # nothing ran on this machine
    assert turn.reply == "found three sources"
    assert turn.status == "completed"
    # The task/session ids are the runner's own durable record.
    assert (turn.task_id, turn.session_id) == ("task-runner-1", "sess-runner-1")
    # The hub's trace id is carried into the runner's turn.
    assert fake.calls[0]["payload"]["trace_id"] == turn.trace_id
    assert [item["role"] for item in store.list("researcher")] == ["user", "bot"]


@pytest.mark.asyncio
async def test_offline_runner_produces_a_visible_error_turn(
    product_env, fake_master, runner_env, monkeypatch
):
    _add_runner()
    fake = FakeTransport(
        [
            TransportResponse(0, error="ConnectError: [Errno 111] Connection refused"),
            TransportResponse(0, error="ConnectError: [Errno 111] Connection refused"),
        ]
    )
    monkeypatch.setattr(
        runner_client_module, "default_runner_client", lambda: RunnerClient(transport=fake)
    )
    store = ConversationStore()

    turn = await run_bot_turn(
        bot=_remote_bot(), text="find sources", user=USER, conversations=store
    )

    assert turn.status == "error"
    assert turn.task_id == ""
    assert "Researcher" in turn.reply
    assert "'mac'" in turn.reply
    assert BASE_URL in turn.reply
    assert "NOT executed here" in turn.reply
    assert len(fake.calls) == 2  # retried once, then the error turn
    assert len(fake_master) == 0  # never silently falls back to local
    # The failure is in the transcript, exactly like a reply.
    messages = store.list("researcher")
    assert [item["role"] for item in messages] == ["user", "bot"]
    assert messages[1]["text"] == turn.reply


@pytest.mark.asyncio
async def test_unknown_runner_is_a_visible_error_turn(product_env, fake_master, monkeypatch):
    fake = FakeTransport([])
    monkeypatch.setattr(
        runner_client_module, "default_runner_client", lambda: RunnerClient(transport=fake)
    )

    turn = await run_bot_turn(
        bot=BotSpec(bot_id="researcher", owner_id=USER["user_id"], runner_id="ghost"),
        text="hi",
        user=USER,
        conversations=ConversationStore(),
    )

    assert turn.status == "error"
    assert "not registered" in turn.reply
    assert fake.calls == []  # never dialled an unregistered machine
    assert len(fake_master) == 0


@pytest.mark.asyncio
async def test_unreachable_token_ref_is_a_visible_error_turn(product_env, fake_master, monkeypatch):
    RunnerRegistry().add(
        runner_id="mac",
        name="Mac studio",
        base_url=BASE_URL,
        token_ref="VEYA_TEST_RUNNER_MISSING",
    )
    fake = FakeTransport([])
    monkeypatch.setattr(
        runner_client_module, "default_runner_client", lambda: RunnerClient(transport=fake)
    )

    turn = await run_bot_turn(
        bot=_remote_bot(),
        text="hi",
        user=USER,
        conversations=ConversationStore(),
    )

    assert turn.status == "error"
    assert "token secret is not reachable" in turn.reply
    assert fake.calls == []  # never sent an empty bearer header
    assert len(fake_master) == 0


@pytest.mark.asyncio
async def test_runner_token_never_reaches_a_reply_or_a_log(
    product_env, fake_master, monkeypatch, caplog
):
    caplog.set_level(logging.WARNING, logger="veya.bot_team.runners")
    _add_runner()
    leak = f"ConnectError: rejected header 'Bearer {SECRET}'"
    fake = FakeTransport([TransportResponse(0, error=leak), TransportResponse(0, error=leak)])
    monkeypatch.setattr(
        runner_client_module, "default_runner_client", lambda: RunnerClient(transport=fake)
    )
    store = ConversationStore()

    turn = await run_bot_turn(bot=_remote_bot(), text="hi", user=USER, conversations=store)

    assert "[redacted]" in turn.reply
    assert SECRET not in turn.reply
    assert SECRET not in caplog.text  # the hub-side warning is redacted too
    assert SECRET not in json.dumps(store.list("researcher"))
    assert SECRET not in _runners_file().read_text(encoding="utf-8")


# ── the runner-facing turn endpoint ───────────────────────────────────────────


def _write_token_auth(monkeypatch):
    """The runner's credential authority: one write token, one read-only."""

    from veya.remote.auth import RemoteAuth
    from veya.remote.models import RemotePermissions

    def fake_runner_auth() -> RemoteAuth:
        auth = RemoteAuth()
        auth.issue("hub", permissions=RemotePermissions(write=True), secret="hub-write-token")
        auth.issue("hub-read", permissions=RemotePermissions(), secret="hub-read-token")
        return auth

    monkeypatch.setattr(runners_routes, "runner_auth", fake_runner_auth)


@pytest.mark.asyncio
async def test_runner_turn_endpoint_executes_the_hubs_bot_locally(
    product_env, fake_master, client, monkeypatch
):
    _write_token_auth(monkeypatch)
    payload = build_turn_payload(
        bot=_remote_bot(), text="summarise three sources", user=USER, trace_id="tr-hub-1"
    )

    answered = client.post(
        "/api/v1/runners/turn",
        json=payload,
        headers={"Authorization": "Bearer hub-write-token"},
    )

    assert answered.status_code == 200
    turn = answered.json()["turn"]
    assert turn["status"] == "completed"
    assert turn["reply"] == "done:summarise three sources"
    assert turn["task_id"] and turn["session_id"]  # this machine's own record
    assert turn["trace_id"] == "tr-hub-1"  # the hub's trace id is kept
    # The turn went through the normal path: MasterAgent -> GoalRun, as this bot.
    assert len(fake_master) == 1
    request = fake_master[0]
    assert request.constraints.metadata["bot_id"] == "researcher"
    assert request.constraints.metadata["semantic_llm_kwargs"] == {
        "provider": "ollama",
        "model": "qwen-test",
    }
    assert "Cite every claim." in request.objective


def test_runner_turn_endpoint_fails_closed(product_env, fake_master, client, monkeypatch):
    _write_token_auth(monkeypatch)
    payload = {
        "version": 1,
        "runner_id": "mac",
        "user": {"user_id": USER["user_id"]},
        "bot": {"bot_id": "researcher"},
        "text": "hi",
    }

    # No token, a wrong token, and nothing configured at all: all denied.
    assert client.post("/api/v1/runners/turn", json=payload).status_code == 401
    assert (
        client.post(
            "/api/v1/runners/turn",
            json=payload,
            headers={"Authorization": "Bearer wrong-token"},
        ).status_code
        == 401
    )
    from veya.remote.auth import RemoteAuth

    monkeypatch.setattr(runners_routes, "runner_auth", lambda: RemoteAuth())
    assert (
        client.post(
            "/api/v1/runners/turn",
            json=payload,
            headers={"Authorization": "Bearer hub-write-token"},
        ).status_code
        == 401
    )

    # A read-only token may not cause real work to run.
    _write_token_auth(monkeypatch)
    refused = client.post(
        "/api/v1/runners/turn",
        json=payload,
        headers={"Authorization": "Bearer hub-read-token"},
    )
    assert refused.status_code == 403
    assert len(fake_master) == 0


# ── runner management REST ─────────────────────────────────────────────────────


def test_runners_rest_crud_and_local_is_implicit(runner_env, client):
    del runner_env
    listed = client.get("/api/v1/runners")
    assert listed.status_code == 200
    assert listed.json()["default_runner_id"] == "local"
    assert [view["runner_id"] for view in listed.json()["runners"]] == ["local"]

    created = client.post(
        "/api/v1/runners",
        json={
            "runner_id": "mac",
            "name": "Mac studio",
            "base_url": BASE_URL,
            "token_ref": SECRET_REF,
        },
    )
    assert created.status_code == 200
    view = created.json()["runner"]
    assert view["runner_id"] == "mac"
    assert view["token_ref"] == SECRET_REF
    assert SECRET not in json.dumps(view)

    for bad in (
        {"runner_id": "mac", "base_url": BASE_URL, "token_ref": SECRET_REF},  # duplicate
        {"runner_id": "local", "base_url": BASE_URL, "token_ref": SECRET_REF},
        {"runner_id": "pc", "base_url": "100.64.0.5:8765", "token_ref": SECRET_REF},
        {"runner_id": "pc", "base_url": BASE_URL, "token_ref": SECRET},  # raw token
    ):
        assert client.post("/api/v1/runners", json=bad).status_code == 422

    assert client.delete("/api/v1/runners/local").status_code == 422
    assert client.delete("/api/v1/runners/ghost").status_code == 404
    assert client.delete("/api/v1/runners/mac").status_code == 200
    listed_again = client.get("/api/v1/runners").json()["runners"]
    assert [view["runner_id"] for view in listed_again] == ["local"]


def test_runner_health_rest_probes_with_a_timeout(runner_env, client, monkeypatch):
    del runner_env
    _add_runner()

    online = FakeTransport([TransportResponse(200, {"status": "ok", "version": "0.2.0"})])
    monkeypatch.setattr(
        runners_routes, "default_runner_client", lambda: RunnerClient(transport=online)
    )
    checked = client.post("/api/v1/runners/mac/health")
    assert checked.status_code == 200
    assert checked.json()["runner"]["status"] == "online"
    assert checked.json()["runner"]["last_seen"] is not None
    assert checked.json()["health"]["version"] == "0.2.0"
    assert online.calls[0]["timeout"] == 5.0
    # Persisted: the health projection is what a later list shows.
    assert client.get("/api/v1/runners").json()["runners"][1]["status"] == "online"

    unreachable = FakeTransport([TransportResponse(0, error="ConnectError: refused")])
    monkeypatch.setattr(
        runners_routes, "default_runner_client", lambda: RunnerClient(transport=unreachable)
    )
    down = client.post("/api/v1/runners/mac/health")
    assert down.json()["runner"]["status"] == "offline"
    # An offline runner is marked, never forgotten.
    assert client.get("/api/v1/runners").json()["runners"][1]["runner_id"] == "mac"

    assert client.post("/api/v1/runners/ghost/health").status_code == 404
    local = client.post("/api/v1/runners/local/health")
    assert local.status_code == 200
    assert local.json()["status"] == "local"


def test_bot_can_be_pointed_at_a_runner_over_rest(runner_env, client):
    del runner_env
    assert (
        client.post("/api/v1/bots", json={"bot_id": "researcher", "name": "Researcher"}).status_code
        == 200
    )
    assert client.get("/api/v1/bots/researcher").json()["bot"]["runner_id"] == "local"
    client.post(
        "/api/v1/runners",
        json={
            "runner_id": "mac",
            "name": "Mac studio",
            "base_url": BASE_URL,
            "token_ref": SECRET_REF,
        },
    )

    bound = client.post("/api/v1/runners/mac/bots/researcher")
    assert bound.status_code == 200
    assert bound.json()["runner_id"] == "mac"
    assert bound.json()["runner_base_url"] == BASE_URL
    assert BotRegistry().get("researcher").runner_id == "mac"

    # local brings it back; unknown runner or bot are 404s.
    assert client.post("/api/v1/runners/local/bots/researcher").json()["runner_id"] == "local"
    assert client.post("/api/v1/runners/ghost/bots/researcher").status_code == 404
    assert client.post("/api/v1/runners/mac/bots/ghost").status_code == 404

    # The same validation guards the team PATCH: a typo cannot bind a bot to a
    # machine that does not exist.
    assert client.patch("/api/v1/bots/researcher", json={"runner_id": "ghost"}).status_code == 422
    assert client.patch("/api/v1/bots/researcher", json={"runner_id": "mac"}).status_code == 200
    assert BotRegistry().get("researcher").runner_id == "mac"


@pytest.mark.asyncio
async def test_remote_bot_message_reports_the_runner_and_no_local_workbench(
    product_env, fake_master, client, monkeypatch
):
    _add_runner()
    client.post("/api/v1/bots", json={"bot_id": "researcher", "name": "Researcher"})
    client.post("/api/v1/runners/mac/bots/researcher")
    fake = FakeTransport(
        [
            TransportResponse(
                200,
                {
                    "ok": True,
                    "turn": {
                        "task_id": "task-runner-1",
                        "session_id": "sess-runner-1",
                        "reply": "from the runner",
                        "status": "completed",
                    },
                },
            )
        ]
    )
    monkeypatch.setattr(
        runner_client_module, "default_runner_client", lambda: RunnerClient(transport=fake)
    )

    sent = client.post("/api/v1/bots/researcher/messages", json={"text": "hi"})

    assert sent.status_code == 200
    body = sent.json()
    assert body["runner_id"] == "mac"
    assert body["reply"] == "from the runner"
    assert (body["task_id"], body["session_id"]) == ("task-runner-1", "sess-runner-1")
    # A remote turn's task lives on the runner, so this server's workbench URL
    # would be meaningless.
    assert body["workbench_url"] is None
    assert len(fake_master) == 0

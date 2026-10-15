"""Lorca P5: Web Push for the bot team.

Covers the half of P5 that can be verified without a push service:

* VAPID key pairs are generated once, persisted with owner-only permissions
  and survive a reload in the wire format browsers expect;
* subscription create / list / delete, per user, including that one user may
  not drop another user's subscription;
* the notification-centre hook sends ``<bot name>: first line`` — and only for
  messages that asked to leave the browser;
* ``?async=1`` answers ``202`` with the task id and still finishes the turn
  through the same product path, pushing the reply when it lands.

``pywebpush`` is deliberately not installed in this environment (it is an
optional extra), so the real sender is replaced with a fake one: the hook, the
filtering and the payload are what is under test here. ``default_sender``'s
"not installed" degradation is covered separately.
"""

from __future__ import annotations

import json
import os
import stat
from base64 import urlsafe_b64decode
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.bot_team.async_turns import (
    drain_async_turns,
    pending_refs,
    start_group_turn_async,
)
from server.bot_team.conversations import ConversationStore
from server.bot_team.groups import GroupStore
from server.bot_team.push import (
    _P256_N,
    PushNotifier,
    PushPayload,
    PushSubscription,
    SubscriptionStore,
    VapidKeyManager,
    VapidKeys,
    _scalar_mult,
    _stdlib_vapid_pair,
    announce_bot_reply,
    default_sender,
    install_push_hook,
    on_p256_curve,
    subscription_id,
)
from server.events import EventStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.notification_center import WEB_PUSH_FLAG, global_notifier
from server.routes import product as product_routes
from server.routes.bot_team import router as bots_router
from server.routes.push import router as push_router
from server.task_store import TaskStore

USER = {"user_id": "lorca-user", "username": "lorca"}
_P256DH = "BA9HrKZEwHPMLuYCmGQUYDBnMFkdSZpTLIq6THfHrzE"
_AUTH = "kTZ4nDLQv9BrOsWgEQrjXA"

#: A Chef reply carrying a proposal in the documented block. Only this shape is
#: read as a team to create, and only after an explicit accept.
_CHEF_REPLY = (
    "Here is a team for your research work:\n\n"
    "```veya-bots\n"
    '{"bots": [{"bot_id": "researcher", "name": "Researcher",'
    ' "description": "Finds sources", "instructions": "Cite sources."},'
    ' {"bot_id": "writer", "name": "Writer",'
    ' "description": "Writes briefs", "instructions": "Be brief."}]}\n'
    "```"
)


@pytest.fixture
def push_root(tmp_path, monkeypatch) -> Path:
    """Every push file lands in tmp_path; nothing touches the real ~/.veya."""

    root = tmp_path / "push-root"
    monkeypatch.setenv("VEYA_PUSH_ROOT", str(root))
    return root


@pytest.fixture
def bot_env(tmp_path, monkeypatch) -> dict[str, Path]:
    bot_root = tmp_path / "bots-root"
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(bot_root))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    events = EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(
        product_routes, "task_store", TaskStore(tmp_path / "tasks.json", event_store=events)
    )

    class _History:
        async def save(self, session_id: str, messages: list[Any], *, user_id: str) -> None:
            return None

    monkeypatch.setattr(product_routes, "default_history_store", lambda: _History())
    return {"root": bot_root}


@pytest.fixture
def fake_master(monkeypatch):
    """Deterministic reply per bot: 'say:<bot id>'."""

    calls: list[Any] = []

    async def fake_execute(request: Any) -> GoalRunResponse:
        calls.append(request)
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=f"say:{bot_id}",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)
    return calls


@pytest.fixture
def recorder(push_root):
    """A notifier bound to tmp_path whose sender only records.

    Installed on the global notification centre for the whole test, so nothing
    in this module can reach the real ``~/.veya/push`` or leak an observer into
    another test.
    """

    calls: list[dict[str, Any]] = []

    def fake_sender(payload: PushPayload, subscription: PushSubscription, vapid: VapidKeys):
        calls.append({"payload": payload, "subscription": subscription, "vapid": vapid})
        return True, "delivered"

    notifier = PushNotifier(store=SubscriptionStore(push_root), sender=fake_sender)
    install_push_hook(notifier)
    yield {"calls": calls, "notifier": notifier}
    notifier.uninstall()


@pytest.fixture
def subscribed(recorder) -> dict[str, Any]:
    store = recorder["notifier"].store
    store.upsert(
        endpoint="https://push.example.com/aaa",
        p256dh=_P256DH,
        auth=_AUTH,
        user_id=USER["user_id"],
    )
    return recorder


def _current_user() -> dict[str, Any]:
    """What the bearer token resolves to for the whole module."""

    return dict(USER)


@pytest.fixture
def app():
    application = FastAPI()
    # W11-AUTH: bot routes now require a signed-in user (D2).
    from server import auth as _w11_auth

    application.dependency_overrides[_w11_auth.require_user] = lambda: dict(USER)
    application.include_router(bots_router)
    application.include_router(push_router)
    # Push is a per-user surface, and without a token the routes fall back to
    # "anonymous" (server/auth.py). Resolving the token the way the browser does
    # keeps a turn's announcement aimed at the user who subscribed.
    application.dependency_overrides[auth_mod.get_current_user] = _current_user
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def _register(*bot_ids: str) -> None:
    registry = BotRegistry()
    for bot_id in bot_ids:
        registry.register(
            BotSpec(
                bot_id=bot_id,
                owner_id=USER["user_id"],
                name=bot_id.capitalize(),
                description=f"Does {bot_id} work.",
            )
        )


def _dec(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return urlsafe_b64decode(padded)


# ── P5.1 VAPID keys ──────────────────────────────────────────────────────


def test_vapid_keys_are_generated_once_and_persisted(push_root):
    manager = VapidKeyManager(push_root)
    first = manager.load_or_create()

    # A second call and a fresh manager both read the same stored pair.
    assert manager.load_or_create() == first
    reloaded = VapidKeyManager(push_root).load()
    assert reloaded == first

    stored = json.loads((push_root / "vapid.json").read_text(encoding="utf-8"))
    assert set(stored) >= {"private_key", "public_key", "subject"}
    assert stored["public_key"] == first.public_key
    assert stored["subject"]

    # The key is a secret: owner-only, and the mode is not the umask default.
    mode = stat.S_IMODE(os.stat(push_root / "vapid.json").st_mode)
    assert mode == 0o600

    # Wire format the browser and pywebpush expect: base64url, no padding,
    # public key an uncompressed P-256 point.
    public = _dec(first.public_key)
    assert len(public) == 65 and public[0] == 0x04
    x = int.from_bytes(public[1:33], "big")
    y = int.from_bytes(public[33:], "big")
    assert on_p256_curve(x, y)
    assert len(_dec(first.private_key)) == 32


def test_generated_public_key_is_the_private_keys_point(push_root):
    """The two halves belong together (catches a mismatched generator)."""

    del push_root
    private, public = _stdlib_pair()
    x, y = _scalar_mult(int.from_bytes(_dec(private), "big"))
    assert x is not None
    assert _dec(public) == b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def test_public_key_differs_between_roots(push_root, tmp_path):
    """Two deployments get two key pairs (each root is its own identity)."""

    other = VapidKeyManager(tmp_path / "other-root").load_or_create()
    assert other.public_key != VapidKeyManager(push_root).load_or_create().public_key


def test_a_corrupt_vapid_file_is_regenerated_not_trusted(push_root):
    (push_root / "vapid.json").parent.mkdir(parents=True, exist_ok=True)
    (push_root / "vapid.json").write_text("{ not json", encoding="utf-8")

    keys = VapidKeyManager(push_root).load_or_create()

    assert keys.private_key and keys.public_key
    assert json.loads((push_root / "vapid.json").read_text(encoding="utf-8"))["public_key"] == (
        keys.public_key
    )


def _stdlib_pair() -> tuple[str, str]:
    # The stdlib generator is the dependency-free path; drive it directly so
    # this assertion does not depend on whether cryptography is installed.
    private, public = _stdlib_vapid_pair()
    assert 0 < int.from_bytes(_dec(private), "big") < int(_P256_N)
    return private, public


# ── P5.2 subscription CRUD ───────────────────────────────────────────────


def test_subscription_store_crud_is_per_user(push_root):
    store = SubscriptionStore(push_root)
    record = store.upsert(
        endpoint="https://push.example.com/aaa",
        p256dh=_P256DH,
        auth=_AUTH,
        user_id="user-a",
    )
    assert record.id == subscription_id("https://push.example.com/aaa")
    assert [item.id for item in store.list("user-a")] == [record.id]

    # Same endpoint again is a refresh, not a second subscription.
    store.upsert(
        endpoint="https://push.example.com/aaa", p256dh=_P256DH, auth=_AUTH, user_id="user-a"
    )
    assert len(store.list("user-a")) == 1

    # Another user's endpoint is a separate record and is invisible to the first.
    other = store.upsert(
        endpoint="https://push.example.com/bbb", p256dh=_P256DH, auth=_AUTH, user_id="user-b"
    )
    assert other.id != record.id
    assert len(store.list("user-a")) == 1
    assert len(store.list("user-b")) == 1
    assert len(store.list()) == 2

    # One user may not drop another user's subscription.
    assert store.delete(record.id, user_id="user-b") is False
    assert len(store.list("user-a")) == 1
    assert store.delete(record.id, user_id="user-a") is True
    assert store.list("user-a") == []
    assert store.delete(record.id, user_id="user-a") is False


def test_subscription_store_refuses_nonsense(push_root):
    store = SubscriptionStore(push_root)
    cases = [
        # An endpoint that is not a URL at all.
        {"endpoint": "not-a-url", "p256dh": _P256DH, "auth": _AUTH},
        # A missing half of the key pair.
        {"endpoint": "https://push.example.com/x", "p256dh": _P256DH, "auth": ""},
        # Key material that is not base64url.
        {"endpoint": "https://push.example.com/x", "p256dh": "!!", "auth": _AUTH},
    ]
    for bad in cases:
        with pytest.raises(ValueError):
            store.upsert(user_id="user-a", **bad)


def test_subscription_endpoints_over_http(client, recorder):
    del recorder
    created = client.post(
        "/api/v1/push/subscriptions",
        json={
            "endpoint": "https://push.example.com/aaa",
            "keys": {"p256dh": _P256DH, "auth": _AUTH},
        },
    )
    assert created.status_code == 201
    subscription = created.json()["subscription"]
    assert subscription["id"] == subscription_id("https://push.example.com/aaa")
    # The raw key material is never echoed back.
    assert _P256DH not in json.dumps(subscription)

    listed = client.get("/api/v1/push/subscriptions")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["subscriptions"]] == [subscription["id"]]

    duplicate = client.post(
        "/api/v1/push/subscriptions",
        json={
            "endpoint": "https://push.example.com/aaa",
            "keys": {"p256dh": _P256DH, "auth": _AUTH},
        },
    )
    assert duplicate.status_code == 201
    assert len(client.get("/api/v1/push/subscriptions").json()["subscriptions"]) == 1

    malformed = client.post(
        "/api/v1/push/subscriptions",
        json={"endpoint": "nope", "keys": {"p256dh": "x", "auth": "y"}},
    )
    assert malformed.status_code == 422
    missing_key = client.post(
        "/api/v1/push/subscriptions",
        json={"endpoint": "https://push.example.com/aaa", "keys": {"p256dh": _P256DH}},
    )
    assert missing_key.status_code == 422

    gone = client.delete(f"/api/v1/push/subscriptions/{subscription['id']}")
    assert gone.status_code == 200
    assert client.get("/api/v1/push/subscriptions").json()["subscriptions"] == []
    assert client.delete(f"/api/v1/push/subscriptions/{subscription['id']}").status_code == 404

    # The endpoint-only form is what a browser that lost its id has.
    client.post(
        "/api/v1/push/subscriptions",
        json={
            "endpoint": "https://push.example.com/ccc",
            "keys": {"p256dh": _P256DH, "auth": _AUTH},
        },
    )
    by_endpoint = client.delete(
        "/api/v1/push/subscriptions", params={"endpoint": "https://push.example.com/ccc"}
    )
    assert by_endpoint.status_code == 200
    assert client.get("/api/v1/push/subscriptions").json()["subscriptions"] == []


def test_public_key_and_status_endpoints(client, push_root):
    """The browser gets the server's stored public key, not a generated one."""

    expected = VapidKeyManager(push_root).load_or_create().public_key
    key = client.get("/api/v1/push/public_key")

    assert key.status_code == 200
    assert key.json()["public_key"] == expected
    assert "private" not in key.json()

    status = client.get("/api/v1/push/status")
    assert status.status_code == 200
    assert status.json()["enabled"] is (status.json()["sender"] == "pywebpush")
    assert status.json()["subscriptions"] == 0


# ── P5.3 notification hook ───────────────────────────────────────────────


def test_a_finished_bot_turn_is_pushed_as_bot_name_plus_first_line(subscribed):
    announce_bot_reply(
        bot_name="Researcher",
        reply="first line of the answer\nsecond line the user does not need",
        task_id="task-1",
        user_id=USER["user_id"],
        bot_id="researcher",
    )

    calls = subscribed["calls"]
    assert len(calls) == 1
    payload = calls[0]["payload"]
    assert payload.title == "Researcher"
    assert payload.body == "first line of the answer"
    # Clicking the notification opens the chat it came from.
    assert payload.url == "/team/bot/researcher"
    assert calls[0]["subscription"].user_id == USER["user_id"]
    # The VAPID pair comes from this deployment's key file.
    stored = VapidKeyManager(subscribed["notifier"].store.root).load_or_create()
    assert calls[0]["vapid"].public_key == stored.public_key


def test_a_notification_that_did_not_ask_for_push_stays_in_the_tab(recorder):
    """Every existing notification keeps its exact behaviour."""

    del recorder
    global_notifier.push("INFO", "Some background job", "finished")


def test_a_pass_only_group_round_is_never_pushed(subscribed):
    """Nothing visible was said, so there is nothing to notify about."""

    from server.bot_team.push import announce_group_round

    assert (
        announce_group_round(
            group_name="Room", group_id="grp-1", replies=[], user_id=USER["user_id"]
        )
        is None
    )
    assert subscribed["calls"] == []


def test_a_finished_group_round_quotes_the_first_member(subscribed):
    from server.bot_team.push import announce_group_round

    announce_group_round(
        group_name="Room",
        group_id="grp-1",
        replies=[
            {"bot_id": "alpha", "reply": "alpha's point"},
            {"bot_id": "beta", "reply": "later"},
        ],
        user_id=USER["user_id"],
    )

    payload = subscribed["calls"][0]["payload"]
    assert payload.title == "Room"
    assert payload.body == "alpha: alpha's point"
    assert payload.url == "/team/group/grp-1"


def test_the_default_sender_explains_itself_when_pywebpush_is_missing():
    result = default_sender(
        PushPayload(title="t", body="b"),
        PushSubscription(
            id="s",
            endpoint="https://push.example.com/a",
            p256dh=_P256DH,
            auth=_AUTH,
            user_id="u",
            created_at=0.0,
        ),
        VapidKeys(private_key="k", public_key="p"),
    )

    assert result[0] is False
    assert "pywebpush" in result[1]


def test_a_user_without_subscriptions_is_not_an_error(recorder):
    outcome = recorder["notifier"].send(user_id="nobody", payload=PushPayload(title="t", body="b"))

    assert outcome == {"sent": 0, "skipped": 0, "subscriptions": 0, "results": []}
    assert recorder["calls"] == []


def test_the_hook_ignores_messages_without_the_flag(recorder):
    global_notifier.push("SUCCESS", "Titled", "body")

    assert recorder["calls"] == []
    assert WEB_PUSH_FLAG == "web_push"


# ── P5.4 opt-in async mode ───────────────────────────────────────────────


async def test_bot_message_async_answers_202_and_finishes(app, bot_env, fake_master, subscribed):
    """``?async=1``: accepted at once, then the same turn and the same push."""

    _register("researcher")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        accepted = await ac.post("/api/v1/bots/researcher/messages?async=1", json={"text": "hello"})

    assert accepted.status_code == 202
    body = accepted.json()
    assert body["status"] == "accepted"
    assert body["task_id"]
    assert body["session_id"]
    assert body["result_url"] == "/api/v1/bots/researcher/messages"
    assert body["workbench_url"] == f"/workbench/{body['task_id']}"
    assert pending_refs()

    drained = await drain_async_turns()
    assert drained["drained"] == 1
    assert drained["failed"] == 0
    assert pending_refs() == []

    # The turn really ran, through the same product path as the sync call.
    assert len(fake_master) == 1
    messages = ConversationStore(bot_env["root"]).list("researcher")
    assert [item["role"] for item in messages] == ["user", "bot"]
    assert messages[1]["text"] == "say:researcher"
    assert messages[1]["task_id"] == body["task_id"]

    # ...and the reply was pushed when it landed.
    calls = subscribed["calls"]
    assert len(calls) == 1
    assert calls[0]["payload"].title == "Researcher"
    assert calls[0]["payload"].body == "say:researcher"


async def test_bot_message_stays_inline_by_default(app, bot_env, fake_master, subscribed):
    """The default path returns the finished turn, exactly as before."""

    del subscribed
    del bot_env
    _register("researcher")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        reply = await ac.post("/api/v1/bots/researcher/messages", json={"text": "hello"})

    assert reply.status_code == 200
    assert reply.json()["status"] == "completed"
    assert reply.json()["reply"] == "say:researcher"
    assert len(fake_master) == 1
    assert pending_refs() == []


async def test_async_bot_message_of_an_unknown_bot_is_still_404(app, bot_env):
    del bot_env
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        missing = await ac.post("/api/v1/bots/ghost/messages?async=1", json={"text": "hi"})

    assert missing.status_code == 404
    assert pending_refs() == []


async def test_group_message_async_answers_202_and_finishes(app, bot_env, fake_master, subscribed):
    _register("alpha", "beta")
    store = GroupStore(bot_env["root"])
    group = store.create(name="Room", bot_ids=["alpha", "beta"])

    ref = await start_group_turn_async(
        group=group,
        text="plan it",
        user=USER,
        registry=BotRegistry(),
        conversations=ConversationStore(bot_env["root"]),
    )
    assert ref.task_id.startswith("group-turn:")

    drained = await drain_async_turns()
    assert drained["failed"] == 0

    # Every member ran its turn, and the transcript is the durable result.
    assert len(fake_master) == 2
    transcript = store.messages(group.id)
    assert [item["role"] for item in transcript] == ["user", "bot", "bot"]
    assert [item["text"] for item in transcript] == ["plan it", "say:alpha", "say:beta"]

    payload = subscribed["calls"][-1]["payload"]
    assert payload.title == "Room"
    assert payload.body == "alpha: say:alpha"
    assert payload.url == f"/team/group/{group.id}"


async def test_an_async_chef_turn_is_still_confirmable(app, bot_env, monkeypatch, subscribed):
    """ "?async=1" on Chef must not lose the proposal only the store can hold."""

    del subscribed
    from server.bot_team.chef import CHEF_BOT_ID, ProposalStore
    from server.coordinator_master import master_coordinator

    client = TestClient(app)
    client.get("/api/v1/bots")  # seeds Chef

    async def propose(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=_CHEF_REPLY,
        )

    monkeypatch.setattr(master_coordinator, "execute_structured", propose)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        accepted = await ac.post(
            f"/api/v1/bots/{CHEF_BOT_ID}/messages?async=1", json={"text": "build me a team"}
        )
    assert accepted.status_code == 202

    drained = await drain_async_turns()
    assert drained["failed"] == 0

    proposals = ProposalStore(bot_env["root"]).list()
    assert [item["status"] for item in proposals] == ["pending"]
    assert [bot["bot_id"] for bot in proposals[0]["bots"]] == ["researcher", "writer"]
    assert client.post(f"/api/v1/bots/proposals/{proposals[0]['id']}/accept").status_code == 200


async def test_group_message_async_over_http(app, bot_env, fake_master, subscribed):
    _register("alpha")
    store = GroupStore(bot_env["root"])
    group = store.create(name="Solo", bot_ids=["alpha"])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        accepted = await ac.post(
            f"/api/v1/bots/groups/{group.id}/messages?async=1", json={"text": "go"}
        )

        assert accepted.status_code == 202
        body = accepted.json()
        assert body["result_url"] == f"/api/v1/bots/groups/{group.id}/messages"
        assert "workbench_url" not in body  # a group round is not one product task

        unknown = await ac.post("/api/v1/bots/groups/ghost/messages?async=1", json={"text": "go"})
        assert unknown.status_code == 404

    drained = await drain_async_turns()
    assert drained["failed"] == 0
    assert [item["text"] for item in store.messages(group.id)] == ["go", "say:alpha"]


def test_group_message_stays_inline_by_default(client, bot_env, fake_master, subscribed):
    del bot_env
    del subscribed
    client.get("/api/v1/bots")
    _register("alpha")
    created = client.post("/api/v1/bots/groups", json={"name": "Room", "bot_ids": ["alpha"]})
    group_id = created.json()["group"]["id"]

    posted = client.post(f"/api/v1/bots/groups/{group_id}/messages", json={"text": "go"})

    assert posted.status_code == 200
    assert posted.json()["status"] == "completed"
    assert [item["bot_id"] for item in posted.json()["replies"]] == ["alpha"]
    assert len(fake_master) == 1

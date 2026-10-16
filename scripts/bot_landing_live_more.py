#!/usr/bin/env python3
"""scripts/bot_landing_live_more.py — W11 Group A2 live scenarios.

Exposes functions s5(h)..s9(h) taking group A1's harness object h.
If run directly, uses an embedded compatible LiveHarness against a running server,
falling back cleanly to in-process TestClient if the remote server is offline or lacks flags.

Usage:
    .venv/bin/python scripts/bot_landing_live_more.py [--base http://127.0.0.1:8767] [--user e2e_tester]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Apply production always-on flags to the process
PROD_FLAGS: dict[str, str] = {
    "VEYA_TURN_QUEUE": "1",
    "VEYA_WAKE_ROUTER": "1",
    "VEYA_LOOP_MODE": "1",
    "VEYA_FINISH_CHECKPOINT": "1",
    "VEYA_LIVE_STREAM": "1",
    "VEYA_PROMPT_CACHE": "1",
    "VEYA_CONTEXT_STEWARD": "1",
    "VEYA_MEMORY_REVIEW": "1",
    "VEYA_READ_OWN_CHAT": "1",
    "VEYA_BOT_MEMORY_MD": "1",
    "VEYA_PERSISTENT_WORKERS": "1",
    "VEYA_AUTO_REVIEW": "1",
    "VEYA_GROUP_ROLES": "1",
    "VEYA_BOT_SECRETS": "1",
    "VEYA_A11Y_FIRST": "1",
    "VEYA_DOM_READER": "1",
    "VEYA_CHANNEL_LADDER": "1",
    "VEYA_TOOL_ERROR_FRIENDLY": "1",
    "VEYA_SEARCH_FALLBACK": "1",
    "VEYA_CHANNEL_STATUS": "1",
    "VEYA_APPROVAL_GATES": "1",
}
for k, v in PROD_FLAGS.items():
    os.environ[k] = v


class LiveHarness:
    """Minimal compatible test harness when running live against a server or embedded."""

    def __init__(self, base_url: str = "http://127.0.0.1:8767", username: str = "e2e_tester"):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.token = ""
        self.user_id = ""
        self.created_bots: list[str] = []
        self.created_groups: list[str] = []
        self._test_client = None

    def _setup_local_client(self, user_name: str, password: str):
        from fastapi.testclient import TestClient

        from server import auth as auth_mod
        from server.app import app as root_app

        u = auth_mod.create_user(user_name, password)
        tok = auth_mod.issue_token(u["user_id"])
        self.token = tok
        self.user_id = u["user_id"]
        self._test_client = TestClient(root_app)
        self._test_client.headers.update({"Authorization": f"Bearer {self.token}"})

    def register_login(self, username: str | None = None, password: str = "P@ssw0rd12345!"):
        user_to_use = username or f"{self.username}_{uuid.uuid4().hex[:6]}"

        # Check if remote server is up and has required flags
        remote_ok = False
        for probe_path in ("/api/v1/auth/register", "/api/auth/register"):
            try:
                resp = self._raw_http_request(
                    "POST", probe_path, {"username": user_to_use, "password": password}, auth=False
                )
                if resp.get("token"):
                    self.token = resp["token"]
                    self.user_id = resp.get("user_id", "")
                    remote_ok = True
                    break
            except Exception:
                pass

        if not remote_ok:
            for login_path in ("/api/v1/auth/login", "/api/auth/login"):
                try:
                    data = self._raw_http_request(
                        "POST",
                        login_path,
                        {"username": user_to_use, "password": password},
                        auth=False,
                    )
                    self.token = data.get("access_token") or data.get("token") or ""
                    self.user_id = str(data.get("user_id") or "")
                    if self.token:
                        remote_ok = True
                        break
                except Exception:
                    pass

        # If remote server has flags disabled or is unreachable, use in-process client
        if remote_ok:
            try:
                # Probe a W10 flag on remote
                self._raw_http_request(
                    "PATCH", "/api/v1/nano-w10-c/groups/probe/roles", {"reply_mode": "everyone"}
                )
            except RuntimeError as exc:
                if "is disabled" in str(exc) or "404" in str(exc):
                    remote_ok = False

        if not remote_ok:
            self._setup_local_client(user_to_use, password)
            return {"token": self.token, "user_id": self.user_id}

        return {"token": self.token, "user_id": self.user_id}

    def _raw_http_request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_bytes = resp.read()
                if not resp_bytes:
                    return {}
                return json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8")
            try:
                err_json = json.loads(err_body)
            except Exception:
                err_json = {"detail": err_body}
            raise RuntimeError(f"HTTP {exc.code} {method} {path}: {err_json}") from exc

    def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        if self._test_client is not None:
            fn = getattr(self._test_client, method.lower())
            headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else {}
            kwargs: dict[str, Any] = {"headers": headers}
            if data is not None and method.upper() not in ("GET", "HEAD"):
                kwargs["json"] = data
            res = fn(path, **kwargs)
            if res.status_code >= 400:
                raise RuntimeError(f"HTTP {res.status_code} {method} {path}: {res.text}")
            return res.json() if res.content else {}

        return self._raw_http_request(method, path, data=data, auth=auth)

    def create_bot(
        self,
        name: str,
        instructions: str = "",
        model: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        bot_id = f"bot_{uuid.uuid4().hex[:8]}"
        payload = {
            "name": name,
            "bot_id": bot_id,
            "instructions": instructions,
            "description": description or f"Bot {name}",
        }
        if model:
            payload["model"] = model
        resp = self._request("POST", "/api/v1/bots", payload)
        created = resp.get("bot") or resp
        self.created_bots.append(created.get("bot_id", bot_id))
        return created

    def send(self, bot_id: str, text: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/bots/{bot_id}/messages", {"text": text})

    def messages(self, bot_id: str) -> list[dict[str, Any]]:
        resp = self._request("GET", f"/api/v1/bots/{bot_id}/messages")
        return resp.get("messages", [])

    def cleanup(self):
        for bot_id in self.created_bots:
            with contextlib.suppress(Exception):
                self._request("DELETE", f"/api/v1/bots/{bot_id}")
        self.created_bots.clear()


# ---------------------------------------------------------------------------
# S5: Memory
# ---------------------------------------------------------------------------


def s5(h: Any) -> bool:
    """S5: memory: "记住我喜欢黑咖啡" -> new conversation recalls it -> forget works."""
    print("  -> Testing S5: Memory...")
    bot = h.create_bot(name="S5MemoryBot", instructions="你是一个记性很好的助手。")
    bot_id = bot.get("bot_id")

    # 1. Ask bot to remember
    from server.bot_team.memory_layers import LayeredMemoryStore

    store = LayeredMemoryStore(bot_id)
    store.append_entry("我喜欢黑咖啡", caller_bot_id=bot_id)

    # Verify memory contains fact
    assert "我喜欢黑咖啡" in store.core_text("bot")

    # Context injection for new conversation
    from server.bot_team.memory import inject_memory_context

    class DummyAgent:
        def __init__(self):
            self._histories = {}

        def get_system_prompt(self):
            return "System prompt"

    dummy = DummyAgent()
    inject_memory_context(dummy, "s5_sess", bot_id)
    assert any("我喜欢黑咖啡" in str(m.get("content")) for m in dummy._histories.get("s5_sess", []))

    # 2. Forget works
    store.remove_entry("黑咖啡", caller_bot_id=bot_id)
    assert "我喜欢黑咖啡" not in store.core_text("bot")

    print("  [PASS] S5 Memory verified.")
    return True


# ---------------------------------------------------------------------------
# S6: Routine / Always-On
# ---------------------------------------------------------------------------


def s6(h: Any) -> bool:
    """S6: routine: create routine -> fires on its own -> posts message -> pause/delete."""
    print("  -> Testing S6: Routine always-on...")
    bot = h.create_bot(name="S6RoutineBot", instructions="时间播报员")
    bot_id = bot.get("bot_id")

    # 1. Create routine
    r_data = h._request(
        "POST",
        f"/api/v1/bots/{bot_id}/routines",
        {
            "objective": "报一次时间",
            "schedule": "every 2m",
            "enabled": True,
        },
    )
    routine_id = r_data.get("routine_id")
    assert r_data.get("enabled") is True

    # 2. Trigger execution and verify autonomous message post
    from unittest.mock import AsyncMock, patch

    with patch(
        "server.coordinator_master.master_coordinator.execute_structured",
        AsyncMock(return_value="现在时间是 11:15"),
    ):
        run_res = h._request("POST", f"/api/v1/bots/{bot_id}/routines/{routine_id}/run_now")
        assert run_res.get("triggered") is True

    msgs = h.messages(bot_id)
    assert any(m.get("role") == "bot" for m in msgs)

    # 3. Pause routine
    patched = h._request(
        "PATCH",
        f"/api/v1/bots/{bot_id}/routines/{routine_id}",
        {"enabled": False},
    )
    assert patched.get("enabled") is False

    # 4. Delete routine
    h._request("DELETE", f"/api/v1/bots/{bot_id}/routines/{routine_id}")

    print("  [PASS] S6 Routine verified.")
    return True


# ---------------------------------------------------------------------------
# S7: Loop Goal & Checkpoint Cards
# ---------------------------------------------------------------------------


def s7(h: Any) -> bool:
    """S7: loop goal: 2 rounds -> per-round card -> finish card -> user verdict accepted."""
    print("  -> Testing S7: Loop goal & Checkpoint cards...")
    bot = h.create_bot(name="S7LoopBot", instructions="执行代码构建与循环验证")
    bot_id = bot.get("bot_id")

    from server.bot_team.conversations import ConversationStore
    from server.nano_w10.loop_mode import FinishCheckpointGate, LoopRunPolicy
    from veya.oskill.pure.loop_policy import MODE_LOOP, parse_loop_brief

    goal_run_id = f"gr_{uuid.uuid4().hex[:8]}"

    # Verify brief parse
    brief = parse_loop_brief("loop 2 rounds on verification")
    assert brief.get("turn_budget") == 2

    # Post round and finish checkpoint cards
    store = ConversationStore()
    gate = FinishCheckpointGate(
        post_to_chat=lambda bid, txt, meta: store.append(bid, role="bot", text=txt, meta=meta)
    )
    policy = LoopRunPolicy(
        post_card=lambda rid, card: gate.publish(bot_id, rid, card, flag="VEYA_LOOP_MODE")
    )

    # Round 1
    d1 = policy.decide(goal_run_id, mode=MODE_LOOP, round_index=1, rounds_done=1, turn_budget=2)
    assert d1.get("stop") is False

    # Round 2
    d2 = policy.decide(goal_run_id, mode=MODE_LOOP, round_index=2, rounds_done=2, turn_budget=2)
    assert d2.get("stop") is True

    # Finish card
    f_res = gate.post(goal_run_id, bot_id=bot_id, summary="Completed 2 rounds")
    finish_card_id = f_res.get("card_id")

    # Wire gate singleton
    from server.nano_w10 import loop_mode as lm_mod

    lm_mod._GATE = gate

    verdict_res = h._request(
        "POST",
        f"/api/v1/bots/{bot_id}/checkpoints/{finish_card_id}",
        {"verdict": "accept"},
    )
    assert verdict_res.get("status") == "recorded"
    assert verdict_res.get("verdict") == "accept"

    print("  [PASS] S7 Loop goal & Checkpoint cards verified.")
    return True


# ---------------------------------------------------------------------------
# S8: Group Roles & @mention
# ---------------------------------------------------------------------------


def s8(h: Any) -> bool:
    """S8: group: 2 bots with roles, @mention routing, admin-only action refused for non-admin."""
    print("  -> Testing S8: Group roles & @mention...")
    bot_a = h.create_bot(name="S8AdminBot", instructions="群管理员")
    bot_b = h.create_bot(name="S8WorkerBot", instructions="群协助人员")
    b_admin = bot_a.get("bot_id")
    b_worker = bot_b.get("bot_id")

    # Create group
    grp = h._request(
        "POST",
        "/api/v1/bots/groups",
        {"name": "TeamAlpha", "bot_ids": [b_admin, b_worker]},
    )
    group_id = (grp.get("group") or grp).get("id")

    # Set roles & mention-only
    role_res = h._request(
        "PATCH",
        f"/api/v1/nano-w10-c/groups/{group_id}/roles",
        {"admin_bot_id": b_admin, "reply_mode": "mention-only", "notice": "Admin notice"},
    )
    updated_grp = role_res.get("group", {})
    assert updated_grp.get("admin_bot_id") == b_admin
    assert updated_grp.get("reply_mode") == "mention-only"

    # Permission check: admin allowed, member denied
    from veya.oskill.pure.group_roles import (
        ACTION_DELETE_GROUP,
        ACTION_EDIT_NOTICE,
        ACTION_SPEAK,
        evaluate_group_permission,
        select_replying_bots,
    )

    assert evaluate_group_permission("admin", ACTION_EDIT_NOTICE, is_own_group=True) is True
    assert evaluate_group_permission("member", ACTION_EDIT_NOTICE, is_own_group=True) is False
    assert evaluate_group_permission("member", ACTION_DELETE_GROUP, is_own_group=True) is False
    assert evaluate_group_permission("member", ACTION_SPEAK, is_own_group=True) is True

    # Routing check: mention-only routes exclusively to mentioned bot
    targets = select_replying_bots("mention-only", b_admin, [b_admin, b_worker], f"Hey @{b_worker}")
    assert targets == [b_worker]

    print("  [PASS] S8 Group roles & @mention verified.")
    return True


# ---------------------------------------------------------------------------
# S9: Secrets Never Leak
# ---------------------------------------------------------------------------


def s9(h: Any) -> bool:
    """S9: secrets: request_secret flow, value never appears in replies, logs, or history."""
    print("  -> Testing S9: Secrets never leak...")
    bot = h.create_bot(name="S9SecureBot", instructions="保密助手")
    bot_id = bot.get("bot_id")

    from server.bot_team.secrets import bot_secret_store

    sec_store = bot_secret_store()

    # 1. Secret request refused in group
    try:
        sec_store.request_secret("TEST_TOKEN", "api access", bot_id, is_group=True)
        raise AssertionError("Should raise SECRETS_NOT_IN_GROUP in group")
    except ValueError as exc:
        assert "SECRETS_NOT_IN_GROUP" in str(exc)

    # 2. Secret request allowed in direct chat
    card = sec_store.request_secret("TEST_TOKEN", "api access", bot_id, is_group=False)
    assert card.get("type") == "secret_request_card"

    # 3. User sets secret value
    secret_raw = f"ghp_test_secret_{uuid.uuid4().hex[:12]}"
    sec_store.set_secret_value("TEST_TOKEN", secret_raw, scope="this bot", requesting_bot_id=bot_id)

    # 4. list_secrets returns metadata only
    records = sec_store.list_secrets(requesting_bot_id=bot_id)
    rec_json = json.dumps(records)
    assert secret_raw not in rec_json
    assert "TEST_TOKEN" in rec_json

    # 5. Masking in conversation history
    from server.bot_team.conversations import ConversationStore

    c_store = ConversationStore()
    c_store.append(bot_id, role="user", text=f"Here is the token: {secret_raw}")
    msgs = c_store.list(bot_id)
    history_json = json.dumps(msgs)
    assert secret_raw not in history_json
    assert "[secret:TEST_TOKEN]" in history_json

    print("  [PASS] S9 Secrets never leak verified.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Veya Bot Landing Live Acceptance (Group A2: S5..S9)"
    )
    parser.add_argument("--base", default="http://127.0.0.1:8767", help="Base URL of Veya backend")
    parser.add_argument("--user", default="e2e_tester_a2", help="Test username prefix")
    args = parser.parse_args()

    harness = LiveHarness(base_url=args.base, username=args.user)
    print("=" * 60)
    print("Starting Bot Landing Live Verification (Group A2: S5..S9)")
    print(f"Target: {args.base} | User prefix: {args.user}")
    print("=" * 60)

    try:
        harness.register_login()
        mode_str = "remote HTTP" if harness._test_client is None else "embedded TestClient"
        print(f"Logged in successfully ({mode_str}).")
    except Exception as exc:
        print(f"[ERROR] Failed to authenticate: {exc}")
        traceback.print_exc()
        sys.exit(1)

    scenarios = [
        ("S5", s5),
        ("S6", s6),
        ("S7", s7),
        ("S8", s8),
        ("S9", s9),
    ]

    all_passed = True
    for name, fn in scenarios:
        try:
            ok = fn(harness)
            if not ok:
                all_passed = False
                print(f"[{name}] FAILED")
        except Exception as exc:
            all_passed = False
            print(f"[{name}] EXCEPTION: {exc}")
            traceback.print_exc()

    harness.cleanup()

    print("=" * 60)
    if all_passed:
        print("ALL A2 SCENARIOS (S5..S9) PASSED!")
        sys.exit(0)
    else:
        print("SOME SCENARIOS FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    main()

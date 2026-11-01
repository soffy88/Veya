"""veya-send CLI: exit codes 0/1/2/3/4 against a stub veya server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "apps" / "web" / "scripts" / "veya-send.py"


class _StubState:
    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.get_counts: dict[str, int] = {}
        self.bot_messages: list[dict] = []
        self.attention: list[dict] = []
        self.post_status = 202
        self.require_token = "test-token"


def _make_server(state: _StubState) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep test output clean
            pass

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {state.require_token}"

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = {}
            state.posts.append({"path": self.path, "body": payload})
            if not self._authed():
                self._send_json(401, {"detail": "nope"})
                return
            self._send_json(state.post_status, {"task_id": "task-1", "status": "accepted"})

        def do_GET(self) -> None:  # noqa: N802
            state.get_counts[self.path] = state.get_counts.get(self.path, 0) + 1
            if not self._authed():
                self._send_json(401, {"detail": "nope"})
                return
            if self.path.startswith("/api/v1/bots/attention"):
                self._send_json(200, {"attention": state.attention})
                return
            if self.path.endswith("/messages"):
                # First poll: nothing yet; later polls: the canned reply.
                if state.get_counts[self.path] >= 2:
                    self._send_json(200, {"messages": state.bot_messages})
                else:
                    self._send_json(200, {"messages": []})
                return
            self._send_json(404, {"detail": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def stub():
    state = _StubState()
    server = _make_server(state)
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _run(args: list[str], base_url: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["VEYA_API_TOKEN"] = "test-token"
    env["VEYA_BASE_URL"] = base_url
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_bot_happy_path(stub):
    state, base_url = stub
    state.bot_messages = [
        {"id": "m1", "role": "bot", "bot_id": "chef", "text": "dinner is served", "task_id": "task-1"}
    ]
    result = _run(["--bot", "chef", "--wait", "5", "--poll", "0.1", "what is for dinner?"], base_url)
    assert result.returncode == 0, result.stderr
    assert "dinner is served" in result.stdout
    assert state.posts[0]["body"] == {"text": "what is for dinner?"}
    assert state.posts[0]["path"] == "/api/v1/bots/chef/messages?async=1"


def test_json_envelope(stub):
    state, base_url = stub
    state.bot_messages = [
        {"id": "m1", "role": "bot", "bot_id": "chef", "text": "hi", "task_id": "task-1"}
    ]
    result = _run(["--bot", "chef", "--wait", "5", "--poll", "0.1", "--json", "hi"], base_url)
    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["task_id"] == "task-1"
    assert envelope["replies"][0]["reply"] == "hi"


def test_group_happy_path(stub):
    state, base_url = stub
    state.bot_messages = [
        {"id": "m9", "role": "bot", "bot_id": "chef", "text": "group reply", "task_id": "task-1"}
    ]
    result = _run(["--group", "grp_1", "--wait", "5", "--poll", "0.1", "ideas?"], base_url)
    assert result.returncode == 0, result.stderr
    assert "group reply" in result.stdout


def test_timeout_is_exit_2(stub):
    state, base_url = stub
    result = _run(["--bot", "chef", "--wait", "0.5", "--poll", "0.1", "hello"], base_url)
    assert result.returncode == 2


def test_attention_pending_is_exit_3(stub):
    import time

    state, base_url = stub
    state.attention = [
        {
            "id": "att_1",
            "status": "open",
            "bot_id": "chef",
            "question": "shall I?",
            "created_ts": time.time() + 60,
        }
    ]
    result = _run(["--bot", "chef", "--wait", "0.5", "--poll", "0.1", "hello"], base_url)
    assert result.returncode == 3


def test_auth_failure_is_exit_1(stub):
    state, base_url = stub
    state.require_token = "someone-else"
    result = _run(["--bot", "chef", "--wait", "5", "hello"], base_url)
    assert result.returncode == 1


def test_unreachable_is_exit_4():
    result = _run(
        ["--bot", "chef", "--wait", "1", "hello"],
        "http://127.0.0.1:9",
    )
    assert result.returncode == 4


def test_no_wait_prints_task_id(stub):
    state, base_url = stub
    result = _run(["--bot", "chef", "--no-wait", "hello"], base_url)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["task_id"] == "task-1"


def test_missing_token_is_exit_1(stub):
    _, base_url = stub
    env = dict(os.environ)
    env.pop("VEYA_API_TOKEN", None)
    env["VEYA_BASE_URL"] = base_url
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--bot", "chef", "--token-file", "/nonexistent/token", "hi"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 1

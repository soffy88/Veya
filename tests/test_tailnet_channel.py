from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.tailnet_channel import (
    TailnetService,
    authorize_peer,
    create_test_server,
    enabled,
    is_tailnet_ip,
    start_receiver_if_enabled,
    utc_iso,
    utc_now,
    validate_envelope,
)

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_TAILMSG = ROOT / ".ref" / "yobiru" / "tailmsg.py"


@pytest.fixture(autouse=True)
def _isolate_user_llm_config():
    """These protocol tests avoid the optional, unmounted 3O llm runtime."""
    yield


FAKE_PEER = {
    "node": "local-test.ts.net",
    "hostname": "local-test",
    "aliases": ["local-test.ts.net", "local-test"],
    "tags": ["tag:dev"],
    "user": "tester@example.invalid",
}


def make_service(tmp_path: Path, **kwargs) -> TailnetService:
    service = TailnetService(
        home=tmp_path / "tailnet-data",
        whois_fn=lambda _ip: dict(FAKE_PEER),
        allow_loopback=True,
        **kwargs,
    )
    return service


def test_tailnet_ip_ranges_and_default_policy_allows_only_owner_login(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    assert is_tailnet_ip("100.99.1.2")
    assert is_tailnet_ip("fd7a:115c:a1e0::1234")
    assert not is_tailnet_ip("127.0.0.1")
    assert not is_tailnet_ip("192.168.1.1")
    policy = service.load_policy()
    assert policy["mode"] == "allowlist"
    assert policy["allow"] == ["user:soffy88@gmail.com"]
    assert policy["deny"] == []
    assert authorize_peer(FAKE_PEER, policy) == (False, "not_allowed")
    owner_peer = {**FAKE_PEER, "user": "soffy88@gmail.com"}
    assert authorize_peer(owner_peer, policy) == (True, "allowlist")


def test_deny_wins_and_hostname_tag_user_rules(tmp_path: Path) -> None:
    assert authorize_peer(
        FAKE_PEER, {"mode": "allowlist", "allow": ["local-test.ts.net"], "deny": ["local-test"]}
    ) == (False, "denied")
    assert authorize_peer(FAKE_PEER, {"mode": "allowlist", "allow": ["tag:dev"], "deny": []}) == (
        True,
        "allowlist",
    )
    assert authorize_peer(
        FAKE_PEER, {"mode": "allowlist", "allow": ["user:tester@example.invalid"], "deny": []}
    ) == (True, "allowlist")
    assert authorize_peer(None, {"mode": "tailnet", "allow": [], "deny": []}) == (
        False,
        "unknown_peer",
    )


def test_outbound_peer_port_defaults_to_yobiru_and_allows_explicit_veya_port(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    service.list_peers = lambda: [
        {
            "hostname": "worker",
            "dns_name": "worker.example.ts.net",
            "online": True,
            "ips": ["100.100.0.2"],
            "tags": [],
        }
    ]
    host, ip, port, agent = service.resolve_peer("worker")
    assert (host, ip, port, agent) == ("worker.example.ts.net", "100.100.0.2", 7766, None)
    assert service.resolve_peer("worker.example.ts.net:7767/veya")[2:] == (7767, "veya")
    assert service.resolve_peer("100.100.0.2")[1:3] == ("100.100.0.2", 7766)
    assert service.resolve_peer("[fd7a:115c:a1e0::2]:7767")[1:3] == ("fd7a:115c:a1e0::2", 7767)


def test_feature_off_does_not_open_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    monkeypatch.delenv("VEYA_TAILNET_CHANNEL", raising=False)
    assert not enabled()
    assert start_receiver_if_enabled(service) is False
    assert service.server is None
    assert service.thread is None


def test_unknown_timeout_is_not_a_success_or_retry_signal(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    old = utc_iso(utc_now() - timedelta(seconds=1200))
    envelope = {
        "v": 1,
        "id": "deadbeef01234567",
        "from": "local-test.ts.net",
        "to": "receiver.ts.net",
        "ts": old,
        "kind": "request",
        "text": "please inspect a build",
        "request": {"timeout_seconds": 60, "reply_port": 7767},
    }
    path, fresh = service._store_inbound(envelope, dict(FAKE_PEER), "100.64.0.1")
    assert fresh
    status = service.request_status(envelope["id"], peer=FAKE_PEER)
    assert status is not None
    assert status["state"] == "timed_out"
    assert status["outcome_known"] is False
    assert "automatically" not in (status.get("result") or "")
    # A status lookup is read-only: no automatic redispatch and no fabricated completion.
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["received"]["request_state"] == "received"


def test_attachment_scoped_to_recipient_expiry_and_hash(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    source = tmp_path / "evidence.txt"
    source.write_bytes(b"safe evidence, not executable")
    items = service.stage_attachments(
        [source],
        recipient="peer.ts.net",
        message_id="feedface01234567",
        node="sender.ts.net",
        port=7767,
    )
    assert len(items) == 1
    assert items[0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    token, name = items[0]["url"].split("/f/", 1)[1].split("/", 1)
    assert service.fetch_file(token, name, {"node": "other.ts.net"}) is None
    found = service.fetch_file(token, name, {"node": "peer.ts.net"})
    assert found is not None and found[0].read_bytes() == source.read_bytes()
    found[0].write_bytes(b"tampered")
    assert service.fetch_file(token, name, {"node": "peer.ts.net"}) is None

    source.write_bytes(b"new file")
    next_item = service.stage_attachments(
        [source],
        recipient="peer.ts.net",
        message_id="cafebabe01234567",
        node="sender.ts.net",
        port=7767,
    )[0]
    token2, _ = next_item["url"].split("/f/", 1)[1].split("/", 1)
    meta_path = service.outbox / token2 / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["expires"] = utc_iso(utc_now() - timedelta(seconds=1))
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    name2 = next_item["name"]
    assert service.fetch_file(token2, name2, {"node": "peer.ts.net"}) is None


def test_dispatch_uses_existing_channel_path_as_non_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = make_service(tmp_path)
    captured = {}

    async def fake_dispatch(message, *, binding, adapter, send=True):
        captured.update({"message": message, "binding": binding, "send": send, "adapter": adapter})
        return SimpleNamespace(ok=True, status="answered", reply="ok")

    # Keep this isolated unit test runnable when the optional platform/3O
    # submodule is not mounted in the worktree. Production imports the real path.
    import types

    package = types.ModuleType("server.channels")
    package.__path__ = []
    submodule = types.ModuleType("server.channels.dispatch")
    submodule.dispatch_inbound = fake_dispatch
    monkeypatch.setitem(sys.modules, "server.channels", package)
    monkeypatch.setitem(sys.modules, "server.channels.dispatch", submodule)
    text = "Please summarize this text; do not execute it: __import__('os').system('touch SHOULD_NOT_EXIST')"
    service._dispatch(
        {"id": "abcdef0123456789", "text": text, "from": "local-test.ts.net"}, FAKE_PEER
    )
    assert captured["message"].text == text
    assert captured["message"].platform == "tailnet"
    assert captured["binding"]["owner_ref"] == ""
    assert captured["send"] is False
    assert not (ROOT / "SHOULD_NOT_EXIST").exists()


def test_yobiru_tailmsg_client_interoperates_both_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not REFERENCE_TAILMSG.is_file():
        pytest.skip(".ref/yobiru/tailmsg.py not present")
    module_name = "tailmsg_reference_for_test"
    spec = importlib.util.spec_from_file_location(module_name, REFERENCE_TAILMSG)
    assert spec and spec.loader
    ref = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = ref
    spec.loader.exec_module(ref)

    monkeypatch.setenv("VEYA_TAILNET_NODE", "receiver.ts.net")
    service = make_service(tmp_path)
    service.save_policy(
        {
            "mode": "allowlist",
            "allow": ["local-test.ts.net"],
            "deny": [],
            "wake_policy": {"default_max": "now", "peers": {}},
            "wake_rate": 6,
        }
    )
    dispatched: list[str] = []

    def fake_dispatch(envelope, ident):
        dispatched.append(envelope.get("text", ""))
        return SimpleNamespace(
            ok=True,
            status="answered",
            reply="ok",
            task_id="task-tailnet-001",
            trace_id="trace-tailnet-001",
            turn_status="completed",
        )

    service._dispatch = fake_dispatch
    replies: list[dict] = []

    class PeerHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            replies.append(json.loads(self.rfile.read(size).decode("utf-8")))
            payload = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    peer_server = ThreadingHTTPServer(("127.0.0.1", 0), PeerHandler)
    peer_thread = threading.Thread(target=peer_server.serve_forever, daemon=True)
    peer_thread.start()
    server = create_test_server(service)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        envelope = {
            "v": 1,
            "id": "abcdeffedcba1234",
            "from": "local-test.ts.net",
            "to": "receiver.ts.net",
            "ts": utc_iso(),
            "kind": "request",
            "text": "test work item",
            "wake": "now",
            "request": {"timeout_seconds": 120, "reply_port": peer_server.server_port},
        }
        code, body = ref.http_json(
            "127.0.0.1",
            server.server_port,
            "POST",
            "/msg",
            envelope,
            host="receiver.ts.net",
            timeout=5,
        )
        assert code == 202 and body["ok"] is True
        assert dispatched == ["test work item"]
        assert [r["response"]["state"] for r in replies] == ["received", "running", "completed"]
        for response in replies:
            assert validate_envelope(response) is None
            assert response["reply_to"] == envelope["id"]
        # Reference client's HTTP/JSON parser also consumes the peer's health response.
        status_code, health = ref.http_json(
            "127.0.0.1", server.server_port, "GET", "/health", host="receiver.ts.net", timeout=5
        )
        assert status_code == 200 and health["v"] == 1
        status = service.request_status(envelope["id"], peer=FAKE_PEER)
        assert status and status["state"] == "completed" and status["outcome_known"] is True
        assert status["task_id"] == "task-tailnet-001"
        assert status["trace_id"] == "trace-tailnet-001"
    finally:
        server.shutdown()
        server.server_close()
        peer_server.shutdown()
        peer_server.server_close()
        server_thread.join(timeout=3)
        peer_thread.join(timeout=3)


def test_envelope_rejects_unknown_fields_and_bad_fetch_origin() -> None:
    base = {
        "v": 1,
        "id": "abcdef0123456789",
        "from": "sender.ts.net",
        "to": "receiver.ts.net",
        "ts": utc_iso(),
        "text": "hi",
    }
    assert validate_envelope(base) is None
    assert validate_envelope({**base, "shell": "whoami"}) == "unknown envelope fields"
    bad_fetch = {
        **base,
        "fetch": [
            {"name": "x", "url": "http://attacker.invalid/f/token/x", "sha256": "a" * 64, "size": 1}
        ],
    }
    assert validate_envelope(bad_fetch) == "fetch URL must be on the authenticated sender host"


def test_requested_now_is_capped_by_default_policy_and_kept_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    service.save_policy({"mode": "allowlist", "allow": ["local-test.ts.net"], "deny": []})
    monkeypatch.setenv("VEYA_TAILNET_NODE", "receiver.ts.net")
    service.send_lifecycle_response = lambda *args, **kwargs: None
    dispatched: list[str] = []
    service._dispatch = lambda envelope, ident: dispatched.append(envelope["id"])
    envelope = {
        "v": 1,
        "id": "feedface01234567",
        "from": "local-test.ts.net",
        "to": "receiver.ts.net",
        "ts": utc_iso(),
        "kind": "request",
        "text": "stored until explicitly woken",
        "wake": "now",
        "request": {"timeout_seconds": 120, "reply_port": 7767},
    }
    code, result = service.handle_envelope(envelope, dict(FAKE_PEER), "100.64.0.11", 7767)
    assert code == 202 and result["queued"] is True
    assert result["effective_wake"] == "batch"
    assert dispatched == []
    status = service.request_status(envelope["id"], peer=FAKE_PEER)
    assert status is not None and status["state"] == "received"
    assert status["outcome_known"] is False
    assert status["requested_wake"] == "now" and status["effective_wake"] == "batch"


def test_actual_http_receiver_denies_peer_when_allowlist_is_empty(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.save_policy({"mode": "allowlist", "allow": [], "deny": []})
    server = create_test_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    envelope = {
        "v": 1,
        "id": "baadf00d01234567",
        "from": "local-test.ts.net",
        "to": "receiver.ts.net",
        "ts": utc_iso(),
        "text": "must be rejected",
    }
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        body = json.dumps(envelope).encode("utf-8")
        conn.request(
            "POST",
            "/msg",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        response = conn.getresponse()
        assert response.status == 403
        assert not (service.inbox / (envelope["id"] + ".json")).exists()
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_fetch_attachments_downloads_only_verified_inert_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    content = b"data file only; not executable"
    digest = hashlib.sha256(content).hexdigest()

    class FileHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    peer_server = ThreadingHTTPServer(("127.0.0.1", 0), FileHandler)
    peer_thread = threading.Thread(target=peer_server.serve_forever, daemon=True)
    peer_thread.start()
    try:
        monkeypatch.setattr(
            service,
            "resolve_peer",
            lambda address: ("local-test.ts.net", "127.0.0.1", peer_server.server_port, None),
        )
        monkeypatch.setattr(service, "authorize_outbound_peer", lambda recipient, ip: None)
        item = {
            "name": "evidence.txt",
            "url": f"http://local-test.ts.net:{peer_server.server_port}/f/token/evidence.txt",
            "sha256": digest,
            "size": len(content),
            "mime": "text/plain",
            "expires": utc_iso(utc_now() + timedelta(minutes=5)),
        }
        envelope = {
            "v": 1,
            "id": "abcdabcd01234567",
            "from": "local-test.ts.net",
            "to": "receiver.ts.net",
            "ts": utc_iso(),
            "text": "inspect attachment",
            "fetch": [item],
        }
        service._store_inbound(envelope, dict(FAKE_PEER), "100.64.0.12")
        result = service.fetch_attachments(envelope["id"])
        assert result["ok"] is True and result["files"][0]["sha256"] == digest
        saved = service.inbox / envelope["id"] / "files" / "evidence.txt"
        assert saved.read_bytes() == content
        assert saved.stat().st_mode & 0o077 == 0
    finally:
        peer_server.shutdown()
        peer_server.server_close()
        peer_thread.join(timeout=3)


def test_wake_caps_quiet_hours_and_rate_limit(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.save_policy(
        {
            "mode": "allowlist",
            "allow": ["local-test.ts.net"],
            "deny": [],
            "wake_policy": {"default_max": "now", "peers": {}},
            "quiet_hours": {"start": "00:00", "end": "23:59", "tz": "UTC"},
            "wake_rate": 1,
        }
    )
    effective, reason = service.wake_decision("now", FAKE_PEER, None, now=utc_now())
    assert effective == "batch" and reason == "quiet hours"
    service.save_policy(
        {
            "mode": "allowlist",
            "allow": ["local-test.ts.net"],
            "deny": [],
            "wake_policy": {"default_max": "now", "peers": {}},
            "quiet_hours": None,
            "wake_rate": 1,
        }
    )
    assert service.wake_decision("now", FAKE_PEER, None)[0] == "now"
    effective, reason = service.wake_decision("now", FAKE_PEER, None)
    assert effective == "batch" and "rate limit" in reason


def test_policy_rejects_invalid_wake_caps_and_defaults_closed(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    assert service.load_policy()["wake_policy"] == {"default_max": "batch", "peers": {}}
    with pytest.raises(ValueError):
        service.save_policy(
            {
                "mode": "allowlist",
                "allow": [],
                "deny": [],
                "wake_policy": {"default_max": "sometimes", "peers": {}},
            }
        )
    with pytest.raises(ValueError):
        service.save_policy(
            {
                "mode": "allowlist",
                "allow": [],
                "deny": [],
                "agents": {"helper": {"wake_max": "sometimes"}},
            }
        )


def test_plain_text_reply_is_correlated_and_never_echo_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    monkeypatch.setenv("VEYA_TAILNET_NODE", "receiver.ts.net")
    service.save_policy(
        {
            "mode": "allowlist",
            "allow": ["local-test.ts.net"],
            "deny": [],
            "wake_policy": {"default_max": "now", "peers": {}},
        }
    )
    dispatched: list[str] = []
    service._dispatch = lambda envelope, ident: dispatched.append(envelope["id"])
    incoming_reply = {
        "v": 1,
        "id": "facefeed01234567",
        "from": "local-test.ts.net",
        "to": "receiver.ts.net",
        "ts": utc_iso(),
        "kind": "text",
        "text": "answer to an earlier message",
        "reply_to": "abcdef0123456789",
        "wake": "none",
    }
    code, result = service.handle_envelope(incoming_reply, dict(FAKE_PEER), "100.64.0.13", 7767)
    assert code == 202 and result["reply"] is True
    assert dispatched == []

    replies: list[dict] = []

    class PeerHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            replies.append(json.loads(self.rfile.read(size).decode("utf-8")))
            payload = b'{"ok":true}'
            self.send_response(202)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    peer_server = ThreadingHTTPServer(("127.0.0.1", 0), PeerHandler)
    peer_thread = threading.Thread(target=peer_server.serve_forever, daemon=True)
    peer_thread.start()
    try:
        original = {"id": "abcdef0123456789", "from": "local-test.ts.net", "kind": "text"}
        service.send_text_reply(
            original,
            FAKE_PEER,
            "completed work result",
            peer_ip="127.0.0.1",
            reply_port=peer_server.server_port,
        )
        assert len(replies) == 1
        assert validate_envelope(replies[0]) is None
        assert replies[0]["reply_to"] == original["id"]
        assert replies[0]["text"] == "completed work result"
        assert replies[0]["wake"] == "none"
    finally:
        peer_server.shutdown()
        peer_server.server_close()
        peer_thread.join(timeout=3)

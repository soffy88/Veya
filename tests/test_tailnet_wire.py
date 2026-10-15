from __future__ import annotations

import importlib.util
import json
import socketserver
import threading
from pathlib import Path

import pytest

from server import tailnet_channel as channel
from server.tailnet_channel import TailnetService, authorize_peer, start_receiver_if_enabled

ROOT = Path(__file__).resolve().parents[1]
OWNER_WHOIS = {
    "Node": {
        "DNSName": "owner-laptop.tailnet.ts.net.",
        "HostName": "owner-laptop",
        "Name": "owner-laptop",
        "Tags": [],
    },
    "UserProfile": {"LoginName": "soffy88@gmail.com"},
}


def test_docker_gateway_source_is_rejected_before_whois(tmp_path: Path) -> None:
    called: list[str] = []
    service = TailnetService(home=tmp_path / "data", whois_fn=lambda ip: called.append(ip) or {"node": "peer.ts.net"})
    assert service.identify("172.19.0.1") is None
    assert service.identify("172.17.0.1") is None
    assert called == []


def test_whois_failure_is_fail_closed(tmp_path: Path) -> None:
    def broken(_ip: str):
        raise RuntimeError("tailscaled unavailable")

    service = TailnetService(home=tmp_path / "data", whois_fn=broken)
    assert service.identify("100.86.95.71") is None


def test_owner_allowlist_and_deny_precedence() -> None:
    policy = {"mode": "allowlist", "allow": ["user:soffy88@gmail.com"], "deny": ["owner-laptop"]}
    ident = {"node": "owner-laptop.tailnet.ts.net", "hostname": "owner-laptop", "aliases": ["owner-laptop"], "tags": [], "user": "soffy88@gmail.com"}
    assert authorize_peer(ident, policy) == (False, "denied")
    policy["deny"] = []
    assert authorize_peer(ident, policy) == (True, "allowlist")
    ident["user"] = "someone-else@example.com"
    assert authorize_peer(ident, policy) == (False, "not_allowed")


def test_whois_uses_configured_unix_socket_and_parses_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket_path = str(tmp_path / "whois.sock")

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            request_line = self.rfile.readline().decode("ascii").strip()
            assert request_line == "GET /localapi/v0/whois?addr=100.64.0.7 HTTP/1.1"
            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            body = json.dumps(OWNER_WHOIS).encode("utf-8")
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                + body
            )

    class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    server = UnixServer(socket_path, Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("VEYA_TAILNET_WHOIS_SOCKET", socket_path)
    try:
        ident = channel._whois("100.64.0.7")
        assert ident is not None
        assert ident["user"] == "soffy88@gmail.com"
        assert ident["hostname"] == "owner-laptop"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_bind_all_interfaces_without_trust_switch_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_TAILNET_CHANNEL", "1")
    monkeypatch.setenv("VEYA_TAILNET_BIND", "0.0.0.0")
    monkeypatch.delenv("VEYA_TAILNET_TRUST_DOCKER_PUBLISH", raising=False)
    monkeypatch.delenv("VEYA_TAILNET_PUBLISH_HOST", raising=False)
    monkeypatch.delenv("VEYA_TAILNET_WHOIS_SOCKET", raising=False)
    monkeypatch.delenv("VEYA_TAILNET_NODE", raising=False)
    service = TailnetService(home=tmp_path / "data", whois_fn=lambda _ip: OWNER_WHOIS)
    assert start_receiver_if_enabled(service) is False
    assert service.server is None
    assert service.thread is None


def test_proxy_accepts_only_single_tailnet_whois_request() -> None:
    path = ROOT / "deploy" / "tailnet_whois_proxy.py"
    spec = importlib.util.spec_from_file_location("tailnet_whois_proxy_for_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_whois_target("GET", "/localapi/v0/whois?addr=100.64.0.2", "HTTP/1.1") == "/localapi/v0/whois?addr=100.64.0.2"
    assert module.parse_whois_target("GET", "/localapi/v0/status", "HTTP/1.1") is None
    assert module.parse_whois_target("POST", "/localapi/v0/whois?addr=100.64.0.2", "HTTP/1.1") is None
    assert module.parse_whois_target("GET", "/localapi/v0/whois?addr=127.0.0.1", "HTTP/1.1") is None
    assert module.parse_whois_target("GET", "/localapi/v0/whois?addr=100.64.0.2&foo=bar", "HTTP/1.1") is None

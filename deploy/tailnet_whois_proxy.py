#!/usr/bin/env python3
"""Expose only Tailscale LocalAPI whois over a separate Unix socket.

Do not mount tailscaled.sock into an application container: a read-only bind
mount does not restrict which LocalAPI endpoints a peer can call.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
import os
import socket
import socketserver
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

MAX_HEADER_BYTES = 8192
MAX_BODY_BYTES = 1024 * 1024
WHOIS_PATH = "/localapi/v0/whois"
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def is_tailnet_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address in (TAILNET_V4 if address.version == 4 else TAILNET_V6)


def parse_whois_target(method: str, target: str, version: str) -> str | None:
    """Return a canonical upstream path only for a single Tailnet whois lookup."""
    if method != "GET" or version not in {"HTTP/1.0", "HTTP/1.1"}:
        return None
    try:
        parsed = urlsplit(target)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != WHOIS_PATH:
        return None
    if set(query) != {"addr"} or len(query["addr"]) != 1:
        return None
    address = query["addr"][0]
    if not is_tailnet_address(address):
        return None
    return f"{WHOIS_PATH}?addr={quote(address, safe='')}"


def json_response(sock: socket.socket, status: int, payload: dict[str, object]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    reasons = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}
    header = (
        f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    sock.sendall(header + body)


def make_handler(upstream_socket: str):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(5.0)
            total = 0
            try:
                first = self.rfile.readline(MAX_HEADER_BYTES + 1)
                total += len(first)
                if not first or len(first) > MAX_HEADER_BYTES or not first.endswith(b"\n"):
                    json_response(self.request, 400, {"ok": False, "error": "invalid request"})
                    return
                try:
                    method, target, version = first.decode("ascii").strip("\r\n").split(" ")
                except (UnicodeDecodeError, ValueError):
                    json_response(self.request, 400, {"ok": False, "error": "invalid request"})
                    return
                content_length = "0"
                transfer_encoding = ""
                while True:
                    line = self.rfile.readline(MAX_HEADER_BYTES + 1)
                    total += len(line)
                    if not line or len(line) > MAX_HEADER_BYTES or total > MAX_HEADER_BYTES:
                        json_response(self.request, 400, {"ok": False, "error": "invalid headers"})
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    try:
                        key, value = line.decode("ascii").split(":", 1)
                    except (UnicodeDecodeError, ValueError):
                        json_response(self.request, 400, {"ok": False, "error": "invalid headers"})
                        return
                    if key.strip().lower() == "content-length":
                        content_length = value.strip()
                    elif key.strip().lower() == "transfer-encoding":
                        transfer_encoding = value.strip()
                if transfer_encoding or content_length not in {"", "0"}:
                    json_response(
                        self.request, 403, {"ok": False, "error": "request body is not permitted"}
                    )
                    return
                upstream_target = parse_whois_target(method, target, version)
                if upstream_target is None:
                    json_response(
                        self.request, 403, {"ok": False, "error": "only Tailnet whois is permitted"}
                    )
                    return
                conn = UnixHTTPConnection(upstream_socket, timeout=5.0)
                try:
                    conn.request(
                        "GET",
                        upstream_target,
                        headers={"Host": "local-tailscaled.sock", "Connection": "close"},
                    )
                    response = conn.getresponse()
                    body = response.read(MAX_BODY_BYTES + 1)
                    if response.status != 200 or len(body) > MAX_BODY_BYTES:
                        json_response(
                            self.request, 502, {"ok": False, "error": "whois unavailable"}
                        )
                        return
                    try:
                        parsed = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        json_response(
                            self.request, 502, {"ok": False, "error": "invalid whois response"}
                        )
                        return
                    if not isinstance(parsed, dict) or not isinstance(parsed.get("Node"), dict):
                        json_response(
                            self.request, 502, {"ok": False, "error": "invalid whois response"}
                        )
                        return
                    header = (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "Cache-Control: no-store\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                    self.request.sendall(header + body)
                finally:
                    conn.close()
            except (OSError, ValueError, http.client.HTTPException):
                with contextlib.suppress(OSError):
                    json_response(self.request, 502, {"ok": False, "error": "whois unavailable"})

    return Handler


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] != "--listen" or sys.argv[3] != "--upstream":
        print(
            "usage: tailnet_whois_proxy.py --listen SOCKET --upstream TAILSCALED_SOCKET",
            file=sys.stderr,
        )
        return 2
    listen_path = Path(sys.argv[2])
    upstream_path = sys.argv[4]
    if not Path(upstream_path).is_socket():
        print("tailscaled LocalAPI socket is unavailable", file=sys.stderr)
        return 2
    listen_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if os.path.lexists(listen_path):
        print("whois proxy socket path already exists; refusing to replace it", file=sys.stderr)
        return 2
    server = ThreadingUnixServer(str(listen_path), make_handler(upstream_path))
    os.chmod(listen_path, 0o660)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if listen_path.is_socket():
            listen_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

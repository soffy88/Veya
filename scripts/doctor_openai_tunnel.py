#!/usr/bin/env python3
"""Doctor for the Veya OpenAI Secure MCP Tunnel.

Checks the local tunnel-client install, the loopback MCP, credential files
(and their modes), the owner inputs, control-plane reachability and tunnel
readiness. Each failure is reported with a stable code:

    MCP_UNAVAILABLE, VEYA_AUTH_FAILED, TUNNEL_ID_MISSING,
    TUNNEL_API_KEY_MISSING, TUNNEL_PERMISSION_DENIED,
    CONTROL_PLANE_UNREACHABLE, TUNNEL_NOT_READY

It never prints secret values.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
MCP_URL = os.environ.get("VEYA_MCP_URL", "http://127.0.0.1:8790/mcp")
AUTH_FILE = Path(os.environ.get("VEYA_MCP_AUTH_FILE", HOME / ".veya" / "remote_mcp_auth_header"))
TOKEN_FILE = Path(
    os.environ.get("VEYA_REMOTE_TOKEN_FILE", HOME / ".veya" / "remote_mcp_token.secret")
)
TUNNEL_DIR = Path(os.environ.get("VEYA_TUNNEL_DIR", HOME / ".veya" / "openai-tunnel"))
CLIENT = Path(os.environ.get("TUNNEL_CLIENT_BIN", HOME / ".local" / "bin" / "tunnel-client"))
RUNTIME = Path(
    os.environ.get("TUNNEL_RUNTIME_BIN", HOME / ".local" / "bin" / "tunnel-client-runtime")
)
CONTROL_PLANE = os.environ.get("CONTROL_PLANE_BASE_URL", "https://api.openai.com")
HEALTH_URL_DEFAULT = "http://127.0.0.1:8794"

RESULTS: list[tuple[str, bool, str, str]] = []  # name, ok, code, detail


def check(name: str, ok: bool, code: str = "", detail: str = "") -> bool:
    RESULTS.append((name, ok, code, detail))
    print(f"{'OK  ' if ok else 'FAIL'} {name:<34} {code or ''} {detail}".rstrip())
    return ok


def _mode_0600(path: Path) -> bool:
    return path.is_file() and (path.stat().st_mode & 0o777) == 0o600


def _env_value(key: str) -> str | None:
    if os.environ.get(key):
        return os.environ[key].strip()
    env_file = TUNNEL_DIR / "tunnel.env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return None


def check_client() -> bool:
    if not CLIENT.is_file():
        return check("tunnel-client installed", False, "TUNNEL_CLIENT_MISSING", str(CLIENT))
    try:
        out = subprocess.run([str(CLIENT), "--version"], capture_output=True, text=True, timeout=20)
        version = (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as exc:
        return check("tunnel-client installed", False, "TUNNEL_CLIENT_MISSING", str(exc))
    return check("tunnel-client installed", out.returncode == 0, "", version)


def check_loopback() -> bool:
    host, _, port = MCP_URL.split("//", 1)[1].partition("/")[0].partition(":")
    try:
        with socket.create_connection((host, int(port or 80)), timeout=5):
            return check("127.0.0.1:8790 reachable", True, "", f"{host}:{port}")
    except OSError as exc:
        return check("127.0.0.1:8790 reachable", False, "MCP_UNAVAILABLE", str(exc))


def check_mcp_initialize() -> bool:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if AUTH_FILE.is_file():
        headers["Authorization"] = AUTH_FILE.read_text(encoding="utf-8").strip()
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    ).encode()
    request = urllib.request.Request(MCP_URL, data=payload, headers=headers, method="POST")
    session_id = None
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode())
            session_id = response.headers.get("mcp-session-id")
    except urllib.error.HTTPError as exc:
        return check("MCP initialize works", False, "MCP_UNAVAILABLE", f"HTTP {exc.code}")
    except Exception as exc:
        return check("MCP initialize works", False, "MCP_UNAVAILABLE", str(exc))
    if "result" in body:
        if session_id:
            # Terminate so repeated doctor runs never exhaust the session cap.
            try:
                terminate = urllib.request.Request(
                    MCP_URL, method="DELETE", headers={**headers, "Mcp-Session-Id": session_id}
                )
                urllib.request.urlopen(terminate, timeout=10).close()
            except Exception:
                pass
        return check(
            "MCP initialize works", True, "", body["result"].get("serverInfo", {}).get("name", "")
        )
    code = (body.get("error") or {}).get("data", {}).get("error_code", "MCP_UNAVAILABLE")
    return check(
        "MCP initialize works", False, "VEYA_AUTH_FAILED" if code == "AUTH_DENIED" else code
    )


def check_bearer() -> bool:
    if not AUTH_FILE.is_file():
        return check("Veya bearer available", False, "VEYA_AUTH_FAILED", "auth header file missing")
    value = AUTH_FILE.read_text(encoding="utf-8").strip()
    ok = value.startswith("Bearer ") and len(value) > 20
    return check("Veya bearer available", ok, "" if ok else "VEYA_AUTH_FAILED", str(AUTH_FILE))


def check_modes() -> bool:
    ok = True
    if TOKEN_FILE.exists():
        ok = _mode_0600(TOKEN_FILE) and ok
    ok = _mode_0600(AUTH_FILE) and ok
    return check(
        "token file mode=0600", ok, "" if ok else "VEYA_AUTH_FAILED", "secret files must be 0600"
    )


def check_tunnel_id() -> bool:
    value = _env_value("CONTROL_PLANE_TUNNEL_ID")
    ok = bool(value) and value.startswith("tunnel_")
    return check(
        "Tunnel ID present", ok, "" if ok else "TUNNEL_ID_MISSING", "CONTROL_PLANE_TUNNEL_ID"
    )


def check_api_key() -> bool:
    key_file = TUNNEL_DIR / "runtime_api_key"
    ok = key_file.is_file() and bool(key_file.read_text(encoding="utf-8").strip())
    return check(
        "Runtime API key present", ok, "" if ok else "TUNNEL_API_KEY_MISSING", str(key_file)
    )


def check_control_plane() -> bool:
    proxy = (
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "http://127.0.0.1:7890"
    )
    handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})]
    opener = urllib.request.build_opener(*handlers)
    url = CONTROL_PLANE.rstrip("/") + "/v1/models"
    try:
        with opener.open(url, timeout=12) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        # Any HTTP response (even 401) proves the control plane is reachable.
        if exc.code in (401, 403):
            return check("control-plane connectivity", True, "", f"HTTP {exc.code} via proxy")
        return check(
            "control-plane connectivity", False, "CONTROL_PLANE_UNREACHABLE", f"HTTP {exc.code}"
        )
    except Exception as exc:
        return check("control-plane connectivity", False, "CONTROL_PLANE_UNREACHABLE", str(exc))
    return check("control-plane connectivity", True, "", f"HTTP {status}")


def _journal_tail(lines: int = 60) -> str:
    try:
        return subprocess.run(
            ["journalctl", "--user", "-u", "veya-openai-tunnel", "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except Exception:
        return ""


def check_readiness() -> bool:
    try:
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "veya-openai-tunnel"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:
        active = "unknown"
    detail = f"service={active}"
    if active != "active":
        log = _journal_tail().lower()
        if any(token in log for token in ("401", "403", "forbidden", "unauthorized", "permission")):
            return check("tunnel-client readiness", False, "TUNNEL_PERMISSION_DENIED", detail)
        return check("tunnel-client readiness", False, "TUNNEL_NOT_READY", detail)
    url_file = TUNNEL_DIR / "health.url"
    base = (
        url_file.read_text(encoding="utf-8").strip() if url_file.is_file() else HEALTH_URL_DEFAULT
    )
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/readyz", timeout=8) as response:
            ready = response.status == 200
            detail += f" readyz={response.status}"
    except Exception as exc:
        ready = False
        detail += f" readyz_err={type(exc).__name__}"
    return check("tunnel-client readiness", ready, "" if ready else "TUNNEL_NOT_READY", detail)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    check_client()
    check_loopback()
    check_mcp_initialize()
    check_bearer()
    check_modes()
    check_tunnel_id()
    check_api_key()
    check_control_plane()
    check_readiness()

    failed = [r for r in RESULTS if not r[1]]
    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failed,
                    "checks": [
                        {"name": n, "ok": ok, "code": code, "detail": detail}
                        for n, ok, code, detail in RESULTS
                    ],
                    "missing_owner_input": sorted(
                        {
                            r[2]
                            for r in failed
                            if r[2] in {"TUNNEL_ID_MISSING", "TUNNEL_API_KEY_MISSING"}
                        }
                    ),
                },
                indent=2,
            )
        )
    else:
        print("-" * 60)
        if failed:
            print("DOCTOR: not ready — " + ", ".join(sorted({r[2] for r in failed if r[2]})))
        else:
            print("DOCTOR: ready")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

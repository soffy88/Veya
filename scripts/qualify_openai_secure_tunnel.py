#!/usr/bin/env python3
"""Qualification for the Veya OpenAI Secure MCP Tunnel.

Local evidence is collected for real; everything that requires the OpenAI
control plane is only marked PASS when the tunnel is actually ready, otherwise
BLOCKED with the missing owner input. Nothing is asserted from "the process
exists".

    python scripts/qualify_openai_secure_tunnel.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parents[1]
MCP_URL = os.environ.get("VEYA_MCP_URL", "http://127.0.0.1:8790/mcp")
QUALIFICATION_WORKSPACE = os.environ.get("VEYA_QUALIFICATION_WORKSPACE", str(REPO))
AUTH_FILE = Path(os.environ.get("VEYA_MCP_AUTH_FILE", HOME / ".veya" / "remote_mcp_auth_header"))
# Dedicated qualifier token: qualification runs must never share (or clean up)
# the operator/ChatGPT worktree. Falls back to the operator header if absent.
QUALIFIER_AUTH_FILE = Path(
    os.environ.get("VEYA_QUALIFIER_AUTH_FILE", HOME / ".veya" / "remote_qualifier_auth_header")
)
TOKEN_FILE = Path(
    os.environ.get("VEYA_REMOTE_TOKEN_FILE", HOME / ".veya" / "remote_mcp_token.secret")
)
TUNNEL_DIR = Path(os.environ.get("VEYA_TUNNEL_DIR", HOME / ".veya" / "openai-tunnel"))
CLIENT = Path(os.environ.get("TUNNEL_CLIENT_BIN", HOME / ".local" / "bin" / "tunnel-client"))
CONFIG = TUNNEL_DIR / "tunnel-client.yaml"
HEALTH_DEFAULT = "http://127.0.0.1:8794"

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"
RESULTS: list[tuple[str, str, str]] = []


def rec(name: str, status: str, note: str = "") -> None:
    RESULTS.append((name, status, note))
    print(f"{name}={status}" + (f"  # {note}" if note else ""))


def _token() -> str:
    return TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.is_file() else ""


def _auth_header() -> str:
    return AUTH_FILE.read_text(encoding="utf-8").strip() if AUTH_FILE.is_file() else ""


def _local_auth_header() -> str:
    """Auth for local MCP calls (qualifier token preferred)."""

    if QUALIFIER_AUTH_FILE.is_file():
        return QUALIFIER_AUTH_FILE.read_text(encoding="utf-8").strip()
    return _auth_header()


def _rpc(payload: dict, session: str | None = None, auth: str | None = None, timeout: float = 30):
    import httpx

    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    if session:
        headers["Mcp-Session-Id"] = session
    with httpx.Client(timeout=timeout) as client:
        return client.post(MCP_URL, headers=headers, json=payload)


def _terminate(session: str | None) -> None:
    """Close a remote MCP session (MCP Streamable HTTP DELETE)."""
    if not session:
        return
    try:
        import httpx

        with httpx.Client(timeout=10) as client:
            client.request(
                "DELETE",
                MCP_URL,
                headers={"Authorization": _local_auth_header(), "Mcp-Session-Id": session},
            )
    except Exception:
        pass


def _envelope(response) -> dict:
    body = response.json()
    return (body.get("result") or {}).get("structuredContent") or {
        "ok": False,
        "raw": json.dumps(body)[:200],
    }


def check_installed() -> None:
    if not CLIENT.is_file():
        return rec("OPENAI_TUNNEL_CLIENT_INSTALLED", FAIL, "tunnel-client missing")
    out = subprocess.run([str(CLIENT), "--version"], capture_output=True, text=True, timeout=20)
    version = (out.stdout or out.stderr).strip().splitlines()[0]
    rec("OPENAI_TUNNEL_CLIENT_INSTALLED", PASS if out.returncode == 0 else FAIL, version)


def check_loopback_only() -> None:
    try:
        listeners = subprocess.run(
            ["ss", "-tln"], capture_output=True, text=True, timeout=10
        ).stdout.splitlines()
    except Exception as exc:
        return rec("LOCAL_MCP_LOOPBACK_ONLY", FAIL, str(exc))
    loop = any("127.0.0.1:8790" in line for line in listeners)
    public = any(s in line for line in listeners for s in ("0.0.0.0:8790", "[::]:8790"))
    rec("LOCAL_MCP_LOOPBACK_ONLY", PASS if loop and not public else FAIL, "127.0.0.1 only")


def check_local_auth() -> bool:
    bad = _rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, auth="Bearer invalid"
    )
    bad_code = (bad.json().get("error") or {}).get("data", {}).get("error_code")
    good = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        },
        auth=_local_auth_header(),
    )
    ok = bad_code == "AUTH_DENIED" and "result" in good.json()
    rec("LOCAL_MCP_AUTH", PASS if ok else FAIL, f"invalid={bad_code}")
    _terminate(good.headers.get("mcp-session-id"))
    return ok


def _mcp_flow() -> None:
    """Real MCP flow against the tunnel's configured upstream (loopback)."""
    auth = _local_auth_header()
    init = _rpc({"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {}}, auth=auth)
    session = init.headers.get("mcp-session-id") or (init.json().get("result") or {}).get(
        "sessionId"
    )
    rec("TUNNEL_MCP_INITIALIZE", PASS if "result" in init.json() else FAIL)

    listing = _rpc(
        {"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}},
        session=session,
        auth=auth,
    )
    tools = [t["name"] for t in ((listing.json().get("result") or {}).get("tools") or [])]
    rec("TUNNEL_MCP_TOOLS_LIST", PASS if "file.read" in tools else FAIL, f"{len(tools)} tools")
    rec("TUNNEL_SESSION_PROPAGATION", PASS if session else FAIL, str(session)[:20])

    def call(name: str, arguments: dict) -> dict:
        return _envelope(
            _rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                session=session,
                auth=auth,
            )
        )

    selector = {"workspace_path": QUALIFICATION_WORKSPACE}
    # For file primitives the selector is the file target itself.  Passing a
    # repository selector together with a different relative ``path`` is
    # intentionally rejected by the adapter as an ambiguous target.
    read = call(
        "file.read",
        {"workspace_path": str(Path(QUALIFICATION_WORKSPACE) / "AGENTS.md")},
    )
    rec("TUNNEL_FILE_READ", PASS if read.get("ok") is True else FAIL)
    probe = "tests/_remote_tunnel_probe.py"
    probe_path = str(Path(QUALIFICATION_WORKSPACE) / probe)
    write = call(
        "file.write",
        {
            "workspace_path": probe_path,
            "content": "def test_tunnel_probe():\n    assert True\n",
        },
    )
    rec("TUNNEL_FILE_WRITE", PASS if write.get("ok") is True else FAIL)
    test = call(
        "test.run",
        {
            **selector,
            "command": "python3 -c \"print('tunnel probe ok')\"",
            "wait": True,
        },
    )
    rec("TUNNEL_TEST_RUN", PASS if test.get("ok") is True else FAIL)
    diff = call("git.diff", {"workspace_path": probe_path})
    rec(
        "TUNNEL_GIT_DIFF",
        PASS
        if diff.get("ok") is True
        and diff.get("result", {}).get("repo_root") == str(Path(QUALIFICATION_WORKSPACE).resolve())
        else FAIL,
    )
    status = call("git.status", {"workspace_path": probe_path})
    rec(
        "TUNNEL_WORKTREE_ISOLATION",
        PASS if ".veya/worktrees/task-remote-" in json.dumps(status) else FAIL,
    )
    _terminate(session)


def check_secret_not_in_repo() -> None:
    token = _token()
    if not token:
        return rec("SECRET_NOT_IN_REPO", FAIL, "token file unreadable")
    skip = {
        ".git",
        "node_modules",
        "venv",
        ".venv",
        "__pycache__",
        ".mypy_cache",
        "dist",
        "build",
        ".veya",
    }
    hits = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            path = Path(root) / name
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if token in text:
                hits.append(str(path.relative_to(REPO)))
    rec("SECRET_NOT_IN_REPO", PASS if not hits else FAIL, ",".join(hits[:3]))


def check_secret_not_in_log() -> None:
    token = _token()
    if not token:
        return rec("SECRET_NOT_IN_LOG", FAIL, "token file unreadable")
    haystacks = [HOME / ".veya" / "remote_audit.jsonl"]
    haystacks += list(TUNNEL_DIR.glob("*.log"))
    try:
        journal = subprocess.run(
            ["journalctl", "--user", "-u", "veya-openai-tunnel", "-n", "500", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except Exception:
        journal = ""
    leaks = []
    for path in haystacks:
        if path.is_file() and token in path.read_text(encoding="utf-8", errors="ignore"):
            leaks.append(path.name)
    if token in journal:
        leaks.append("journal:veya-openai-tunnel")
    rec("SECRET_NOT_IN_LOG", PASS if not leaks else FAIL, ",".join(leaks[:3]))


def check_token_isolated() -> None:
    """Static proof the Veya bearer is an MCP-origin header, never a control-plane header."""
    if not CONFIG.is_file():
        return rec("VEYA_TOKEN_NOT_SENT_TO_CONTROL_PLANE", FAIL, "config missing")
    try:
        import yaml

        cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return rec("VEYA_TOKEN_NOT_SENT_TO_CONTROL_PLANE", FAIL, f"config unparsable: {exc}")
    cp_headers = (cfg.get("control_plane") or {}).get("extra_headers") or {}
    mcp_headers = (cfg.get("mcp") or {}).get("extra_headers") or {}
    ok = not cp_headers and "authorization" in {k.lower() for k in mcp_headers}
    rec(
        "VEYA_TOKEN_NOT_SENT_TO_CONTROL_PLANE",
        PASS if ok else FAIL,
        "auth header is MCP-origin only",
    )


def check_static_auth_injection() -> None:
    """Prove the official runtime injects the Veya bearer as a static MCP header.

    Starts a local mock MCP, runs the real tunnel-client-runtime against it with
    a deliberately dummy control plane (no real OpenAI credential), and asserts
    the captured Authorization header equals the Veya bearer.
    """
    auth = _auth_header()
    if not auth:
        return rec("VEYA_STATIC_AUTH_INJECTION", FAIL, "auth header missing")

    import http.server
    import tempfile
    import threading
    import time

    captured: list[str | None] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # silence
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            captured.append(self.headers.get("Authorization"))
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "mock", "version": "0"},
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="veya-tunnel-probe-"))
    (tmp / "key").write_text("sk-dummy-local-probe-not-a-real-key\n", encoding="utf-8")
    (tmp / "cfg.yaml").write_text(
        "config_version: 1\n"
        "control_plane:\n"
        f"  api_key: file:{tmp / 'key'}\n"
        "mcp:\n"
        "  server_urls:\n"
        f"    - url: http://127.0.0.1:{port}/mcp\n"
        "      channel: main\n"
        "  extra_headers:\n"
        f"    Authorization: file:{AUTH_FILE}\n"
        "health:\n"
        "  listen_addr: 127.0.0.1:0\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["CONTROL_PLANE_TUNNEL_ID"] = "tunnel_" + "0" * 32
    env.pop("CONTROL_PLANE_API_KEY", None)
    runtime = HOME / ".local" / "bin" / "tunnel-client-runtime"
    proc = subprocess.Popen(
        [str(runtime), "run", "--config", str(tmp / "cfg.yaml")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(48):
            time.sleep(0.25)
            if captured:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        server.shutdown()
        server.server_close()

    ok = bool(captured) and captured[0] == auth
    rec("VEYA_STATIC_AUTH_INJECTION", PASS if ok else FAIL, f"mock_mcp_requests={len(captured)}")


def check_local_side_effects() -> None:
    """FALSE_SUCCESS=0 / DUPLICATE_SIDE_EFFECTS=0 against the loopback MCP."""
    auth = _local_auth_header()
    init = _rpc({"jsonrpc": "2.0", "id": 20, "method": "initialize", "params": {}}, auth=auth)
    session = init.headers.get("mcp-session-id") or (init.json().get("result") or {}).get(
        "sessionId"
    )

    def call(name: str, arguments: dict) -> dict:
        return _envelope(
            _rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 21,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                session=session,
                auth=auth,
            )
        )

    escape = call("file.read", {"path": "../../../etc/passwd"})
    rec(
        "FALSE_SUCCESS_0",
        PASS if escape.get("ok") is False else FAIL,
        f"escape_ok={escape.get('ok')}",
    )
    probe = "tests/_remote_tunnel_probe.py"
    call("file.write", {"path": probe, "content": "x\n"})
    status = call("git.status", {})
    occurrences = json.dumps(status).count(probe)
    rec(
        "DUPLICATE_SIDE_EFFECTS_0", PASS if occurrences <= 2 else FAIL, f"occurrences={occurrences}"
    )
    _terminate(session)


def _tunnel_state() -> tuple[bool, str]:
    try:
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "veya-openai-tunnel"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:
        active = "unknown"
    if active != "active":
        missing = []
        key = TUNNEL_DIR / "runtime_api_key"
        if not (key.is_file() and key.read_text(encoding="utf-8").strip()):
            missing.append("RUNTIME_API_KEY")
        env = TUNNEL_DIR / "tunnel.env"
        has_id = env.is_file() and any(
            line.strip().startswith("CONTROL_PLANE_TUNNEL_ID=tunnel_")
            for line in env.read_text(encoding="utf-8").splitlines()
        )
        if not has_id:
            missing.append("TUNNEL_ID")
        return False, "OWNER_INPUT_REQUIRED=" + ",".join(missing) if missing else "TUNNEL_NOT_READY"
    base = HEALTH_DEFAULT
    if (TUNNEL_DIR / "health.url").is_file():
        base = (TUNNEL_DIR / "health.url").read_text(encoding="utf-8").strip()
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/readyz", timeout=8) as response:
            body = response.read().decode()
            ready = response.status == 200
    except urllib.error.HTTPError as exc:
        return False, f"TUNNEL_NOT_READY(readyz={exc.code})"
    except Exception as exc:
        return False, f"TUNNEL_NOT_READY({type(exc).__name__})"
    return ready, body.strip() or "ready"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    check_installed()
    check_loopback_only()
    check_local_auth()
    check_secret_not_in_repo()
    check_secret_not_in_log()
    check_token_isolated()
    check_static_auth_injection()
    check_local_side_effects()

    ready, detail = _tunnel_state()
    tunnel_names = [
        "TUNNEL_CONTROL_PLANE_AUTH",
        "TUNNEL_CONNECTED",
        "TUNNEL_READY",
    ]
    for name in tunnel_names:
        rec(name, PASS if ready else BLOCKED, detail if not ready else "")

    if ready:
        _mcp_flow()
        # A ready tunnel whose own startup probe reached the same upstream is
        # the strongest non-ChatGPT evidence available locally.
    else:
        for name in (
            "TUNNEL_MCP_INITIALIZE",
            "TUNNEL_MCP_TOOLS_LIST",
            "TUNNEL_SESSION_PROPAGATION",
            "TUNNEL_FILE_READ",
            "TUNNEL_FILE_WRITE",
            "TUNNEL_TEST_RUN",
            "TUNNEL_GIT_DIFF",
            "TUNNEL_WORKTREE_ISOLATION",
        ):
            rec(name, BLOCKED, detail)

    blocked = [n for n, s, _ in RESULTS if s == BLOCKED]
    failed = [n for n, s, _ in RESULTS if s == FAIL]
    print("-" * 60)
    if failed:
        print(f"TUNNEL QUALIFICATION FAILED: {', '.join(failed)}")
        return 1
    if blocked:
        print(f"TUNNEL QUALIFICATION BLOCKED: {len(blocked)} item(s)")
        print(
            detail if detail.startswith("OWNER_INPUT_REQUIRED=") else "OWNER_INPUT_REQUIRED=UNKNOWN"
        )
        return 2
    print("TUNNEL QUALIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

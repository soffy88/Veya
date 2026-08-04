#!/usr/bin/env python
"""P4 live validation script — run all non-LLM routes."""

import os
import sys
import warnings

warnings.filterwarnings("ignore")

sys.path.insert(0, "/home/soffy/projects/hicode")
os.chdir("/home/soffy/projects/hicode")

from config.loader import load_config

load_config()
from fastapi.testclient import TestClient

from server.app import app

client = TestClient(app, raise_server_exceptions=False)

passed, failed = [], []


def check(name, cond, detail=""):
    sym = "OK" if cond else "NG"
    (passed if cond else failed).append(name)
    print(f"  [{sym}] {name}" + (f"  ({detail})" if detail else ""), flush=True)


print("=== /tool ===")
r = client.post(
    "/tool",
    json={
        "name": "write",
        "args": {"path": "README.md", "content": "# hicode via /tool\nhicode.\n"},
        "persona": "build",
    },
)
first = open("README.md").readline().strip()
check("/tool write disk", first == "# hicode via /tool", first)
open("README.md", "w").write("# hicode\nhicode — AI 编码 agent。\n")

r = client.post(
    "/tool",
    json={"name": "write", "args": {"path": "x.txt", "content": "x"}, "persona": "research"},
)
check("/tool H2 block research", r.status_code == 403, str(r.json()))

r = client.post(
    "/tool", json={"name": "bash", "args": {"command": "echo hi_bash"}, "persona": "build"}
)
check("/tool bash stdout", "hi_bash" in str(r.json()), str(r.json()))

print("\n=== /session ===")
r = client.post("/session", json={"persona": "build"})
sid = r.json().get("session_id", "")
check("/session create", r.status_code == 200 and bool(sid))

r = client.post(f"/session/{sid}/fork", json={"label": "fork1"})
new_sid = r.json().get("session_id", "")
check("/session fork: 200", r.status_code == 200, str(r.status_code) + " " + str(r.json()))
check("/session fork: new id", new_sid != sid and bool(new_sid), new_sid)

check("/session get", client.get(f"/session/{sid}").status_code == 200)
check("/session undo", client.post(f"/session/{sid}/undo").status_code == 200)
check("/session compact", client.post(f"/session/{sid}/compact").status_code == 200)
r = client.post(f"/session/{sid}/share")
check("/session share token", r.status_code == 200 and "share_token" in r.json(), str(r.json()))

print("\n=== /models ===")
r = client.get("/models")
check("/models 200", r.status_code == 200, str(r.json()))
r = client.get("/models/openai")
check("/models/openai 200", r.status_code == 200)
check("/models/bad 404", client.get("/models/nonexistent").status_code == 404)

print("\n=== /auth ===")
check("/auth/providers", client.get("/auth/providers").status_code == 200)
r = client.post("/auth/key", json={"provider": "anthropic", "api_key": "sk-ant-test"})
check(
    "/auth/key set",
    r.status_code == 200 and os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-test",
    str(r.json()),
)
check(
    "/auth/key bad",
    client.post("/auth/key", json={"provider": "unknown", "api_key": "x"}).status_code == 400,
)

print("\n=== /mcp /stream /health ===")
check("/mcp list", client.get("/mcp").status_code == 200)
# Pre-close the queue so the SSE stream terminates immediately during test
from server.sse import get_or_create_queue

_q = get_or_create_queue("stream-test-123")
_q.close()
r = client.get("/stream/stream-test-123", headers={"accept": "text/event-stream"})
check("/stream 200", r.status_code == 200, str(r.status_code))
check("/stream DONE body", "[DONE]" in r.text, r.text[:80])
check("/health", client.get("/health").json()["status"] == "ok")

print()
print("=" * 40)
print(f"Non-LLM: {len(passed)}/{len(passed) + len(failed)} PASS")
if failed:
    print(f"FAIL: {failed}")
    sys.exit(1)

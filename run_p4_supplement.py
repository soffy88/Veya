#!/usr/bin/env python
"""P4 補驗 — 4 items side-effect validation"""
import warnings, os, pathlib, json, sys
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

# ─────────────────────────────────────────────
# ① /share 脱敏 (SECURITY)
# ─────────────────────────────────────────────
print("=== ① /share 脱敏 ===")

# Create a session with sensitive data
r = client.post("/session", json={"persona": "build", "config": {"api_key": "sk-c1a49b21757040e5abba2a578634f6c0"}})
sid = r.json()["session_id"]

# Inject sensitive messages into the session directly (simulate history)
from server.routes.session import _sessions, _shares
_sessions[sid]["messages"] = [
    {"role": "user", "content": "my key is sk-c1a49b21757040e5abba2a578634f6c0"},
    {"role": "assistant", "content": "file saved to /home/soffy/projects/hicode/.env"},
]

# Call share
r = client.post(f"/session/{sid}/share")
check("/share: 200", r.status_code == 200, str(r.json()))
share_token = r.json().get("share_token", "")
check("/share: token returned", bool(share_token))

# Verify redacted payload in _shares
payload = _shares.get(share_token, {})
blob = json.dumps(payload)
print(f"  redacted blob: {blob[:200]}", flush=True)
check("/share: sk-key REDACTED", "sk-c1a49b21757040e5abba2a578634f6c0" not in blob,
      "[LEAK] key still in payload" if "sk-c1a49b21757040e5abba2a578634f6c0" in blob else "ok")
check("/share: /home/soffy REDACTED", "/home/soffy" not in blob,
      "[LEAK] path still in payload" if "/home/soffy" in blob else "ok")
check("/share: config NOT in payload", "api_key" not in blob,
      "[LEAK] config key in payload" if "api_key" in blob else "ok")

# ─────────────────────────────────────────────
# ② /session/undo 真恢复
# ─────────────────────────────────────────────
print("\n=== ② /session/undo 真恢復 ===")

# Create a session for undo test
r = client.post("/session", json={"persona": "build"})
sid2 = r.json()["session_id"]

# Record original file content
original_content = pathlib.Path("README.md").read_text()
check("/undo: README original exists", "# hicode" in original_content)

# Write via /tool WITH session_id (so snapshot is saved)
r = client.post("/tool", json={
    "name": "write",
    "args": {"path": "README.md", "content": "# CHANGED_FOR_UNDO_TEST\nModified.\n"},
    "persona": "build",
    "session_id": sid2,
})
check("/undo: tool write succeeded", r.status_code == 200, str(r.json()))
changed_content = pathlib.Path("README.md").read_text()
check("/undo: file changed on disk", "# CHANGED_FOR_UNDO_TEST" in changed_content, changed_content[:50])

# Now undo
r = client.post(f"/session/{sid2}/undo")
check("/undo: 200", r.status_code == 200, str(r.json()))
files_restored = r.json().get("files_restored", [])
check("/undo: files_restored non-empty", bool(files_restored), str(files_restored))

# Verify file is restored
restored_content = pathlib.Path("README.md").read_text()
check("/undo: README restored to original", restored_content == original_content,
      f"got: {restored_content[:50]!r}")

# ─────────────────────────────────────────────
# ③ /session/compact 真压缩
# ─────────────────────────────────────────────
print("\n=== ③ /session/compact 真壓縮 ===")

# Create a session and stuff it with 12 messages
r = client.post("/session", json={"persona": "build"})
sid3 = r.json()["session_id"]
_sessions[sid3]["messages"] = [
    {"role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i}: " + "x" * 200}
    for i in range(12)
]
msg_count_before = len(_sessions[sid3]["messages"])
chars_before = sum(len(m["content"]) for m in _sessions[sid3]["messages"])
print(f"  before: {msg_count_before} msgs, {chars_before} chars", flush=True)

r = client.post(f"/session/{sid3}/compact")
check("/compact: 200", r.status_code == 200, str(r.json()))
result = r.json()
check("/compact: status=compacted", result.get("status") == "compacted", result.get("status"))
check("/compact: messages_before=12", result.get("messages_before") == 12, str(result.get("messages_before")))
check("/compact: messages_after<12", (result.get("messages_after") or 99) < 12, str(result.get("messages_after")))
check("/compact: chars_removed>0", (result.get("chars_removed") or 0) > 0, str(result.get("chars_removed")))

# Verify summary message is present
new_msgs = _sessions[sid3]["messages"]
has_summary = any("[Compacted:" in m.get("content", "") for m in new_msgs)
check("/compact: summary msg present", has_summary, str(new_msgs[0].get("content", "")[:80]))

# Verify short session is skipped
r = client.post("/session", json={"persona": "build"})
sid4 = r.json()["session_id"]
_sessions[sid4]["messages"] = [{"role": "user", "content": "short"} for _ in range(3)]
r = client.post(f"/session/{sid4}/compact")
check("/compact: short session skipped", r.json().get("status") == "skipped", r.json().get("status"))

# ─────────────────────────────────────────────
# ④ /mcp 真連
# ─────────────────────────────────────────────
print("\n=== ④ /mcp 真連 ===")

# Verify GET /mcp returns note about 待外部環境
r = client.get("/mcp")
check("/mcp: list 200", r.status_code == 200)
check("/mcp: note present", "待外部環境" in r.json().get("note", ""), str(r.json()))

# Verify /mcp/call returns 404 for unregistered server
r = client.post("/mcp/call", json={"server": "nonexistent", "tool": "any", "args": {}})
check("/mcp/call: unregistered → 404", r.status_code == 404, str(r.json()))

# Verify /mcp/connect fails gracefully when no server running
r = client.post("/mcp/connect", json={"name": "test", "url": "http://localhost:19999/mcp", "timeout": 1.0})
check("/mcp/connect: bad url → 500 graceful", r.status_code == 500, str(r.json().get("detail","")[:60]))

print()
print("  [NOTE] ④ /mcp: 待外部環境 — oprim interface verified, real MCP server needed for live tool test")

# ─────────────────────────────────────────────
print()
print("="*50)
print(f"P4 補驗: {len(passed)}/{len(passed)+len(failed)} PASS")
if failed:
    print(f"FAIL: {failed}")
    sys.exit(1)

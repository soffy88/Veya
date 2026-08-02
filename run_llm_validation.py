#!/usr/bin/env python
"""P4 LLM live validation — /prompt, /agent/invoke, /init"""
import warnings, os, pathlib, sys
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

# /prompt regression
print("=== /prompt ===")
r = client.post("/prompt", json={"text": "README.md の第一行は何?", "persona": "build"})
print(f"  status={r.status_code} cost={r.json().get('cost_usd')} content={str(r.json().get('content',''))[:60]}", flush=True)
check("/prompt success", r.json().get("status") == "success", str(r.json().get("status")))
check("/prompt cost>0", (r.json().get("cost_usd") or 0) > 0, str(r.json().get("cost_usd")))

# /agent/invoke
print("\n=== /agent/invoke ===")
r = client.post("/agent/invoke", json={"text": "Reply with just: PING", "persona": "build"})
print(f"  status={r.status_code} cost={r.json().get('cost_usd')} content={str(r.json().get('content',''))[:60]}", flush=True)
check("/agent build: 200", r.status_code == 200)
check("/agent build cost>0", (r.json().get("cost_usd") or 0) > 0, str(r.json().get("cost_usd")))

# /init
print("\n=== /init ===")
pathlib.Path("AGENTS.md").unlink(missing_ok=True)
r = client.post("/init", json={"repo": "."})
print(f"  status={r.status_code} body={str(r.json())[:120]}", flush=True)
check("/init: 200", r.status_code == 200, str(r.status_code))
check("/init: AGENTS.md on disk", pathlib.Path("AGENTS.md").exists())
check("/init: size>0", pathlib.Path("AGENTS.md").stat().st_size > 0 if pathlib.Path("AGENTS.md").exists() else False,
      str(pathlib.Path("AGENTS.md").stat().st_size) if pathlib.Path("AGENTS.md").exists() else "missing")
check("/init: cost>0", (r.json().get("cost_usd") or 0) > 0 if r.status_code == 200 else False,
      str(r.json().get("cost_usd")))

print()
print("="*40)
print(f"LLM checks: {len(passed)}/{len(passed)+len(failed)} PASS")
if failed:
    print(f"FAIL: {failed}")
    sys.exit(1)

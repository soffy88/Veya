#!/usr/bin/env python3
"""Test all routes called by frontend against http://127.0.0.1:8799."""

import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path

TOKEN = open(".aud3data/token.txt").read().strip()
BASE = "http://127.0.0.1:8799"

with open("evidence/frontend_calls.json") as f:
    calls = json.load(f)

# Load OpenAPI paths
req = urllib.request.Request(f"{BASE}/openapi.json")
with urllib.request.urlopen(req) as resp:
    openapi = json.loads(resp.read().decode())
openapi_paths = openapi.get("paths", {})

results = []

def make_request(method, path, body=None, token=TOKEN):
    url = f"{BASE}{path}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body_text = resp.read().decode(errors="replace")
            try:
                body_json = json.loads(body_text)
            except Exception:
                body_json = body_text[:200]
            return resp.status, body_json
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        try:
            body_json = json.loads(body_text)
        except Exception:
            body_json = body_text[:200]
        return e.code, body_json
    except Exception as e:
        return 0, str(e)

print(f"Loaded {len(calls)} frontend calls to verify.")

seen = set()
for c in calls:
    key = (c["method"], c["raw_path"])
    if key in seen:
        continue
    seen.add(key)
    
    # Resolve any placeholder in path
    path = c["raw_path"]
    if not path.startswith("/"):
        path = "/" + path
    
    # Check if raw_path has template variables like ${...} or encodeURIComponent
    clean_path = path
    clean_path = re.sub(r"\$\{[^}]*id[^}]*\}", "test-id", clean_path, flags=re.I)
    clean_path = re.sub(r"\$\{[^}]*name[^}]*\}", "test-name", clean_path, flags=re.I)
    clean_path = re.sub(r"\$\{[^}]*action[^}]*\}", "test-action", clean_path, flags=re.I)
    clean_path = re.sub(r"\$\{[^}]*sid[^}]*\}", "test-sid", clean_path, flags=re.I)
    clean_path = re.sub(r"\$\{[^}]*kind[^}]*\}", "role", clean_path, flags=re.I)
    clean_path = re.sub(r"\$\{[^}]*task_id[^}]*\}", "test-task", clean_path, flags=re.I)
    clean_path = re.sub(r"\$\{[^}]*status[^}]*\}", "pending", clean_path, flags=re.I)
    clean_path = re.sub(r"\$\{[^}]*params[^}]*\}", "q=test", clean_path, flags=re.I)
    clean_path = re.sub(r"\$\{[^}]*q[^}]*\}", "q=test", clean_path, flags=re.I)
    clean_path = re.sub(r"\$\{[^}]*filePath[^}]*\}", "README.md", clean_path, flags=re.I)
    clean_path = re.sub(r"\$\{[^}]*path[^}]*\}", "README.md", clean_path, flags=re.I)
    clean_path = re.sub(r"\$\{[^}]*\}", "test-param", clean_path)

    method = c["method"].upper()
    body = None
    if method in ("POST", "PUT", "PATCH"):
        body = {}
        # Simple sample bodies for common patterns
        if "action" in c["options"]:
            m = re.search(r'action\s*:\s*["\']([^"\']+)["\']', c["options"])
            if m:
                body["action"] = m.group(1)
        if "statement" in c["options"]:
            body["statement"] = "test statement"
        if "text" in c["options"]:
            body["text"] = "test text"
        if "name" in c["options"]:
            body["name"] = "test name"

    # Match in openapi
    # Find matching openapi path pattern (replacing {param} with regex)
    matched_openapi = None
    for op in openapi_paths:
        # turn /api/v1/bots/{bot_id} into regex ^/api/v1/bots/[^/]+$
        pat = re.sub(r"\{[^}]+\}", r"[^/]+", op)
        # ignore query string for path matching
        clean_no_qs = clean_path.split("?")[0]
        if re.match(f"^{pat}$", clean_no_qs):
            matched_openapi = op
            break

    status, resp_data = make_request(method, clean_path, body)
    
    # Analyze problem
    problem = None
    if matched_openapi is None:
        problem = "Route not defined in backend OpenAPI"
    elif method.lower() not in openapi_paths[matched_openapi]:
        problem = f"Method {method} not supported by backend path {matched_openapi} (supports {list(openapi_paths[matched_openapi].keys())})"
    elif status == 401:
        problem = "401 Unauthorized even with valid bearer token"
    elif status == 500:
        problem = f"500 Internal Server Error: {resp_data}"

    results.append({
        "frontend_call": c,
        "test_url": clean_path,
        "method": method,
        "openapi_path": matched_openapi,
        "status": status,
        "response": resp_data,
        "problem": problem
    })
    print(f"[{status}] {method:<6} {clean_path:<45} -> {problem or 'OK'}")

with open("evidence/frontend_route_verification.json", "w") as f:
    json.dump(results, f, indent=2)

print("\nDone! Saved to evidence/frontend_route_verification.json")

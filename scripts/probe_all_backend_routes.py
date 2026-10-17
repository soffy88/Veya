#!/usr/bin/env python3
"""Probe all 527 backend OpenAPI routes to detect any 5xx errors or auth discrepancies."""

import json
import re
import urllib.request
import urllib.error

TOKEN = "758fe9321611a3f290147b2b3e7edb793a9f9a3ada48b695"
BASE = "http://127.0.0.1:8799"

req = urllib.request.Request(f"{BASE}/openapi.json")
with urllib.request.urlopen(req) as resp:
    openapi = json.loads(resp.read().decode())

paths = openapi.get("paths", {})

routes_to_test = []
for p, methods in paths.items():
    for m in methods:
        if m.lower() in ("get", "post", "put", "patch", "delete"):
            routes_to_test.append((m.upper(), p))

print(f"Total routes to probe: {len(routes_to_test)}")

results = []
five_hundreds = []

for m, p in sorted(routes_to_test, key=lambda x: x[1]):
    # Fill path parameters with dummy valid IDs
    test_path = p
    test_path = re.sub(r"\{[^}]*id[^}]*\}", "test-id", test_path, flags=re.I)
    test_path = re.sub(r"\{[^}]*bot[^}]*\}", "test-bot", test_path, flags=re.I)
    test_path = re.sub(r"\{[^}]*session[^}]*\}", "test-sid", test_path, flags=re.I)
    test_path = re.sub(r"\{[^}]*task[^}]*\}", "test-task", test_path, flags=re.I)
    test_path = re.sub(r"\{[^}]*name[^}]*\}", "test-name", test_path, flags=re.I)
    test_path = re.sub(r"\{[^}]*action[^}]*\}", "test-action", test_path, flags=re.I)
    test_path = re.sub(r"\{[^}]*user[^}]*\}", "test-user", test_path, flags=re.I)
    test_path = re.sub(r"\{[^}]*\}", "test-val", test_path)

    url = f"{BASE}{test_path}"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json"
    }
    data = None
    if m in ("POST", "PUT", "PATCH"):
        data = b"{}"

    request = urllib.request.Request(url, data=data, headers=headers, method=m)
    status = 0
    resp_body = ""
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            status = r.status
            resp_body = r.read().decode(errors="replace")[:200]
    except urllib.error.HTTPError as e:
        status = e.code
        resp_body = e.read().decode(errors="replace")[:200]
    except Exception as e:
        resp_body = str(e)[:200]

    record = {
        "method": m,
        "openapi_path": p,
        "test_path": test_path,
        "status": status,
        "response": resp_body
    }
    results.append(record)

    if status >= 500:
        five_hundreds.append(record)
        print(f"FAILED 5XX: [{status}] {m} {test_path} -> {resp_body}")

print(f"\nProbing complete. Total routes tested: {len(results)}. 5xx count: {len(five_hundreds)}")
with open("evidence/all_routes_probe.json", "w") as f:
    json.dump(results, f, indent=2)

if five_hundreds:
    with open("evidence/500_errors.json", "w") as f:
        json.dump(five_hundreds, f, indent=2)

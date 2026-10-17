#!/usr/bin/env python3
"""Audit FastAPI backend routes vs Frontend API calls."""

import inspect
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

# 1. Fetch OpenAPI
OPENAPI_URL = "http://127.0.0.1:8799/openapi.json"
req = urllib.request.Request(OPENAPI_URL)
with urllib.request.urlopen(req) as resp:
    openapi_spec = json.loads(resp.read().decode())

paths = openapi_spec.get("paths", {})

# 2. Extract backend route details via FastAPI app directly
os.environ["VEYA_ROLE_SKILLS_RUNTIME"] = "1"
os.environ["VEYA_TEACH_SKILL"] = "1"
os.environ["VEYA_EVENT_TRIGGERS"] = "1"
os.environ["VEYA_ROUTINE_TRIGGERS"] = "1"
os.environ["VEYA_ROUTINE_CLOCK"] = "1"
os.environ["VEYA_APPROVAL_GATES"] = "1"
os.environ["VEYA_TURN_QUEUE"] = "1"
os.environ["VEYA_EVIDENCE_REPORT"] = "1"
os.environ["VEYA_PUBLIC_SIGNUP"] = "1"
os.environ["VEYA_MCP_PRESETS"] = "1"
os.environ["VEYA_EVO_ONTOLOGY"] = "1"
os.environ["VEYA_SKILL_TEACHING"] = "1"
os.environ["VEYA_SKILL_V2"] = "1"
os.environ["VEYA_MEMORY_V2"] = "1"
os.environ["VEYA_MEMORY_CANDIDATES"] = "1"
os.environ["VEYA_CONTINUITY_V1"] = "1"
os.environ["VEYA_PERSONAL_CONTEXT_UI"] = "1"
os.environ["VEYA_LONG_TERM_LEARNING"] = "1"

from server.app import app
from fastapi.routing import APIRoute

# Recursively unwrap all routes
def get_all_routes(router_or_app):
    results = []
    routes = getattr(router_or_app, "routes", [])
    for r in routes:
        if isinstance(r, APIRoute):
            results.append(r)
        elif hasattr(r, "routes"):
            results.extend(get_all_routes(r))
        elif hasattr(r, "app") and hasattr(r.app, "routes"):
            results.extend(get_all_routes(r.app))
    return results

all_api_routes = get_all_routes(app)

route_auth_map = {}
for r in all_api_routes:
    deps = []
    for d in r.dependencies:
        dep_name = getattr(d.dependency, "__name__", str(d.dependency))
        deps.append(dep_name)
    try:
        sig = inspect.signature(r.endpoint)
        for p in sig.parameters.values():
            if hasattr(p.default, "dependency") and p.default.dependency:
                dep_name = getattr(p.default.dependency, "__name__", str(p.default.dependency))
                deps.append(dep_name)
    except Exception:
        pass
    for m in r.methods:
        route_auth_map[(m.upper(), r.path)] = sorted(set(deps))

print(f"Total backend APIRoutes found: {len(all_api_routes)}")
print(f"Total OpenAPI path items: {len(paths)}")

# 3. Scan frontend calls in apps/web/src
web_dir = Path("apps/web/src")
frontend_calls = []

# Pattern 1: api("gateway", ...)
api_regex = re.compile(
    r'api\(\s*["\'](gateway|legacy)["\']\s*,\s*([`"\'][^,\)]+[`"\'])(?:\s*,\s*(\{.*?\})\s*)?\s*\)',
    re.DOTALL
)

# Pattern 2: fetch(...)
fetch_regex = re.compile(
    r'fetch\(\s*([`"\'][^,\)]+[`"\']|\$\{API_BASE\}[^,\)]+)\s*(?:,\s*(\{.*?\})\s*)?\s*\)',
    re.DOTALL
)

for file_path in web_dir.rglob("*"):
    if file_path.suffix in (".ts", ".svelte", ".js") and "node_modules" not in str(file_path):
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        # Search for api("gateway", ...)
        for match in api_regex.finditer(content):
            up = match.group(1)
            raw_path = match.group(2).strip("`\"'")
            opts = match.group(3) or ""
            method = "POST"
            m = re.search(r'method\s*:\s*["\'](GET|POST|PUT|PATCH|DELETE)["\']', opts)
            if m:
                method = m.group(1)
            frontend_calls.append({
                "source": str(file_path.relative_to(web_dir)),
                "upstream": up,
                "method": method,
                "raw_path": raw_path,
                "options": opts.strip()
            })
        # Search for fetch(...)
        for match in fetch_regex.finditer(content):
            raw_path = match.group(1).strip("`\"'")
            opts = match.group(2) or ""
            method = "GET"
            m = re.search(r'method\s*:\s*["\'](GET|POST|PUT|PATCH|DELETE)["\']', opts)
            if m:
                method = m.group(1)
            elif "body" in opts:
                method = "POST"
            clean_path = raw_path.replace("${API_BASE}", "").replace("${base}", "").strip("`\"'")
            frontend_calls.append({
                "source": str(file_path.relative_to(web_dir)),
                "upstream": "fetch",
                "method": method,
                "raw_path": clean_path,
                "options": opts.strip()
            })

print(f"Total frontend calls scanned: {len(frontend_calls)}")

# Output collected info for analysis
with open("evidence/frontend_calls.json", "w") as f:
    json.dump(frontend_calls, f, indent=2)

with open("evidence/backend_routes.json", "w") as f:
    route_list = []
    for (m, p), deps in route_auth_map.items():
        route_list.append({"method": m, "path": p, "auth_deps": deps})
    json.dump(route_list, f, indent=2)

print("Saved evidence/frontend_calls.json and evidence/backend_routes.json")

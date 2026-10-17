#!/usr/bin/env python3
"""AUD3 route matrix: enumerate every FastAPI route with its auth dependency.

Unwraps FastAPI's ``_IncludedRouter`` wrappers (this build's router shim) so the
matrix is complete, then joins against the OpenAPI spec served by the live
container to record what is actually exposed.
"""

from __future__ import annotations

import inspect
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

BASE = os.environ.get("AUD3_BASE", "http://127.0.0.1:8799")
OUT = Path("evidence/route_matrix.json")

# All feature flags the audit turns on (mirrors .aud3data/stage.env extras).
for name in (
    "VEYA_ROLE_SKILLS_RUNTIME", "VEYA_TEACH_SKILL", "VEYA_EVENT_TRIGGERS",
    "VEYA_ROUTINE_TRIGGERS", "VEYA_ROUTINE_CLOCK", "VEYA_APPROVAL_GATES",
    "VEYA_TURN_QUEUE", "VEYA_EVIDENCE_REPORT", "VEYA_PUBLIC_SIGNUP",
    "VEYA_MCP_PRESETS", "VEYA_EVO_ONTOLOGY", "VEYA_SKILL_TEACHING",
    "VEYA_SKILL_V2", "VEYA_MEMORY_V2", "VEYA_MEMORY_CANDIDATES",
    "VEYA_CONTINUITY_V1", "VEYA_PERSONAL_CONTEXT_UI", "VEYA_LONG_TERM_LEARNING",
    "VEYA_CHANNELS", "VEYA_MORNING_FEED", "VEYA_PROVIDER_REGISTRY",
    "VEYA_BOT_SECRETS", "VEYA_GROUP_ROLES", "VEYA_BOT_MEMORY_MD",
    "VEYA_REMOTE_CONTROL", "VEYA_ROLE_SKILL_PRESETS", "VEYA_SSH_HOSTS",
    "VEYA_PROJECT_BRAIN", "VEYA_TDX_NEEDS_YOU", "VEYA_TDX_AGENT_PUBLISH_PERMS",
    "VEYA_TDX_RUN_HISTORY", "VEYA_MAB_BROWSER", "VEYA_XRT_ENABLED",
    "VEYA_DEVICE_PAIRING", "VEYA_SKILL_EVAL_GATE",
):
    os.environ.setdefault(name, "1")

from fastapi.routing import APIRoute  # noqa: E402

from server.app import app  # noqa: E402

AUTH_NAMES = {
    "require_user": "require_user",
    "require_owner": "require_owner",
    "get_current_user": "get_current_user(optional)",
    "_require_user": "_require_user(wrapper)",
    "_require_owned_bot": "_require_owned_bot(owner+bot)",
    "require_tailnet_owner": "require_tailnet_owner",
}


def _dep_names(route: APIRoute) -> list[str]:
    names: list[str] = []
    for dep in route.dependant.dependencies:
        fn = dep.call
        names.append(getattr(fn, "__name__", str(fn)))
        # nested dependencies (router-level deps land here too)
        for sub in dep.dependencies:
            names.append(getattr(sub.call, "__name__", str(sub.call)))
    for dep in getattr(route, "dependencies", []) or []:
        names.append(getattr(dep.dependency, "__name__", str(dep.dependency)))
    return names


def _auth_label(names: list[str]) -> str:
    for raw in names:
        if raw in AUTH_NAMES:
            return AUTH_NAMES[raw]
    return "public/none"


def walk(router: Any, prefix: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for route in getattr(router, "routes", []):
        if isinstance(route, APIRoute):
            names = _dep_names(route)
            path = route.path
            # A nested router may already carry the full path (its own prefix
            # was absolute); only prepend when the parent prefix is not already
            # part of the path.
            if prefix and not path.startswith(prefix):
                path = prefix + path
            rows.append(
                {
                    "method": sorted(route.methods - {"HEAD", "OPTIONS"}),
                    "path": path,
                    "auth": _auth_label(names),
                    "auth_deps": sorted(set(names)),
                    "name": route.name,
                }
            )
        elif type(route).__name__ == "_IncludedRouter":
            # ``include_context.prefix`` already holds the FULL prefix the
            # parent router was mounted at (absolute, not additive).
            ctx = getattr(route, "include_context", None)
            ctx_prefix = getattr(ctx, "prefix", "") or ""
            rows.extend(walk(route.original_router, ctx_prefix or prefix))
    return rows


rows = walk(app.router)

# Flatten one row per (method, path)
flat: list[dict[str, Any]] = []
for row in rows:
    for m in row["method"]:
        flat.append({**row, "method": m})

with urllib.request.urlopen(f"{BASE}/openapi.json") as resp:
    openapi = json.loads(resp.read().decode())
spec_paths = openapi.get("paths", {})

# OpenAPI normalises path converters (``{p:path}`` -> ``{p}``); match on that form.
normalized = {p.replace(":path", ""): v for p, v in spec_paths.items()}
for row in flat:
    row["spec_path"] = row["path"].replace(":path", "")
    entry = normalized.get(row["spec_path"])
    row["in_openapi"] = bool(entry and row["method"].lower() in entry)

flat.sort(key=lambda r: (r["path"], r["method"]))
OUT.write_text(json.dumps(flat, indent=2) + "\n")

print(f"routes enumerated: {len(flat)}")
print(f"unique paths: {len({r['path'] for r in flat})}")
print(f"in openapi: {sum(1 for r in flat if r['in_openapi'])}")

from collections import Counter

for label, count in Counter(r["auth"] for r in flat).most_common():
    print(f"  auth={label}: {count}")

"""Tests for AUD1: Team & Projects auth state, cookie synchronization, and return navigation."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.routes.projects import router as projects_router


def test_projects_endpoints_handle_unauthenticated_none_user(monkeypatch: pytest.MonkeyPatch):
    """When user is unauthenticated (get_current_user returns None), routes must not crash with 500."""
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "1")
    app = FastAPI()
    app.include_router(projects_router)
    app.dependency_overrides[auth_mod.get_current_user] = lambda: None

    client = TestClient(app, raise_server_exceptions=False)

    # list_projects should succeed (200) for unauthenticated user, showing only public/empty list
    res = client.get("/api/v1/projects")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    assert "projects" in res.json()

    # create_project must require login (401), not crash with 500 AttributeError
    res_post = client.post("/api/v1/projects", json={"name": "New Proj"})
    assert res_post.status_code == 401, f"Expected 401, got {res_post.status_code}: {res_post.text}"

    # get_project, brief, tasks on non-existent or inaccessible project must return 404, not 500
    assert client.get("/api/v1/projects/nonexistent").status_code == 404
    assert client.get("/api/v1/projects/nonexistent/brief").status_code == 404
    assert client.get("/api/v1/projects/nonexistent/tasks").status_code == 404


def test_web_api_proxy_exports_patch():
    """Proxy at apps/web/src/routes/api/v1/[...path]/+server.ts must export PATCH."""
    proxy_src = Path("apps/web/src/routes/api/v1/[...path]/+server.ts").read_text(encoding="utf-8")
    assert "export const PATCH" in proxy_src, "api/v1 proxy must export PATCH handler"


def test_projects_client_api_specifies_get_method():
    """All GET endpoints in apps/web/src/lib/projects/api.ts must explicitly specify method: 'GET'."""
    src = Path("apps/web/src/lib/projects/api.ts").read_text(encoding="utf-8")
    assert 'api("gateway", "api/v1/projects", { method: "GET" })' in src
    assert 'api("gateway", `api/v1/projects/${encodeURIComponent(id)}`, { method: "GET" })' in src
    assert (
        'api("gateway", `api/v1/projects/${encodeURIComponent(id)}/brief`, { method: "GET" })'
        in src
    )
    assert (
        'api("gateway", `api/v1/projects/${encodeURIComponent(id)}/messages`, { method: "GET" })'
        in src
    )
    assert (
        'api("gateway", `api/v1/projects/${encodeURIComponent(id)}/tasks`, { method: "GET" })'
        in src
    )


def test_auth_client_sets_and_clears_cookie():
    """apps/web/src/lib/auth.svelte.ts must manage document.cookie for veya_token and veya.auth.token."""
    src = Path("apps/web/src/lib/auth.svelte.ts").read_text(encoding="utf-8")
    assert "veya_token=" in src
    assert "veya.auth.token=" in src
    assert "document.cookie" in src


def test_team_page_has_return_to_home_navigation():
    """Team page and subpages must have return-to-home navigation links."""
    team_src = Path("apps/web/src/routes/team/+page.svelte").read_text(encoding="utf-8")
    assert 'href="/"' in team_src, "Team front door must have a link back to home (/)"
    assert "返回控制台" in team_src, "Team front door must have return to console text"


def test_top_level_pages_have_return_to_home():
    """All top-level pages must have return-to-home links."""
    pages = [
        "apps/web/src/routes/remote/+page.svelte",
        "apps/web/src/routes/approvals/+page.svelte",
        "apps/web/src/routes/missions/+page.svelte",
        "apps/web/src/routes/settings/channels/+page.svelte",
        "apps/web/src/routes/settings/agent-publish-perms/+page.svelte",
        "apps/web/src/routes/mab/+page.svelte",
    ]
    for p in pages:
        src = Path(p).read_text(encoding="utf-8")
        assert 'href="/"' in src, f"{p} must have a link back to home (/)"
        assert "返回控制台" in src or "返回" in src, f"{p} must have return text"

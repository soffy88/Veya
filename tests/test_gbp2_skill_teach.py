"""GBP2: Teach once save as skill test suite (VEYA_TEACH_SKILL).

Tests:
- Flag off: endpoints return 404 / disabled.
- Flag on: generate skill draft from trajectory data (has "需要你补的判断规则").
- Unapproved skill draft cannot be loaded/executed.
- Approved skill draft via ApprovalService becomes active and can be loaded/executed.
"""

from __future__ import annotations

import asyncio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.routes.skill_teach import router as skill_teach_router


def test_teach_skill_flag_off(monkeypatch):
    monkeypatch.setenv("VEYA_TEACH_SKILL", "0")
    app = FastAPI()
    app.include_router(skill_teach_router)
    app.dependency_overrides[auth_mod.require_user] = lambda: {"user_id": "user-1", "username": "user-1"}
    client = TestClient(app)
    resp = client.post("/api/v1/skills/teach/draft", json={"name": "test", "description": "test"})
    assert resp.status_code == 404


@pytest.fixture()
def teach_client(monkeypatch, tmp_path):
    from server import approvals as approvals_mod
    from veya.obase.approvals import ApprovalStore
    from runtime.personal.runtime import PersonalRuntimeStore, reset_personal_runtime

    monkeypatch.setenv("VEYA_TEACH_SKILL", "1")
    monkeypatch.setenv("VEYA_APPROVAL_GATES", "1")
    db_path = str(tmp_path / "personal.db")
    app_db = str(tmp_path / "approvals.db")
    
    monkeypatch.setenv("VEYA_EXECUTION_SQLITE_PATH", db_path)
    reset_personal_runtime()
    
    store = ApprovalStore(app_db)
    approvals_mod.configure_service(store=store)
    
    app = FastAPI()
    app.include_router(skill_teach_router)
    from server.approvals import router as approvals_router
    app.include_router(approvals_router)
    from server.routes.personal_runtime import router as personal_router
    app.include_router(personal_router)
    
    app.dependency_overrides[auth_mod.require_user] = lambda: {"user_id": "user-1", "username": "user-1"}
    app.dependency_overrides[auth_mod.get_current_user] = lambda: {"user_id": "user-1", "username": "user-1"}
    
    client = TestClient(app)
    yield client, store
    reset_personal_runtime()


@pytest.mark.asyncio
async def test_teach_skill_workflow(teach_client):
    client, store = teach_client
    
    trajectory_data = {
        "task_id": "task-abc-1",
        "objective": "Scrape stock price and send email",
        "tool_calls": [{"tool": "fetch_url", "args": {"url": "https://example.com"}}],
        "outcome": "completed"
    }
    
    resp = client.post(
        "/api/v1/skills/teach/draft",
        json={
            "name": "stock_scraper",
            "description": "Scrapes stock price from web",
            "trajectory": trajectory_data,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "需要你补的判断规则" in data.get("description", "")
    assert data.get("status") in {"draft", "candidate"}
    
    skill_id = data["skill_id"]
    version_id = data["version_id"]
    approval_id = data.get("approval_id")
    assert approval_id is not None
    
    # Also test list drafts endpoint
    list_resp = client.get("/api/v1/skills/teach/drafts")
    assert list_resp.status_code == 200
    assert list_resp.json().get("count") >= 1

    # 2. Unapproved skill cannot be loaded/executed
    run_resp = client.post(
        f"/api/v1/skills/{skill_id}/run",
        json={"params": {}},
    )
    assert run_resp.status_code in {400, 403, 404, 409}
    
    # 3. Approve via ApprovalService (decision: always)
    approval_resp = client.post(
        f"/api/v1/approvals/{approval_id}/resolve",
        json={"decision": "always", "reason": "Looks good"},
    )
    assert approval_resp.status_code == 200
    
    # 4. Now approved skill is active and can be run / loaded
    run_resp2 = client.post(
        f"/api/v1/skills/{skill_id}/run",
        json={"params": {}},
    )
    assert run_resp2.status_code in {200, 201, 204}

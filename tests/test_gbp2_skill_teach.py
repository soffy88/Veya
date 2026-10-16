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
from fastapi.testclient import TestClient

pytestmark = pytest.mark.asyncio


def test_teach_skill_flag_off(monkeypatch):
    from server.app import app
    from server import feature_flags

    monkeypatch.setenv("VEYA_TEACH_SKILL", "0")
    client = TestClient(app)
    resp = client.post("/api/v1/skills/teach/draft", json={"objective": "test"})
    assert resp.status_code in {404, 403, 400}


@pytest.fixture()
def teach_wired(monkeypatch, tmp_path):
    from server import feature_flags
    from server.app import app
    from server import approvals as approvals_mod
    from veya.obase.approvals import ApprovalStore
    from runtime.personal import configure_personal_runtime, PersonalRuntime

    monkeypatch.setenv("VEYA_TEACH_SKILL", "1")
    monkeypatch.setenv("VEYA_APPROVAL_GATES", "1")
    db_path = str(tmp_path / "personal.db")
    app_db = str(tmp_path / "approvals.db")
    
    store = ApprovalStore(app_db)
    approvals_mod.configure_service(store=store)
    
    pr = PersonalRuntime(db_path)
    configure_personal_runtime(pr)
    
    client = TestClient(app)
    yield client, pr, store


async def test_teach_skill_workflow(teach_wired):
    client, pr, store = teach_wired
    
    # 1. Create skill draft from trajectory
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
        headers={"Authorization": "Bearer test", "user_id": "user-1"}
    )
    # If endpoint not yet implemented, this will fail (RED)
    assert resp.status_code == 201
    data = resp.json()
    assert "需要你补的判断规则" in data.get("description", "")
    assert data.get("status") in {"draft", "candidate"}
    
    skill_id = data["skill_id"]
    version_id = data["version_id"]
    approval_id = data.get("approval_id")
    assert approval_id is not None
    
    # 2. Unapproved skill cannot be loaded/executed
    run_resp = client.post(
        f"/api/v1/skills/{skill_id}/run",
        json={"params": {}},
        headers={"Authorization": "Bearer test", "user_id": "user-1"}
    )
    assert run_resp.status_code in {400, 403, 404, 409}
    
    # 3. Approve via ApprovalService
    approval_resp = client.post(
        f"/api/v1/approvals/{approval_id}/resolve",
        json={"decision": "allow", "reason": "Looks good"},
        headers={"Authorization": "Bearer test", "user_id": "user-1"}
    )
    assert approval_resp.status_code == 200
    
    # 4. Now approved skill is active and can be run / loaded
    run_resp2 = client.post(
        f"/api/v1/skills/{skill_id}/run",
        json={"params": {}},
        headers={"Authorization": "Bearer test", "user_id": "user-1"}
    )
    assert run_resp2.status_code in {200, 201, 204}

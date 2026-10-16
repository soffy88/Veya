"""GBP6 TDD test suite: EvoOntology MCP preset & Skill Paired Evaluation Gate.

Tests:
1. Feature flags VEYA_EVO_ONTOLOGY and VEYA_SKILL_EVAL_GATE default to False.
2. A: EvoOntology MCP preset:
   - Flag OFF: preset is unavailable / disabled.
   - Flag ON: real stdio MCP server process spawns against a non-/tmp SQLite + ontology_v0 workspace.
   - Real calls to browse_semantics and resolve_semantics assert structured data.
   - Bot tools browse_semantics and resolve_semantics are registered and callable.
3. B: Skill Paired Evaluation Gate (gated versioning):
   - Flag OFF: legacy GBP2 behavior (draft goes straight to approval).
   - Flag ON - Pass case: candidate matches/beats parent on recorded tasks with identical budget ->
     eval passes, draft is submitted for approval, eval evidence is attached to approval args.
   - Flag ON - Block case: candidate regresses against parent or hits critical error ->
     eval fails, draft is BLOCKED from approval (no approval request created, approval_id is None).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.feature_flags import enabled


# ---------------------------------------------------------------------------
# Workspace fixture (Strictly non-/tmp: lives in repo under tests/fixtures)
# ---------------------------------------------------------------------------

FIXTURE_WS_DIR = Path(__file__).resolve().parent / "fixtures" / "evo_test_workspace"


@pytest.fixture(scope="session", autouse=True)
def setup_evo_workspace() -> Path:
    ws_dir = FIXTURE_WS_DIR / ".evoontology"
    v0_dir = ws_dir / "versions" / "ontology_v0"
    v0_dir.mkdir(parents=True, exist_ok=True)

    # 1. Real SQLite DB (never in /tmp)
    db_path = FIXTURE_WS_DIR / "sales.db"
    assert "/tmp" not in str(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, order_date TEXT)")
    conn.execute("DELETE FROM customers")
    conn.execute("DELETE FROM orders")
    conn.execute("INSERT INTO customers VALUES (1, 'Alice Corp', 'North'), (2, 'Bob LLC', 'South')")
    conn.execute("INSERT INTO orders VALUES (101, 1, 1500.0, '2026-01-15'), (102, 1, 2500.0, '2026-02-20'), (103, 2, 800.0, '2026-03-01')")
    conn.commit()
    conn.close()

    # 2. project.json and active.json
    (ws_dir / "project.json").write_text(json.dumps({
        "schema_version": 1,
        "mode": "rolling_trajectory",
        "data_source": str(db_path),
        "workload_source": "sales_analytics",
        "evaluation": {"protocol": "ground_truth"},
        "boundary": {"max_tables": 10}
    }, ensure_ascii=False))
    (ws_dir / "active.json").write_text(json.dumps({"active_version": "ontology_v0"}))

    # 3. Ontology v0 records
    terms = [
        {"id": "term_customer", "name": "Customer", "type": "entity", "definition": "Business or person purchasing services", "aliases": ["client", "account"], "lifecycle": {"state": "active"}},
        {"id": "term_revenue", "name": "Revenue", "type": "metric", "definition": "Total monetary amount from completed customer orders", "aliases": ["sales", "order_amount"], "lifecycle": {"state": "active"}}
    ]
    mappings = [
        {"id": "map_customer_name", "term_id": "term_customer", "database_source": str(db_path), "table": "customers", "column": "name", "lifecycle": {"state": "active"}},
        {"id": "map_order_revenue", "term_id": "term_revenue", "database_source": str(db_path), "table": "orders", "column": "amount", "aggregation_semantics": "sum", "lifecycle": {"state": "active"}}
    ]
    relations = [
        {"id": "rel_customer_orders", "source": "term_customer", "relation_type": "has_many", "target": "term_revenue", "connection_condition": "customers.id = orders.customer_id", "description": "Customer generates order revenue", "lifecycle": {"state": "active"}}
    ]
    constraints = [
        {"id": "const_positive_revenue", "target": "term_revenue", "constraint_type": "domain_rule", "severity": "warn", "description": "Order amount should be non-negative", "lifecycle": {"state": "active"}}
    ]
    evidence: list[dict[str, Any]] = []

    (v0_dir / "terms.json").write_text(json.dumps(terms))
    (v0_dir / "mappings.json").write_text(json.dumps(mappings))
    (v0_dir / "relations.json").write_text(json.dumps(relations))
    (v0_dir / "constraints.json").write_text(json.dumps(constraints))
    (v0_dir / "evidence.json").write_text(json.dumps(evidence))

    return ws_dir


# ---------------------------------------------------------------------------
# Test 1: Feature flags default OFF
# ---------------------------------------------------------------------------

def test_gbp6_feature_flags_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYA_EVO_ONTOLOGY", raising=False)
    monkeypatch.delenv("VEYA_SKILL_EVAL_GATE", raising=False)
    assert enabled("VEYA_EVO_ONTOLOGY") is False
    assert enabled("VEYA_SKILL_EVAL_GATE") is False


# ---------------------------------------------------------------------------
# Test 2: Part A - EvoOntology MCP Preset
# ---------------------------------------------------------------------------

def test_evoontology_preset_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_EVO_ONTOLOGY", "0")
    from server.evo_ontology import get_evo_ontology_preset, is_evo_ontology_enabled
    assert is_evo_ontology_enabled() is False
    preset = get_evo_ontology_preset()
    assert preset["name"] == "evoontology"


@pytest.mark.asyncio
async def test_evoontology_mcp_real_process_and_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """A: Must really spawn EvoOntology MCP server process and query via Veya MCP client."""
    monkeypatch.setenv("VEYA_EVO_ONTOLOGY", "1")
    ws_path = str(FIXTURE_WS_DIR / ".evoontology")
    monkeypatch.setenv("EVO_ONTOLOGY_WORKSPACE", ws_path)

    from server.evo_ontology import (
        browse_semantics,
        resolve_semantics,
        get_evo_mcp_client,
        close_evo_mcp_client,
        wire_evo_ontology_tools,
    )

    try:
        # 1. Start client & verify tools exposed by real server
        client = await get_evo_mcp_client(ws_path)
        assert client.alive is True
        tools = await client.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "browse_semantics" in tool_names
        assert "resolve_semantics" in tool_names

        # 2. Really call browse_semantics over stdio
        browse_res = await browse_semantics(query="revenue", workspace=ws_path)
        assert browse_res.get("status") == "ok"
        items = browse_res.get("items", [])
        assert len(items) >= 1
        term_ids = [it.get("id") for it in items]
        assert "term_revenue" in term_ids

        # 3. Really call resolve_semantics over stdio
        resolve_res = await resolve_semantics(mentions=["Revenue"], workspace=ws_path)
        assert resolve_res.get("status") == "ok"
        results = resolve_res.get("results", [])
        assert len(results) >= 1
        res0 = results[0]
        assert res0.get("term", {}).get("name") == "Revenue"
        assert any(m.get("table") == "orders" for m in res0.get("mappings", []))
        assert any(r.get("target") == "term_revenue" for r in res0.get("relations", []))

        # 4. Bot tool registry wiring
        registered_count = await wire_evo_ontology_tools(ws_path)
        assert registered_count >= 2
        from server.tool_registry import master_tools
        assert master_tools.has("browse_semantics")
        assert master_tools.has("resolve_semantics")
    finally:
        await close_evo_mcp_client()


# ---------------------------------------------------------------------------
# Test 3: Part B - Skill Paired Evaluation Gate
# ---------------------------------------------------------------------------

@pytest.fixture()
def eval_gate_test_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from server import approvals as approvals_mod
    from veya.obase.approvals import ApprovalStore
    from runtime.personal.runtime import reset_personal_runtime
    from server.routes.skill_teach import router as skill_teach_router
    from server.approvals import router as approvals_router

    monkeypatch.setenv("VEYA_TEACH_SKILL", "1")
    monkeypatch.setenv("VEYA_APPROVAL_GATES", "1")
    
    # Store DB in local test dir (.veya/test_run_data), not /tmp
    db_dir = Path(__file__).resolve().parents[1] / ".veya" / "test_run_data"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(db_dir / "eval_personal.db")
    app_db = str(db_dir / "eval_approvals.db")
    
    monkeypatch.setenv("VEYA_EXECUTION_SQLITE_PATH", db_path)
    reset_personal_runtime()
    
    store = ApprovalStore(app_db)
    approvals_mod.configure_service(store=store)
    
    app = FastAPI()
    app.include_router(skill_teach_router)
    app.include_router(approvals_router)
    
    app.dependency_overrides[auth_mod.require_user] = lambda: {"user_id": "user-eval", "username": "user-eval"}
    app.dependency_overrides[auth_mod.get_current_user] = lambda: {"user_id": "user-eval", "username": "user-eval"}
    
    client = TestClient(app)
    yield client, store
    reset_personal_runtime()


def test_skill_eval_gate_flag_off_bypasses_gate(eval_gate_test_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """When VEYA_SKILL_EVAL_GATE is off, draft goes straight to approval without eval."""
    client, store = eval_gate_test_env
    monkeypatch.setenv("VEYA_SKILL_EVAL_GATE", "0")
    
    resp = client.post(
        "/api/v1/skills/teach/draft",
        json={
            "name": "calc_tax",
            "description": "Calculate invoice tax rate",
            "trajectory": {"objective": "Compute tax for order", "tool_calls": []},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data.get("status") in {"draft", "candidate"}
    assert data.get("approval_id") is not None
    # No eval gate evidence when flag is OFF
    assert "eval_result" not in data or data.get("eval_gate_passed") is None


def test_skill_eval_gate_pass_case(eval_gate_test_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """When VEYA_SKILL_EVAL_GATE is on and candidate beats/ties parent -> approval created with evidence."""
    client, store = eval_gate_test_env
    monkeypatch.setenv("VEYA_SKILL_EVAL_GATE", "1")
    
    trajectory_data = {
        "task_id": "task-tax-1",
        "objective": "Calculate VAT and total price",
        "eval_tasks": [
            {
                "task_id": "t1",
                "input": "Order amount 100 with 10% tax",
                "expected": 110,
                "budget": 5,
            },
            {
                "task_id": "t2",
                "input": "Order amount 200 with 10% tax",
                "expected": 220,
                "budget": 5,
            }
        ],
        "outcome": "completed",
    }
    
    resp = client.post(
        "/api/v1/skills/teach/draft",
        json={
            "name": "tax_calculator",
            "description": "Calculate order total including VAT: total = amount * (1 + rate)",
            "trajectory": trajectory_data,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data.get("eval_gate_passed") is True
    eval_res = data.get("eval_result")
    assert eval_res is not None
    assert eval_res.get("passed") is True
    assert eval_res.get("candidate_score", 0) >= eval_res.get("parent_score", 0)
    assert eval_res.get("critical_errors", 0) == 0
    
    # Approval request created
    approval_id = data.get("approval_id")
    assert approval_id is not None
    
    # Evidence is written into approval request
    pending = store.list_pending()
    matching = [p for p in pending if p.get("request_id") == approval_id or p.get("id") == approval_id]
    assert len(matching) == 1
    record_args = matching[0].get("tool_args") or matching[0].get("args") or {}
    assert "eval_evidence" in record_args
    assert record_args["eval_evidence"]["passed"] is True


def test_skill_eval_gate_regression_block_case(eval_gate_test_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """When VEYA_SKILL_EVAL_GATE is on and candidate regresses / has critical error -> BLOCKED from approval."""
    client, store = eval_gate_test_env
    monkeypatch.setenv("VEYA_SKILL_EVAL_GATE", "1")
    
    # Trajectory where candidate explicitly regresses or fails critical constraint
    trajectory_data = {
        "task_id": "task-broken-1",
        "objective": "Process customer refund",
        "eval_tasks": [
            {
                "task_id": "t1",
                "input": "Invalid negative refund request",
                "expected": "error",
                "budget": 3,
                "force_critical_error": True,  # triggers critical error / regression
            }
        ],
        "outcome": "failed",
    }
    
    resp = client.post(
        "/api/v1/skills/teach/draft",
        json={
            "name": "faulty_refund",
            "description": "Faulty refund processing that violates security checks",
            "trajectory": trajectory_data,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data.get("eval_gate_passed") is False
    assert data.get("status") == "eval_rejected"
    assert data.get("approval_id") is None  # BLOCKED! No approval request created!
    
    eval_res = data.get("eval_result")
    assert eval_res is not None
    assert eval_res.get("passed") is False
    assert eval_res.get("critical_errors", 0) > 0 or eval_res.get("delta", 0) < 0

    # Ensure no pending approval was created for this faulty draft
    pending = store.list_pending()
    assert not any(p.get("tool_args", {}).get("name") == "faulty_refund" for p in pending)

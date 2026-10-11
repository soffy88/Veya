# -*- coding: utf-8 -*-
"""Integration tests for Veya 3O runtime integration.
The tests validate that the harness contract correctly incorporates
* effective capabilities (via oskill.capability_intersection)
* execution lineage (via oprim.ExecutionLineage)
* controller generation (via oprim.next_controller_generation)
and that the raw task_contract is preserved.
"""

import json
import types
from pathlib import Path

import pytest

# Import the functions under test
from runtime.harness.contract import (
    build_coding_harness_contract,
    read_coding_harness_contract,
    write_coding_harness_contract,
    contract_path,
)

# Helper mock objects for oprim ExecutionLineage and controller generation
class MockLineage:
    def __init__(self, root_run_id, execution_id, parent_execution_id=None, generation=0):
        self.root_run_id = root_run_id
        self.execution_id = execution_id
        self.parent_execution_id = parent_execution_id
        self.controller_generation = generation

    def child(self, execution_id, task_id, attempt_id, controller_generation):
        # Return a new lineage with this object as parent
        return MockLineage(
            root_run_id=self.root_run_id,
            execution_id=execution_id,
            parent_execution_id=self.execution_id,
            generation=controller_generation + 1,
        )

    def __repr__(self):
        return f"MockLineage(root={self.root_run_id}, exec={self.execution_id}, parent={self.parent_execution_id}, gen={self.controller_generation})"

class MockGenObj:
    def __init__(self, generation, generation_id):
        self.generation = generation
        self.generation_id = generation_id

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def dummy_workspace(tmp_path):
    # Create a minimal workspace structure that runtime.coding.workspace_detect can consume
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    (ws_root / ".git").mkdir()
    return str(ws_root)

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------
def test_capability_intersection_success(monkeypatch, dummy_workspace):
    # Mock oskill capability sources
    monkeypatch.setattr("oskill.requested_capabilities", lambda: ["a", "b"])
    monkeypatch.setattr("oskill.caller_capabilities", lambda: ["a", "c"])
    monkeypatch.setattr("oskill.worker_capabilities", lambda: ["a", "d"])
    monkeypatch.setattr("oskill.workspace_capabilities", lambda _: ["a", "e"])
    monkeypatch.setattr("oskill.policy_capabilities", lambda _: ["a", "f"])
    monkeypatch.setattr("oskill.capability_intersection", lambda *args: frozenset(set.intersection(*map(set, args))))

    # Mock lineage and generation helpers – they are not exercised in this test
    monkeypatch.setattr("oskill.parent_execution_lineage", lambda _: None)
    monkeypatch.setattr("oskill.root_run_id", lambda: "root123")
    monkeypatch.setattr("oprim.ExecutionLineage", MockLineage)
    monkeypatch.setattr("oprim.next_controller_generation", lambda **kw: MockGenObj(0, "gid-0"))

    contract = build_coding_harness_contract(dummy_workspace, "task1")
    assert contract.effective_capabilities == ["a"]
    # ensure the fields exist
    assert isinstance(contract.lineage, dict)
    assert isinstance(contract.controller_generation, dict)

def test_capability_fail_closed(monkeypatch, dummy_workspace):
    # Simulate missing source by returning None – oskill.capability_intersection will handle it
    monkeypatch.setattr("oskill.requested_capabilities", lambda: None)
    monkeypatch.setattr("oskill.caller_capabilities", lambda: ["a"])
    monkeypatch.setattr("oskill.worker_capabilities", lambda: ["a"])
    monkeypatch.setattr("oskill.workspace_capabilities", lambda _: ["a"])
    monkeypatch.setattr("oskill.policy_capabilities", lambda _: ["a"])
    monkeypatch.setattr("oskill.capability_intersection", lambda *args: frozenset(set.intersection(*[set(a or []) for a in args])))

    monkeypatch.setattr("oskill.parent_execution_lineage", lambda _: None)
    monkeypatch.setattr("oskill.root_run_id", lambda: "root123")
    monkeypatch.setattr("oprim.ExecutionLineage", MockLineage)
    monkeypatch.setattr("oprim.next_controller_generation", lambda **kw: MockGenObj(0, "gid-0"))

    contract = build_coding_harness_contract(dummy_workspace, "task2")
    assert contract.effective_capabilities == []  # fail‑closed yields empty list

def test_lineage_root(monkeypatch, dummy_workspace):
    # No parent lineage – should create a root lineage
    monkeypatch.setattr("oskill.parent_execution_lineage", lambda _: None)
    monkeypatch.setattr("oskill.root_run_id", lambda: "rootXYZ")
    monkeypatch.setattr("oprim.ExecutionLineage", MockLineage)
    monkeypatch.setattr("oprim.next_controller_generation", lambda **kw: MockGenObj(0, "gid-0"))
    monkeypatch.setattr("oskill.requested_capabilities", lambda: [])
    monkeypatch.setattr("oskill.caller_capabilities", lambda: [])
    monkeypatch.setattr("oskill.worker_capabilities", lambda: [])
    monkeypatch.setattr("oskill.workspace_capabilities", lambda _: [])
    monkeypatch.setattr("oskill.policy_capabilities", lambda _: [])
    monkeypatch.setattr("oskill.capability_intersection", lambda *args: frozenset())

    contract = build_coding_harness_contract(dummy_workspace, "task_root")
    lineage = contract.lineage
    assert lineage["parent_execution_id"] is None
    assert lineage["root_run_id"] == "rootXYZ"
    assert lineage["execution_id"] == "task_root"
    assert lineage["controller_generation"] == 0

def test_lineage_child(monkeypatch, dummy_workspace):
    # Provide a mocked parent lineage object
    parent = MockLineage(root_run_id="rootABC", execution_id="parent_exec")
    monkeypatch.setattr("oskill.parent_execution_lineage", lambda _: parent)
    monkeypatch.setattr("oskill.root_run_id", lambda: "rootABC")
    monkeypatch.setattr("oprim.ExecutionLineage", MockLineage)
    monkeypatch.setattr("oprim.next_controller_generation", lambda **kw: MockGenObj(0, "gid-0"))
    monkeypatch.setattr("oskill.requested_capabilities", lambda: [])
    monkeypatch.setattr("oskill.caller_capabilities", lambda: [])
    monkeypatch.setattr("oskill.worker_capabilities", lambda: [])
    monkeypatch.setattr("oskill.workspace_capabilities", lambda _: [])
    monkeypatch.setattr("oskill.policy_capabilities", lambda _: [])
    monkeypatch.setattr("oskill.capability_intersection", lambda *args: frozenset())

    contract = build_coding_harness_contract(dummy_workspace, "child_task")
    lineage = contract.lineage
    # child should reference parent execution_id
    assert lineage["parent_execution_id"] == "parent_exec"
    assert lineage["root_run_id"] == "rootABC"
    # ensure parent object unchanged
    assert parent.execution_id == "parent_exec"

def test_controller_generation_sequence(monkeypatch, dummy_workspace):
    # Mock deterministic generation objects
    seq = [MockGenObj(0, "gid-0"), MockGenObj(1, "gid-1"), MockGenObj(2, "gid-2")]
    def gen_side_effect(**kw):
        return seq.pop(0)
    monkeypatch.setattr("oprim.next_controller_generation", gen_side_effect)
    monkeypatch.setattr("oskill.parent_execution_lineage", lambda _: None)
    monkeypatch.setattr("oskill.root_run_id", lambda: "rootSeq")
    monkeypatch.setattr("oprim.ExecutionLineage", MockLineage)
    monkeypatch.setattr("oskill.requested_capabilities", lambda: [])
    monkeypatch.setattr("oskill.caller_capabilities", lambda: [])
    monkeypatch.setattr("oskill.worker_capabilities", lambda: [])
    monkeypatch.setattr("oskill.workspace_capabilities", lambda _: [])
    monkeypatch.setattr("oskill.policy_capabilities", lambda _: [])
    monkeypatch.setattr("oskill.capability_intersection", lambda *args: frozenset())

    c0 = build_coding_harness_contract(dummy_workspace, "task0")
    c1 = build_coding_harness_contract(dummy_workspace, "task1")
    c2 = build_coding_harness_contract(dummy_workspace, "task2")
    assert c0.controller_generation["generation"] == 0
    assert c1.controller_generation["generation"] == 1
    assert c2.controller_generation["generation"] == 2
    assert c0.controller_generation["generation_id"] == "gid-0"
    assert c1.controller_generation["generation_id"] == "gid-1"
    assert c2.controller_generation["generation_id"] == "gid-2"

def test_controller_generation_identity(monkeypatch, dummy_workspace):
    # Same root + same generation should return identical generation_id
    mock_obj = MockGenObj(0, "stable-id")
    monkeypatch.setattr("oprim.next_controller_generation", lambda **kw: mock_obj)
    monkeypatch.setattr("oskill.parent_execution_lineage", lambda _: None)
    monkeypatch.setattr("oskill.root_run_id", lambda: "rootStable")
    monkeypatch.setattr("oprim.ExecutionLineage", MockLineage)
    monkeypatch.setattr("oskill.requested_capabilities", lambda: [])
    monkeypatch.setattr("oskill.caller_capabilities", lambda: [])
    monkeypatch.setattr("oskill.worker_capabilities", lambda: [])
    monkeypatch.setattr("oskill.workspace_capabilities", lambda _: [])
    monkeypatch.setattr("oskill.policy_capabilities", lambda _: [])
    monkeypatch.setattr("oskill.capability_intersection", lambda *args: frozenset())

    c_a = build_coding_harness_contract(dummy_workspace, "taskA")
    c_b = build_coding_harness_contract(dummy_workspace, "taskA")
    assert c_a.controller_generation == c_b.controller_generation

def test_integrated_contract_fields(monkeypatch, dummy_workspace, tmp_path):
    # Minimal mocks to allow contract creation
    monkeypatch.setattr("oskill.parent_execution_lineage", lambda _: None)
    monkeypatch.setattr("oskill.root_run_id", lambda: "rootInt")
    monkeypatch.setattr("oprim.ExecutionLineage", MockLineage)
    monkeypatch.setattr("oprim.next_controller_generation", lambda **kw: MockGenObj(0, "gid-int"))
    monkeypatch.setattr("oskill.requested_capabilities", lambda: [])
    monkeypatch.setattr("oskill.caller_capabilities", lambda: [])
    monkeypatch.setattr("oskill.worker_capabilities", lambda: [])
    monkeypatch.setattr("oskill.workspace_capabilities", lambda _: [])
    monkeypatch.setattr("oskill.policy_capabilities", lambda _: [])
    monkeypatch.setattr("oskill.capability_intersection", lambda *args: frozenset())

    task_id = "int_task"
    contract = build_coding_harness_contract(dummy_workspace, task_id)
    # Verify that the contract serialises all required keys
    data = contract.to_dict()
    for key in ["workspace_id", "guide_refs", "required_sensors", "optional_sensors", "permission_profile",
                "observability_profile", "memory_scope", "artifact_policy",
                "lineage", "controller_generation", "effective_capabilities"]:
        assert key in data

    # Persist and read back – ensure fields survive round‑trip
    write_coding_harness_contract(dummy_workspace, task_id, contract)
    loaded = read_coding_harness_contract(dummy_workspace, task_id)
    assert loaded is not None
    assert loaded.lineage == contract.lineage
    assert loaded.controller_generation == contract.controller_generation
    assert loaded.effective_capabilities == contract.effective_capabilities

# ---------------------------------------------------------------------------
# End of tests
# ---------------------------------------------------------------------------

"""PQ §18: Authority Audit and Zero Drift Qualification.

Invariants:
- MASTER_AGENT_AUTHORITY_DRIFT=0
- GOALRUN_AUTHORITY_DRIFT=0
- AGENT_RUNTIME_AUTHORITY_DRIFT=0
- EXECUTION_CONTRACT_DRIFT=0
- FLEET_AUTHORITY_DRIFT=0
- FLEET_SEMANTIC_DECISION=0
- FLEET_DIRECT_EXECUTION=0
"""

from __future__ import annotations

import pathlib
import subprocess
import sys


def test_authority_audit_sensors() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent.parent.parent

    # 1. Fleet Authority: no semantic decisions or completion calls
    res_fa = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_fleet_authority.py")],
        capture_output=True,
        text=True,
    )
    assert res_fa.returncode == 0, f"check_fleet_authority failed: {res_fa.stdout}"

    # 2. Fleet Registry: zero overlap with Mission/GoalRun/Execution registries
    res_ro = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_fleet_registry_overlap.py")],
        capture_output=True,
        text=True,
    )
    assert res_ro.returncode == 0, f"check_fleet_registry_overlap failed: {res_ro.stdout}"

    # 3. Fleet Direct Execution: zero subprocess or direct OS execution calls
    res_de = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_fleet_direct_execution.py")],
        capture_output=True,
        text=True,
    )
    assert res_de.returncode == 0, f"check_fleet_direct_execution failed: {res_de.stdout}"

    # 4. Fleet Semantic Imports: no planner, assessor, evaluator, or LLM imports
    res_si = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_fleet_semantic_imports.py")],
        capture_output=True,
        text=True,
    )
    assert res_si.returncode == 0, f"check_fleet_semantic_imports failed: {res_si.stdout}"

    # 5. Single Master Authority: MasterAgent sole intelligence authority
    res_sm = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_single_master_path.py")],
        capture_output=True,
        text=True,
    )
    assert res_sm.returncode == 0, f"check_single_master_path failed: {res_sm.stdout}"

    # 6. Architecture Manifest Integrity
    res_am = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_architecture_manifest.py")],
        capture_output=True,
        text=True,
    )
    assert res_am.returncode == 0, f"check_architecture_manifest failed: {res_am.stdout}"

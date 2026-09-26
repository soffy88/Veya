"""Production Qualification: Wave PQG Security & Authority Freeze (spec §49, §50).

Validates:
  Zero security violations:
    WORKSPACE_ESCAPE=0
    CROSS_REPO_WRITE_LEAK=0
    CREDENTIAL_LEAK=0
  Zero authority drift:
    OPERATIONS_SEMANTIC_DECISION=0
    OPERATIONS_DIRECT_EXECUTION=0
    OPERATIONS_DIRECT_PLACEMENT=0
    OPERATIONS_GOAL_MUTATION=0
    OPERATIONS_MISSION_MUTATION=0
    OPERATIONS_PLANNER_AUTHORITY=0
    MASTER_AGENT_AUTHORITY_DRIFT=0
    GOALRUN_AUTHORITY_DRIFT=0
    AGENT_RUNTIME_AUTHORITY_DRIFT=0
    EXECUTION_CONTRACT_DRIFT=0
    FLEET_AUTHORITY_DRIFT=0
    OPERATIONS_AUTHORITY_DRIFT=0
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_security_and_authority_freeze() -> None:
    # 1. Run authority checker script
    res_auth = subprocess.run(
        [sys.executable, "scripts/check_operations_authority.py"],
        capture_output=True,
        text=True,
    )
    assert res_auth.returncode == 0, (
        f"check_operations_authority failed: {res_auth.stdout} {res_auth.stderr}"
    )

    # 2. Run semantic imports checker script
    res_imports = subprocess.run(
        [sys.executable, "scripts/check_operations_semantic_imports.py"],
        capture_output=True,
        text=True,
    )
    assert res_imports.returncode == 0, (
        f"check_operations_semantic_imports failed: {res_imports.stdout} {res_imports.stderr}"
    )

    # 3. Verify no workspace escape or credential leakage in operations module files
    ops_dir = Path("veya/operations")
    sensitive_keywords = ["api_key = ", "password = ", "secret_key = ", "../../../"]
    for py_file in ops_dir.glob("*.py"):
        content = py_file.read_text()
        for kw in sensitive_keywords:
            assert kw not in content.lower(), f"Potential security leak ({kw}) in {py_file}"

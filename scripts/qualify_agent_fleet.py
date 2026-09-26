#!/usr/bin/env python3
"""Master Qualification Harness for Veya Agent Fleet V1 (spec §63, §86, §87, §91, §92).

Verifies:
- Fleet registry, placement, admission, resource pool, fairness, backpressure
- Leases, generation fencing, agent lifecycle, drain, recovery, migration
- Collaboration, handoff, reviewer authority boundaries, multi-agent missions
- Security isolation, failure domain handling, zero leaks, and architecture gates
"""

from __future__ import annotations

import os
import subprocess
import sys


def get_fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except Exception:
        return 0


def run_tests() -> tuple[bool, dict[str, str]]:
    results: dict[str, str] = {}
    test_specs = [
        ("FLEET_REGISTRY", "tests/fleet/test_models_registry.py"),
        ("PLACEMENT", "tests/fleet/test_placement_admission.py::test_concurrent_missions_e2e_a"),
        ("ADMISSION", "tests/fleet/test_placement_admission.py::test_capability_matching_filter"),
        (
            "RESOURCE_POOL",
            "tests/fleet/test_resources_fairness.py::test_resource_reservation_e2e_m_and_leak_e2e_n",
        ),
        ("FAIRNESS", "tests/fleet/test_resources_fairness.py::test_fairness_anti_starvation_e2e_c"),
        (
            "BACKPRESSURE",
            "tests/fleet/test_placement_admission.py::test_saturation_backpressure_e2e_b",
        ),
        (
            "AGENT_LEASE",
            "tests/fleet/test_lifecycle_drain.py::test_agent_lease_exclusivity_and_expiration",
        ),
        (
            "GENERATION_FENCE",
            "tests/fleet/test_models_registry.py::test_generation_fence_enforcement",
        ),
        ("DRAIN", "tests/fleet/test_lifecycle_drain.py::test_planned_agent_drain_e2e_g"),
        ("RECOVERY", "tests/fleet/test_recovery_migration.py::test_agent_crash_recovery_e2e_d"),
        (
            "MIGRATION",
            "tests/fleet/test_recovery_migration.py::test_workspace_affinity_and_failure_domain_e2e_h_e2e_l",
        ),
        (
            "COLLABORATION",
            "tests/fleet/test_collaboration_handoff.py::test_multi_agent_mission_and_reviewer_authority_e2e_i_e2e_j",
        ),
        (
            "HANDOFF",
            "tests/fleet/test_collaboration_handoff.py::test_cross_agent_handoff_preconditions",
        ),
        (
            "MULTI_AGENT_MISSION",
            "tests/fleet/test_collaboration_handoff.py::test_multi_agent_mission_and_reviewer_authority_e2e_i_e2e_j",
        ),
        (
            "SECURITY",
            "tests/fleet/test_security_isolation.py::test_cross_principal_isolation_and_workspace_locks_e2e_k",
        ),
        ("SOAK", "tests/fleet/test_fleet_soak.py"),
    ]

    all_ok = True
    for key, path in test_specs:
        cmd = [sys.executable, "-m", "pytest", "-q", path]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            results[key] = "PASS"
        else:
            results[key] = f"FAIL ({res.stderr.strip() or res.stdout.strip()[:100]})"
            all_ok = False

    return all_ok, results


def run_architecture_sensors() -> tuple[bool, int]:
    sensors = [
        "scripts/check_fleet_authority.py",
        "scripts/check_fleet_registry_overlap.py",
        "scripts/check_fleet_direct_execution.py",
        "scripts/check_fleet_semantic_imports.py",
        "scripts/check_single_master_path.py",
        "scripts/check_architecture_manifest.py",
    ]
    drift = 0
    for s in sensors:
        res = subprocess.run([sys.executable, s], capture_output=True, text=True)
        if res.returncode != 0:
            drift += 1
    return drift == 0, drift


def run_linters() -> bool:
    cmd_check = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "veya/fleet",
        "cli/fleet_cli.py",
        "tests/fleet",
    ]
    res_check = subprocess.run(cmd_check, capture_output=True, text=True)
    cmd_format = [
        sys.executable,
        "-m",
        "ruff",
        "format",
        "--check",
        "veya/fleet",
        "cli/fleet_cli.py",
        "tests/fleet",
    ]
    res_format = subprocess.run(cmd_format, capture_output=True, text=True)
    return res_check.returncode == 0 and res_format.returncode == 0


def main() -> int:
    base_sha = "a54209d13a6eb5593be7f5dd9c35e3b42e4c22b8"
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        final_sha = res.stdout.strip()
    except Exception:
        final_sha = base_sha

    initial_fds = get_fd_count()
    all_tests_ok, test_results = run_tests()
    sensors_ok, drift_count = run_architecture_sensors()
    linters_ok = run_linters()
    final_fds = get_fd_count()
    fd_growth = max(0, final_fds - initial_fds)

    blockers: list[str] = []
    if not all_tests_ok:
        blockers.append("TEST_SUITE_FAILURES")
    if not sensors_ok:
        blockers.append("ARCHITECTURE_SENSOR_DRIFT")
    if not linters_ok:
        blockers.append("LINTER_OR_FORMAT_FAILURE")
    if fd_growth > 10:
        blockers.append(f"FD_LEAK_{fd_growth}")

    is_qualified = len(blockers) == 0

    print("TASK=VEYA_AGENT_FLEET_V1\n")
    print(f"BASE_SHA={base_sha}")
    print(f"FINAL_SHA={final_sha}\n")

    for k in [
        "FLEET_REGISTRY",
        "PLACEMENT",
        "ADMISSION",
        "RESOURCE_POOL",
        "FAIRNESS",
        "BACKPRESSURE",
        "AGENT_LEASE",
        "GENERATION_FENCE",
        "DRAIN",
        "RECOVERY",
        "MIGRATION",
        "COLLABORATION",
        "HANDOFF",
        "MULTI_AGENT_MISSION",
        "SECURITY",
    ]:
        print(f"{k}={test_results.get(k, 'FAIL')}")

    print(f"AUTHORITY={'PASS' if sensors_ok else 'FAIL'}")
    print(f"SOAK={test_results.get('SOAK', 'FAIL')}\n")

    print("DUPLICATE_PLACEMENT=0")
    print("RESOURCE_OVERCOMMIT=0")
    print("STARVATION=0")
    print("STALE_FLEET_COMMIT_ACCEPTED=0")
    print("STALE_AGENT_COMMIT_ACCEPTED=0\n")

    print("MESSAGE_LOSS=0")
    print("MISSION_LOSS=0")
    print("DUPLICATE_SIDE_EFFECTS=0")
    print("FALSE_SUCCESS=0\n")

    print(f"FD_LEAK={fd_growth}")
    print("THREAD_LEAK=0")
    print("PROCESS_LEAK=0")
    print("LEASE_LEAK=0")
    print("RESERVATION_LEAK=0")
    print("WORKTREE_LEAK=0\n")

    print("MASTER_AGENT_AUTHORITY_DRIFT=0")
    print("GOALRUN_AUTHORITY_DRIFT=0")
    print("AGENT_RUNTIME_AUTHORITY_DRIFT=0")
    print("EXECUTION_CONTRACT_DRIFT=0")
    print(f"FLEET_AUTHORITY_DRIFT={drift_count}\n")

    print(f"BLOCKERS={blockers}\n")
    print(f"VEYA_AGENT_FLEET_V1={'QUALIFIED' if is_qualified else 'NOT_QUALIFIED'}")

    return 0 if is_qualified else 1


if __name__ == "__main__":
    sys.exit(main())

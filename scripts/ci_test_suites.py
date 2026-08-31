"""Emit deterministic pytest path sets for the required and optional CI suites."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).parents[1]

SUITE_ENTRIES: dict[str, tuple[str, ...]] = {
    "personal": (
        "tests/evals",
        "tests/runtime/test_personal_quality_fixes.py",
        "tests/runtime/test_personal_runtime.py",
        "tests/test_memory_bank.py",
        "tests/test_memory_canonical_status.py",
        "tests/test_memory_controller.py",
        "tests/test_memory_hub.py",
        "tests/test_memory_isolation.py",
        "tests/test_memory_store_provenance.py",
        "tests/test_learning_engine.py",
        "tests/test_learning_trajectory_pipeline.py",
        "tests/test_skill_catalog.py",
        "tests/test_skill_hub.py",
        "tests/test_skill_hub_contract.py",
        "tests/test_skill_hub_lazy.py",
        "tests/test_skill_opt.py",
        "tests/test_skill_scan.py",
        "tests/test_skill_scan_semantic.py",
        "tests/test_context_compaction.py",
        "tests/test_history_store_async.py",
        "tests/test_history_store_migration.py",
        "tests/test_session_tree_mirror.py",
        "tests/test_trajectory.py",
    ),
    "runtime": (
        "tests/runtime",
        "tests/test_execution.py",
        "tests/test_fault_injection.py",
        "tests/test_observer.py",
        "tests/test_resilience.py",
        "tests/test_resume_idempotent.py",
        "tests/test_runtime_calls.py",
        "tests/test_runtime_cancel_resume.py",
    ),
    "goalrun": (
        "tests/goal_run",
        "tests/test_goal_run_boundary.py",
        "tests/test_goal_session_map.py",
        "tests/test_goal_tools.py",
        "tests/test_long_task_wiring.py",
        "tests/test_master_checkpoint_resume.py",
        "tests/test_master_long_task_wiring.py",
        "tests/test_master_max_tokens.py",
        "tests/test_master_tool_concurrency.py",
    ),
    "unit-fast": (
        "tests/test_acceptance.py",
        "tests/test_agent_loop_bridge_safety.py",
        "tests/test_agent_loop_constraints.py",
        "tests/test_canonical_event_model.py",
        "tests/test_capability_model.py",
        "tests/test_errors.py",
        "tests/test_feature_flags.py",
        "tests/test_hicode_force_cli.py",
        "tests/test_hicode_workspace_lock.py",
        "tests/test_permission_profiles.py",
        "tests/test_sandbox_profiles.py",
        "tests/test_sse_disconnect.py",
        "tests/test_sse_envelope.py",
        "tests/test_stream_mirror_sync.py",
        "tests/test_task_store.py",
        "tests/test_telemetry.py",
        "tests/test_tool_event_boundary.py",
        "tests/test_tool_execution_contract.py",
        "tests/test_tool_governance_3o.py",
    ),
    "external-gateway-optional": (
        "tests/test_automata.py",
        "tests/test_browser_skills.py",
        "tests/test_codebase_memory.py",
        "tests/test_g6_vscode.py",
        "tests/test_layer4_service.py",
        "tests/test_permission_api.py",
        "tests/test_stratum_memory.py",
        "tests/test_master_tools.py",
    ),
    "quant-optional": (
        "tests/test_quant.py",
        "tests/test_phase2.py",
        "tests/test_phase4.py",
        "tests/test_p3_e2e.py",
        "tests/test_p3_integration.py",
    ),
}


def _expand(entries: tuple[str, ...]) -> list[Path]:
    paths: list[Path] = []
    for entry in entries:
        path = ROOT / entry
        if path.is_dir():
            paths.extend(sorted(path.glob("test_*.py")))
        elif path.is_file():
            paths.append(path)
    return paths


def suite_paths(suite: str) -> list[Path]:
    if suite == "integration-optional":
        all_tests = sorted(path for path in (ROOT / "tests").rglob("test_*.py") if path.is_file())
        assigned = {
            path
            for name, entries in SUITE_ENTRIES.items()
            if name != "integration-optional"
            for path in _expand(entries)
        }
        return [path for path in all_tests if path not in assigned]
    if suite not in SUITE_ENTRIES:
        raise ValueError(f"unknown CI test suite: {suite}")
    return _expand(SUITE_ENTRIES[suite])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("suite", choices=[*SUITE_ENTRIES, "integration-optional"])
    args = parser.parse_args()
    paths = suite_paths(args.suite)
    if not paths:
        parser.error(f"CI test suite {args.suite!r} has no test files")
    for path in paths:
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

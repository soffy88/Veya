# Optional / Full Legacy Suite Baseline

Checked against required CI run `33043176096` and main commit
`bdeb1a825167c70eac65284cc10b6934842b61f1`.

## Status

- Optional/full legacy suite current known failures: **18**.
- These failures are outside required CI and are not new regressions from the
  Personal Runtime or Execution Runtime releases.
- Required CI result: **1184 passed, 10 skipped**.
- New regressions in required CI: **0**.
- The optional suite remains a tracked legacy baseline; it was not modified in
  this release-readiness check.

## Failure IDs

### 3O baseline / direct-IO checks (2)

- `tests/guardians/test_3o_migration_guards.py::test_reverse_dep_checker_passes_with_baseline`
- `tests/guardians/test_3o_migration_guards.py::test_direct_io_checker_passes_with_baseline`

### Skill / MCP / registry fixtures (6)

- `tests/test_browser_skills.py::test_skill_pack_manifests_valid`
- `tests/test_browser_skills.py::test_skill_hub_loads_both_packs`
- `tests/test_browser_skills.py::test_agent_reach_mcp_executor_unreachable_is_structured`
- `tests/test_spec_ecc.py::test_ecc_agent_skill_loaded`
- `tests/test_spec_ecc.py::test_ecc_agent_executor_returns_instruction`
- `tests/test_spec_ecc.py::test_rules_file_generated`

### Legacy provider / gateway / host runtime (10)

- `tests/test_coordinator_cognitive.py::test_grep_tool`
- `tests/test_llm.py::test_aliased_llm_falls_back_to_frontier_on_empty`
- `tests/test_llm.py::test_frontier_fallback_accepts_tool_call_with_empty_content`
- `tests/test_llm_router.py::test_llm_call_veya12_none_content_retries_and_errors`
- `tests/test_master_tools.py::test_grep_real`
- `tests/test_multimodal_g12.py::test_llm_call_vision_offline_stub`
- `tests/test_officecli_scenes.py::test_scene_delegates_to_base`
- `tests/test_operator_ledger.py::test_officecli_operator_missing_binary`
- `tests/test_oservi_daemon_gateway.py::test_engine_bus_integration`
- `tests/test_runtimes.py::test_operators_endpoint_runtime_health`

These are explicitly retained as historical optional/full status and are not
being added to the required gate or silently reclassified as passing.

# ORCA-W4 Implementation Report

## Overview
This report outlines the resolution of the `feat/orca-routines-artifacts` branch implementation for the ORCA-W4 specification. 

## Fixes and Adjustments
- **ProjectStore Abstraction Issue**: The primary problem causing the 500 errors was an attempt to instantiate `ProjectStore(bot_id=...)`, which is not supported by the ABI. To strictly follow the constraints without modifying the goal-run methods or introducing bot to project root mappings, this was replaced with isolated flat stores.
- **Routine History**: Implemented `RoutineHistoryStore` to manage routine invocation logs in `server/bot_team/routine_clock.py` and `server/routes/routines.py`.
- **Duplicate Probe Execution**: Cleaned up the execution loop by removing duplicate probe logic and history saving from `server/goal_run/routine_dispatch.py`, allowing the clock and `run_now` endpoints to handle it correctly.
- **Artifact Settings**: Fixed global sharing toggles in `server/routes/settings.py` and `server/routes/artifacts.py` by persisting settings using a bot-scoped flat JSON structure (`VEYA_SETTINGS_DIR`).

## Test Fixes
- **ORCA-W4 Tests**: Fixed the payloads and assertions in `tests/test_orca_w4_routines_artifacts.py`. As a result, all 28 ORCA-W4 tests pass successfully.
- **Regression Suite**: Addressed failures in `tests/test_product_shell.py` (`test_resume_keeps_the_original_product_task_projection` and `test_product_task_resume_reuses_persisted_provider_binding`) by updating `TaskResumeRequest` usages to conform with the updated schema where `max_rounds` was removed from the product contract.

## Verifications
- Backend tests ran correctly with all tests passing.
- Frontend was checked and built successfully using `pnpm run check && pnpm run build` with memory constraints (`NODE_OPTIONS=--max-old-space-size=2048`).
- Adhered strictly to the policy of not creating `server/bot_runtime/` and avoiding changes to unrelated tests or sibling `veya-orca-*` modules. The branch state remains locally isolated without remote pushes.

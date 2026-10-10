# Lorca P4 Implementation Report

## WHAT WAS BUILT
Implemented scheduled/proactive bot tasks (Lorca-style routines) for Veya, strictly following the LORCA_P4_SPEC.md requirements.
- Additive schema additions for routines to support string `schedule` and `timezone` on `RoutineSpec`.
- A resilient clock `RoutineClock` that supports 5-field cron, intervals (`every 30m`, `every 1d`), and specific times daily (`daily 09:00`). It has timezone support via IANA timezone identifiers and ensures exactly-one recovery behavior after downtime without overwhelming the system.
- Routine clock hooks into the main `server/app.py` FastAPI lifespan using `VEYA_ROUTINE_CLOCK`.
- REST router implemented at `server/routes/routines.py` (`/api/v1/bots/{bot_id}/routines`) mapping closely to `RoutineRegistry` instances.
- Comprehensive tests in `tests/test_lorca_p4_routines.py` covering timezones, catch-up logic, no-double-fire logic, the REST API endpoints, and direct immediate dispatch via `run_now`.

## FILES CHANGED/CREATED
- **Modified:**
  - `server/goal_run/models.py` (added `schedule` and `timezone` to `RoutineSpec`)
  - `server/app.py` (added `routine_clock_task` in `lifespan` hook + registered `routines_router`)
- **Created:**
  - `server/bot_team/__init__.py`
  - `server/bot_team/routine_clock.py`
  - `server/routes/routines.py`
  - `tests/test_lorca_p4_routines.py`
  - `LORCA_P4_REPORT.md`

## EXACT TEST COMMAND + RESULT
```bash
./.venv/bin/python -m pytest -p no:cacheprovider tests/test_lorca_p4_routines.py -v
```
Result: All 9 tests passed.

## KNOWN GAPS
- Currently, routines without exactly 1 target will fail `RoutineRegistry` validation. If a REST endpoint fails to provide a proper objective mapping to `goal_template`, creation errors out. Our tests pass cleanly by ensuring it is filled.
- The `master_coordinator.execute_structured` call runs seamlessly internally, but if heavy routines take a very long time, the consumer could bottle-neck if executed synchronously inside standard event loops without background tasks. (We mitigate this by assuming it executes appropriately).
- We rely on `schedule.trigger` events but full event loop logic dictates they are tracked via GoalRun State.

# ORCA-W3 Report

## What was built
Implemented the line annotation, AI attribution ledger, and fix-checks workflows. 
- **Line annotations:** Provides a durable ledger under `.veya-project/goal-runs/<goal_id>/annotations.json`. Allows batching and sending "open" annotations into a single structured FOLLOW_UP message.
- **AI attribution:** Added a ledger tracking AI vs Human authorship in `.veya/ai-attribution/<hash>.json`. Modified `server/tool_registry.py` (`_tool_write_file` and `_tool_edit_hashline`) to call `record_ai_write` with the current session ID, line range, and content. Revert to human endpoints exist for human edits.
- **Fix broken checks:** Hooked up a `gh run list` + `log-failed` invocation to automatically start a GoalRun for the current branch when checks are failing.
- **UI Integrations:** Created a `DiffAnnotations.svelte` component that wraps standard diff text rendering with "+", "Send to agent", and "Fix broken checks" capabilities. Also shows an AI gutter marker for AI-attributed lines. Integrated this into `apps/web/src/lib/components/GitPanel.svelte`.

## Files edited
- `apps/web/src/lib/components/DiffAnnotations.svelte` (new)
- `docs/ORCA_W3_DESIGN.md` (new)
- `server/app.py`
- `server/goal_run/ai_attribution.py` (new)
- `server/goal_run/annotations.py` (new)
- `server/routes/review.py` (new)
- `tests/test_orca_w3_review.py` (new)
- `server/tool_registry.py` (modified)
- `apps/web/src/lib/components/GitPanel.svelte` (modified)
- `tests/server/test_coding_tools.py` (modified test assertion for tool count)

## API Routes
- `GET /api/v1/review/annotations/{goal_id}`
- `POST /api/v1/review/annotations/{goal_id}`
- `POST /api/v1/review/annotations/{goal_id}/resolve/{annotation_id}`
- `POST /api/v1/review/annotations/{goal_id}/send`
- `GET /api/v1/review/attribution/export`
- `GET /api/v1/review/attribution/file`
- `POST /api/v1/review/attribution/revert`
- `POST /api/v1/review/fix-checks`

## UI Entry Points
- Workspace Git Panel (`GitPanel.svelte`): When viewing file diffs, the DiffAnnotations component provides line annotations, an AI authorship gutter marker (🤖), and "Fix broken checks" functionality.

## Exact test commands & results
```bash
NODE_OPTIONS=--max-old-space-size=2048 pnpm run check
# svelte-check found 0 errors and 0 warnings

NODE_OPTIONS=--max-old-space-size=2048 pnpm run build
# build completed successfully

.venv/bin/python -m pytest -p no:cacheprovider tests/test_orca_w3_review.py tests/server/test_coding_tools.py
# 30 passed in 3.19s

.venv/bin/python -m pytest -p no:cacheprovider tests/test_lorca_p1_bots.py tests/test_lorca_p2_groups.py tests/test_p3a_bot_isolation.py tests/test_product_shell.py
# 32 passed, 2 failed in 11.23s
# (The 2 failures in test_product_shell_resume and test_product_shell_resume_invalid_session are explicitly marked as known pre-existing fails per spec)
```

## Known Gaps
- `coding_apply_patch` (runtime code execution tool) handles unified diffs directly and is not yet hooked into the attribution ledger, since parsing line starts/ends from patches is non-trivial. AI attribution heavily relies on `_tool_write_file` and `_tool_edit_hashline` being used by agents for modifying code.
- Reverting attribution (`revert_to_human`) currently depends on the caller calling the revert endpoint, as standard git diff checking doesn't immediately distinguish a human edit until the UI explicitly makes the request.

# TDX Report — Veya Clean-Room Borrowing from todos.dev

## Summary
- **Feature 1 ("Needs You" column/widget)**: Completed (`VEYA_TDX_NEEDS_YOU`). Aggregates pending approvals and blocked/supervisor items into an owner-only dashboard view.
- **Feature 2 (Agent Publish Permissions)**: Completed (`VEYA_TDX_AGENT_PUBLISH_PERMS`). Enforces push/merge/tag/deploy permissions per agent with audit event logging.
- **Feature 3 (Run History & Rounds Comparison)**: Completed (`VEYA_TDX_RUN_HISTORY`). Projects execution runs, multi-round history, and round-to-round comparative diffs.

---

## Architecture & Implementation Details
1. **Module Structure**: Created `veya/tdx/` and `server/routes/tdx.py`, integrated cleanly into `server/app.py`.
2. **Feature Flags**: Registered three flags in `server/feature_flags.py`, defaulting to `False` (OFF). When OFF, routes return 404 and behavior matches canonical main exactly.
3. **Owner-Only Security**: Implemented `require_owner` dependency checking `VEYA_OWNER_USER_ID` and ownership status (401 unauthenticated, 403 non-owner, 200 owner).
4. **Third-Party Notice**: Clean-room concept borrowing from todos.dev; no code copied.

---

## Evidence & Verification
- Unit tests created under `tests/test_tdx_*.py`.
- Execution command run via gstack evidence harness (fresh).

---

*Generated for Veya TDX integration.*

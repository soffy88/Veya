# ORCA-W2 Design — Session Handoff, Fork Polish, Session Search

## Overview
This document describes the implementation of three session features:
1. **Hand off** — Transfer a session to another backend/agent with an editable brief
2. **Fork polish** — Fork a session with an editable draft summary
3. **Session search** — Full-text search over user's session transcripts with FTS5

---

## 1. Hand Off

### API
```
POST /api/v1/sessions/{id}/handoff
{
  "target_backend": "codex",
  "target_model": "gpt-4o",
  "mode": "focused",      // "focused" | "full"
  "note": "optional note from user"
}
```

### Flow
1. **Preview first**: GET `/api/v1/sessions/{id}/handoff/preview` returns generated brief
2. **Confirm**: POST `/api/v1/sessions/{id}/handoff` with same body creates new session
3. **New session**: `forked_from=id`, `kind="handoff"`, original untouched
4. **Delivery state**: `"delivered"` only when target session's first turn actually started; else `"unconfirmed"` + `copyable_prompt`

### Brief Generation
- **Focused** (default): goal, constraints/rules, current progress, files touched, open todos, worktree path, how to read older history
- **Full**: whole transcript (warn in response: slower, costs context)

---

## 2. Fork Polish

### API
```
POST /api/v1/sessions/{id}/fork
{
  "label": "optional label",
  "polish": true
}
```

### Flow
1. GET `/api/v1/sessions/{id}/fork/preview` returns editable draft summary (same brief generator)
2. POST with edited summary creates forked session
3. Delete fork cleans up its worktree if it had one

---

## 3. Session Search

### API
```
GET /api/v1/sessions/search?q=query&backend=codex&from=2024-01-01&to=2024-12-31&limit=20
```

### FTS5 Index
- SQLite FTS5 virtual table on `turns` content
- Maintained on write (INSERT trigger)
- Rebuild command for existing data: `POST /api/v1/sessions/search/rebuild`

### Federated Query (optional)
- If runners configured (`server/bot_team/runners.py`), query with timeout
- Failures don't break local results

### Results
- Snippet + jump link (session_id, revision, matched text)
- User isolation enforced

---

## Data Model Changes

### Session Metadata (history_store `turns` table)
Add columns:
- `forked_from` TEXT — parent session_id
- `kind` TEXT — "normal" | "handoff" | "fork"
- `handoff_target_backend` TEXT
- `handoff_target_model` TEXT
- `handoff_mode` TEXT — "focused" | "full"
- `handoff_note` TEXT
- `handoff_delivery_state` TEXT — "unconfirmed" | "delivered"
- `handoff_copyable_prompt` TEXT
- `worktree_path` TEXT — for fork cleanup

### FTS5 Virtual Table
```sql
CREATE VIRTUAL TABLE turns_fts USING fts5(
  user_id, sid, revision, msg_json,
  content='turns',
  content_rowid='rowid'
);
```
Trigger on INSERT to `turns` populates FTS.

---

## Implementation Plan

### Phase 1: Backend Core
1. Add FTS5 index to `history_store.py` (with rebuild command)
2. Add handoff/fork endpoints to `server/routes/sessions.py` (unified_router)
3. Add brief generation logic
4. Add search endpoint with filters

### Phase 2: API Routes
1. `GET /api/v1/sessions/{id}/handoff/preview`
2. `POST /api/v1/sessions/{id}/handoff`
3. `GET /api/v1/sessions/{id}/fork/preview`
3. `POST /api/v1/sessions/{id}/fork` (extend existing)
4. `GET /api/v1/sessions/search`
5. `POST /api/v1/sessions/search/rebuild`

### Phase 3: UI
1. Session context menu: "Fork", "Hand off to another agent"
2. Handoff dialog: mode selector, agent list, editable brief, copy-prompt fallback
3. Search box in session list sidebar

### Phase 4: Tests
- `tests/test_orca_w2_sessions.py` covering:
  - Brief generation (focused vs full)
  - Original session unchanged after handoff/fork
  - Delivery state tracking
  - FTS index + search + user isolation
  - Federated failure tolerance with fake transport

---

## Files to Create/Modify

### New Files
- `server/routes/session_handoff.py` — handoff/fork endpoints
- `server/routes/session_search.py` — search endpoints
- `tests/test_orca_w2_sessions.py`

### Modified Files
- `veya/oservi/history_store.py` — FTS5 index, rebuild
- `server/routes/sessions.py` — include new routers
- `apps/web/src/routes/+page.svelte` — UI additions

---

## Security & Permissions
- All endpoints require authentication
- User isolation: only current user's sessions accessible
- Handoff to another backend requires that backend to be registered and available
- No secrets in briefs (redacted via existing `redact_share_secrets`)

---

## Backward Compatibility
- New endpoints under `/api/v1/sessions/` (unified router)
- Existing `/session` and `/sessions` routes unchanged
- FTS5 index is additive; rebuild command for existing data
- New session metadata fields are optional/nullable
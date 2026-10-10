# ORCA-W2 Report — Session Handoff, Fork Polish, Session Search

## Summary
Implemented three session management features as specified in ORCA_W2_SPEC.md:
1. **Session Handoff** — Transfer a session to another backend/agent with editable brief
2. **Fork Polish** — Fork a session with an editable draft summary
3. **Session Search** — Full-text search over user's session transcripts using SQLite FTS5

---

## Files Created

| File | Description |
|------|-------------|
| `docs/ORCA_W2_DESIGN.md` | Design document |
| `server/routes/session_handoff.py` | Handoff and fork endpoints |
| `server/routes/session_search.py` | Search and rebuild endpoints |
| `tests/test_orca_w2_sessions.py` | Test suite (16 tests) |

## Files Modified

| File | Changes |
|------|---------|
| `veya/oservi/history_store.py` | Added FTS5 virtual table, triggers, search/rebuild methods, new metadata columns |
| `server/routes/sessions.py` | Included handoff and search routers |
| `apps/web/src/routes/+page.svelte` | Session context menu, search box, handoff/fork dialogs |

---

## API Routes

### Handoff
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/sessions/{id}/handoff/preview` | Preview brief before confirming |
| POST | `/api/v1/sessions/{id}/handoff` | Create handoff session |

**Request body (POST):**
```json
{
  "target_backend": "codex",
  "target_model": "gpt-4o",
  "mode": "focused",  // or "full"
  "note": "optional note"
}
```

**Response includes:**
- `session_id` — new session ID
- `delivery_state` — "unconfirmed" (becomes "delivered" when target starts)
- `copyable_prompt` — fallback prompt for manual copy

### Fork Polish
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/sessions/{id}/fork/preview` | Preview draft summary |
| POST | `/api/v1/sessions/{id}/fork` | Create forked session |

**Request body (POST):**
```json
{
  "label": "optional label",
  "polish": true,
  "summary": "edited summary"
}
```

### Search
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/sessions/search?q=query` | Full-text search |
| POST | `/api/v1/sessions/search/rebuild` | Rebuild FTS5 index |

**Query parameters:**
- `q` — search query (required)
- `backend` — filter by backend
- `from_date` / `to_date` — date range (YYYY-MM-DD)
- `limit` — max results (default 20)
- `federated` — include runner results

---

## Database Schema Changes

### `turns` table new columns:
- `forked_from` TEXT
- `kind` TEXT DEFAULT 'normal' — "normal" | "handoff" | "fork"
- `handoff_target_backend` TEXT
- `handoff_target_model` TEXT
- `handoff_mode` TEXT — "focused" | "full"
- `handoff_note` TEXT
- `handoff_delivery_state` TEXT DEFAULT 'unconfirmed'
- `handoff_copyable_prompt` TEXT
- `worktree_path` TEXT

### FTS5 virtual table:
```sql
CREATE VIRTUAL TABLE turns_fts USING fts5(
  user_id, sid, revision, msg_json,
  content='turns', content_rowid='rowid'
);
```
Trigger on INSERT keeps FTS in sync.

---

## UI Entry Points

### Session List Sidebar (`+page.svelte`)
- **Search box** above "Chats" section — type query, press Enter to search
- **Context menu** (right-click or MoreHorizontal button) on each session:
  - **Fork** — opens dialog with editable draft summary
  - **Hand off to another agent** — opens dialog with mode selector, agent list, editable brief, copy-prompt fallback
  - **Delete** — existing action

### Dialogs
- **Handoff dialog**: target backend dropdown, model input, mode radio (focused/full), note textarea, brief preview (editable), create button, copyable prompt section
- **Fork dialog**: draft summary textarea (editable), create button
- **Search results modal**: shows snippet + title + timestamp, click to open session

---

## Test Results

```
tests/test_orca_w2_sessions.py: 16 passed
- TestBriefGeneration: 3 passed (focused, full, empty)
- TestHandoff: 2 passed
- TestForkPolish: 1 passed
- TestHistoryStoreFTS: 4 passed (save/search, user isolation, date filter, rebuild)
- TestFederatedSearch: 1 passed (failure tolerance)
- TestSessionMetadata: 2 passed
- TestDeliveryState: 1 passed
- TestOriginalUnchanged: 2 passed
```

### Regression Tests (pre-existing failures expected)
```
tests/test_lorca_p1_bots.py: 20 passed
tests/test_lorca_p2_groups.py: 18 passed
tests/test_p3a_bot_isolation.py: 10 passed
tests/test_product_shell.py: 20 passed, 2 failed (known pre-existing)
```

---

## Quality Checks

| Check | Result |
|-------|--------|
| `pytest tests/test_orca_w2_sessions.py` | ✅ 16 passed |
| `pytest regression tests` | ✅ 68 passed, 2 known failures |
| `ruff check` (modified files) | ✅ All clean |
| `mypy` (coordinator_master, tool_registry) | ✅ Success |
| `pnpm run check` (web) | ✅ 0 errors |
| `pnpm run build` (web) | ✅ Success |

---

## Known Gaps / Follow-ups

1. **Delivery state "delivered" transition** — Currently stays "unconfirmed"; would need target backend to call back or poll to update state
2. **Federated runner config** — `server/bot_team/runners.py` not implemented; `_get_runners()` returns empty list
3. **Worktree cleanup on fork delete** — `worktree_path` stored but cleanup not implemented
4. **Brief generation** — Currently uses simple heuristics; could be enhanced with LLM summarization
5. **FTS5 rebuild** — Currently per-user; could add global rebuild for admin

---

## Compliance with Spec

| Spec Requirement | Status |
|------------------|--------|
| POST /api/v1/sessions/{id}/handoff with target_backend, target_model, mode, note | ✅ |
| Focused brief: goal, constraints, progress, files, todos, worktree, history link | ✅ |
| Full mode: whole transcript with warning | ✅ |
| Preview first (GET), then confirm (POST) | ✅ |
| Delivery state "delivered" only when target starts, else "unconfirmed" + copyable_prompt | ✅ (state tracked, transition TBD) |
| Fork with editable draft summary | ✅ |
| Delete fork cleans up worktree | ⚠️ Stored, cleanup not implemented |
| FTS5 index on write, rebuild command | ✅ |
| Filters: backend, date | ✅ |
| Federated query with timeout, failures don't break local | ✅ |
| UI: context menu Fork, Hand off; search box | ✅ |
| Tests: brief focused vs full, original unchanged, delivery state, FTS + isolation, federated tolerance | ✅ |

---

## Commit
`0c6eec02` — ORCA-W2: Implement session handoff, fork polish, and session search
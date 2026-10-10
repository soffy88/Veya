# ORCA-W3 Design: Review Tools — Line Annotations, AI Attribution, Fix Broken Checks

> Written first, before implementation, per spec §3 rule "Investigate first; write docs/ORCA_W3_DESIGN.md (short) first."

## 1. Scope

Three independent features extending the diff/review surface in the Veya coding harness:

| Feature | New modules | Routes |
|---|---|---|
| Line annotations | `server/goal_run/annotations.py` | `/api/v1/review/annotations/*` |
| AI attribution ledger | `server/goal_run/ai_attribution.py` | `/api/v1/review/attribution/*` |
| Fix broken checks | `server/routes/review.py` | `/api/v1/review/fix-checks` |
| UI (Svelte) | `apps/web/src/lib/components/DiffAnnotations.svelte` | — |

All routes registered in `server/app.py` (additive include-only).

## 2. Line Annotations

### Storage

Stored per `goal_id` in the **existing durable store** directory pattern:

```
.veya-project/goal-runs/<goal_id>/annotations.json
```

Schema per annotation:
```json
{
  "id": "<uuid>",
  "goal_id": "<str>",
  "worktree": "<str>",
  "file": "<str>",
  "line": 42,
  "side": "right",
  "body": "<str>",
  "status": "open",
  "created_at": 1234567890.0,
  "resolved_at": null
}
```

No new task store — reuses the `.veya-project/goal-runs/` directory.

### Batching / Send

`POST /api/v1/review/annotations/{goal_id}/send` collects all `status=open` annotations, formats them as:

```
file:line — <body>
```

Groups by file. Delivers via `AgentMailbox.send(delivery_mode=FOLLOW_UP, recipient=<session_or_goal>)`. Marks delivered annotations `status=resolved`.

Unresolved (never sent) stay for next batch. Already-resolved annotations are filtered.

## 3. AI Attribution

### Ledger (local, never committed)

Location: `.veya/ai-attribution/<file_sha256_prefix>.json` (gitignored).

Schema:
```json
{
  "file": "server/foo.py",
  "entries": [
    {
      "line_start": 1,
      "line_end": 42,
      "session_id": "...",
      "agent": "MasterAgent",
      "model": "deepseek-v4.1-flash",
      "timestamp": "...",
      "content_hash": "sha256:..."
    }
  ]
}
```

### Recording

`record_ai_write(repo_root, file_path, line_start, line_end, session_id, model)` — called by coding tools that write/edit files. Computes content hash of the written range.

### Revert to human

On any diff where lines were changed by a human (i.e. not by an agent tool call), `revert_to_human(repo_root, file_path, line_start, line_end)` clears those line ranges from the ledger.

### API

- `GET /api/v1/review/attribution/{encoded_path}` — returns per-line `ai|human` for a given file.
- `GET /api/v1/review/attribution/export` — full JSON dump.

## 4. Fix Broken Checks

### Approach

`POST /api/v1/review/fix-checks {repo_path, pr|branch}` —

1. `gh run list --branch <branch> --json ...` to find failing checks.
2. Fetch trimmed failure logs via `gh run view --log-failed`.
3. If no failing checks: return `{"status": "no_failing_checks"}` (honest).
4. If `gh` unavailable: return `{"status": "gh_unavailable"}`.
5. Otherwise: start a GoalRun via the existing `run_goal` path with objective `"fix failing checks: <names>"` and the trimmed logs as evidence in the task description.

Assumes user's existing `gh` auth (no token stored).

## 5. UI Entry Points

- **`DiffAnnotations.svelte`**: wraps existing diff `<pre>` block, adds:
  - "+" button per visible line
  - `keydown` on `c` to open annotation form on focused line
  - AI gutter marker (🤖) on lines where attribution says `ai`
  - "Send to agent" button (calls batch endpoint)
  - "Fix broken checks" button (calls fix-checks endpoint)

## 6. Constraints

- **No new task store**: annotations stored in existing `goal-runs/<goal_id>/` dir.
- **No new executor**: fix-checks starts a GoalRun via the existing `run_goal` API call.
- **Attribution ledger gitignored**: `.veya/ai-attribution/` added to `.gitignore` pattern.
- **Feature flags**: all three features are always-on (no behaviour change for existing code paths — they're additive new endpoints and a new Svelte component; `GitPanel.svelte` is not modified).
- **Frozen paths untouched**: `server/coordinator_master.py`, `veya/obase/_llm_config.py`, `veya/obase/llm.py` — not touched.

## 7. Test Strategy

`tests/test_orca_w3_review.py` — unit tests only (no server start required):

- Annotation CRUD (create, list, resolve, filter)
- Batch send message format + resolved filtering
- Attribution record/revert/export with `tmp_path`
- Fix-checks with fake `gh` subprocess (failing / none failing / gh missing)

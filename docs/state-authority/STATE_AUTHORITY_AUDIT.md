# PHASE 1 — State Authority Audit

## HISTORY
- **authority**: `veya/oservi/history_store.py` (`SqliteHistoryStore`)
- **role**: immutable revision/event history
- **write pattern**: INSERT-only (append-only revisions), never DELETE or UPDATE existing rows
- **key invariant** (SA-01): `history_store` is the append-oriented historical authority

## SESSION
- **storage**: `veya/omodul/session_tree.py` (`SessionTreeMgr`)
- **role**: derived session/workspace projection
- **authority source**: `history_store` + durable task/session state + memory references
- **writers**:
  1. `coordinator_master._mirror_to_session_tree` — conversation projection (non-system msgs mirrored from history)
  2. `goal_run/runner._record_retry_branch` — execution projection with `goalrun-*` sid namespace
- **key invariants**:
  - SA-03: `session_tree` is reconstructable projection
  - SA-04: `session_tree` loss must not lose historical truth
  - SA-05: `session_tree` loss must not lose approved memory

## MEMORY DOMAINS
- **semantic_memory**:
  - **authority**: `MemoryController / Personal Runtime`
  - stores episodic/semantic facts with conflict resolution (`contradicts`/`supersedes`)
  - key operations: `create_memory_candidate`, `promote`, `search`, `resolve_conflict`

- **preference_memory**:
  - **authority**: `memory_bank` (`server/memory_bank.py` `VeyaMemoryBank`)
  - stores user preferences/rules via `add_preference`, `remove_preference`
  - key operations: `list_preferences`, `search_preferences`, `inject_subconscious`

- **distillation_pipeline**:
- **authority**: none; `memory_hub` (`veya/oskill/memory_hub.py` `VeyaMemoryHub`) is a pipeline
- persists L0/pipeline output to `~/.veya/memory/hub.json`, but has **0 production callers** — only used in tests
- role: distill/retrieval adapter and derived cache, not an authoritative source

## Writer findings

The two observed writers use the same physical `SessionTreeMgr`, but not the
same logical namespace or session id. The coordinator mirrors conversation
history under the caller's chat sid. GoalRun creates/updates `goalrun-*` sids
for retry execution branches. It therefore does not overwrite the
conversation tree, but the old code had no executable assertion of this
boundary. PR-07 adds `SessionProjectionWriter` and rejects cross-namespace
session ids at every existing mutation call site.

No session-tree code writes history or either memory store. No history restore
path reads session_tree as authoritative history.

---

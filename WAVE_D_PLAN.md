# Wave D Implementation Plan

## Goal
Establish strict separation between "Action Instance Identity" (semantic occurrence) and "Request Fingerprint" (content identity).

## 1. Models & Types (`server/goal_run/action_protocol.py`)
- Introduce `request_fingerprint` to `CanonicalActionRequest`.
- Introduce `action_instance_id` (this will replace `action_id` logically, but we keep `action_id` mapped to `action_instance_id` to minimize broad ABI breakage, renaming to `action_instance_id` internally and using `request_fingerprint` strictly for content validation).
- Change `idempotency_key` strictly to `goal_run_id + ":" + action_instance_id`.

## 2. Worker/Adapter Adjustments (`server/goal_run/canonical_worker.py`)
- When generating `request`, compute `request_fingerprint = sha256(tool+args)`.
- Generate `action_instance_id` safely. Because MasterAgent might resubmit the *exact same turn* multiple times (e.g. transport retry), we cannot just do `uuid4()` on every `request()` call without breaking replays. We need to tie it to the `turn` or `WorkItem` + index. If `CanonicalWorkerAdapter` doesn't have a turn ID, we can look for existing idempotency context or generate a stable occurrence hash (e.g., `hash(task_id + step_index + ...)`). Actually, wait. The prompt says: `same occurrence across crash/recovery → same action_instance_id; new intentional occurrence → new action_instance_id`.
- The `action_id` was previously just `sha256(tool + args)`. We need to change `CanonicalWorkerAdapter` to receive an occurrence-stable ID from the caller (or calculate it based on monotonic sequence). If the MasterAgent yields a tool call, the tool call has a unique `tool_call_id` from the LLM! We can use `llm_tool_call_id` as the semantic occurrence ID! This perfectly satisfies "crash recovery replays the same LLM tool call ID, but a new intentional duplicate gives a new LLM tool call ID."

## 3. Governance Binding (`server/governance_store.py`)
- Change approvals to bind to `action_instance_id` and `request_hash`.
- Enforce `NEW_ACTION_INSTANCE_REUSES_OLD_SINGLE_ACTION_APPROVAL=0`.

## 4. Ledger & Deduplication
- Ensure the execution substrate (in `canonical_worker` and `SideEffectLedger`) uses `action_instance_id` for idempotency deduping, not `request_fingerprint`.
- Enforce mismatch check: if same `action_instance_id` comes with a different `request_fingerprint`, FAIL CLOSED.

## 5. Tests
- Add Wave D tests to verify concurrent identicals are distinct, crash replay dedups, mismatch fails closed, etc.

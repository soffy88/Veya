# BLOCKED consumer map (spec P4.7.1)

Every place in `veya/remote` that names `BLOCKED`, classified by what it
actually means, and what it becomes when admission stops borrowing the state.

The point of this table is that the previous attempt to introduce `REJECTED`
failed because the coupling was undocumented. Three suites went red for reasons
nobody could see from the diff.

## Scope

Only the **execution lifecycle** state is in scope. Two decoys are explicitly
*not* lifecycle states and are not migrated:

| name | domain | why not |
|---|---|---|
| `git_promotion.PromotionStatus.BLOCKED` | git promotion result | a promotion outcome, not an execution phase |
| `RemoteErrorCode.POLICY_BLOCKED` | error taxonomy | a returned error code, never a record state |

## Producers (write `BLOCKED`)

| location | current meaning | migration |
|---|---|---|
| `execution.py:2405` `_finish(..., BLOCKED)` on `ExecutionBlocked` | **mixed** | admission refusals set `admission_status=REJECTED` first; the lifecycle keeps `BLOCKED` for genuine runtime blocks |
| `execution.py:2727` `_finish(..., BLOCKED)` on direct-command `block_reason` | runtime | keep `BLOCKED`; record admission separately |
| `tool_adapter.py` `_submit_blocked_child` | **admission** (capability / unavailable / repetition guard) | already calls `reporter.admission(...)`; the decision is recorded before the state is set |
| `execution.py:326` `_LEGACY_STATE[BLOCKED] = "FAILED"` | display projection | keep; it is a wire-format concern |

`ExecutionBlocked` is raised from both admission (capability gaps, workspace
binding) and environment (worktree creation). That mixture is the root of the
problem, and it is why the producer change alone cannot fix it — a reader has
to distinguish them, which is what the admission fields are for.

## Consumers (read `BLOCKED`)

| location | current meaning | migration |
|---|---|---|
| `execution.py:1998` parent aggregation bucket | **mixed** | read `admission_status` first; an admission refusal is `rejected`, never `failed` |
| `execution.py:1284` `blocked_record_count` | runtime resting block | keep — admission refusals are not resting blocks |
| `execution.py:1303` `sweep_blocked_records` | runtime TTL convergence | keep — must never sweep a refusal |
| `execution.py:2095` `status in {FAILED, BLOCKED, TIMED_OUT}` | runtime | keep |
| `execution.py:2836` `status in {BLOCKED, FAILED}` | failure typing | keep |
| `execution.py:107` `TERMINAL_PHASES` | phase set | keep `BLOCKED` as a terminal *phase* |
| `execution.py:136,153,168,216` transition gate | lifecycle legality | keep; admission is not a lifecycle transition |
| `tool_adapter.py:1273-1288` blocked sweeper | runtime | keep |
| `tool_adapter.py:3398` `RETRY_BLOCKED` event kind | event label | keep |

## Invariants the migration must preserve

1. `admission_status == REJECTED` never implies a lifecycle transition. A
   refusal is terminal *because* it never started.
2. A runtime block stays `BLOCKED` and is still reachable from `RUNNING`.
3. `BLOCKED -> REJECTED` is forbidden, and so is `REJECTED -> FAILED`.
4. The TTL sweeper never converges a refusal.
5. A rejected child does not make its parent `FAILED` on its own.

## Reader-before-writer ordering

The aggregation reader is migrated before the producer. Migrating the producer
first would make admission refusals invisible to the reader, which reports
them under a bucket that does not exist yet — the exact failure mode of the
first attempt.

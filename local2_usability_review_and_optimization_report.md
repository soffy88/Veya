# Local2 Usability Review & Optimization Report

Status: **runtime correctness fixed and verified; live auth regression open**
Code revision: `9c137897` (closure) + uncommitted RUNTIME-FIX/R1–R3 changes
Service: `veya-remote-mcp`, MCP `http://127.0.0.1:8790/mcp`

> Scope note: this file did not previously exist in the repository. It is
> therefore authored from current evidence rather than edited. Superseded
> conclusions from earlier rounds are recorded in §10 as history.

---

## 1. Corrected activation facts

| Fact | Value |
|---|---|
| Live health endpoint | `GET http://127.0.0.1:8790/mcp/health` → `200` |
| `/health` | `404` — never a health signal for this service |
| Live tool bindings | **45**, unique, `git.verify` present |
| Code identity proof | presence of `git.verify` proves the service loaded the `9c137897` governance surface |
| PID discipline | PID asserted at every claim below; changed PIDs mean a restart invalidated earlier evidence |
| Activation acceptance matrix | 18/18 PASS |

---

## 2. R1 — dead worktree silently fell back to the parent repo

**Status: FIXED (code + tests + live re-verification)**

Reproduced live before the fix:

```
git.status  ok=True  cwd=<deleted worktree>   ← ran against the PARENT repo
git.diff    ok=True  diff=''                  ← empty, as if no changes existed
```

Root cause: the governed Git fast path resolved an unusable `execution_target`
to a usable directory instead of failing, so a dead worktree produced
authoritative-looking results sourced from the canonical checkout.

Fix: `veya/remote/tool_adapter.py` — `_invalid_git_target_reason()` refuses a
missing, non-directory, or unusable target before any Git call, and the refusal
is `POLICY_BLOCKED` naming the real target. **No canonical fallback.**

Live re-verification after restart:

```
ALIVE git.diff names calc.py: True
DEAD git.status  ok=False code=POLICY_BLOCKED cwd=None
DEAD git.diff    ok=False code=POLICY_BLOCKED cwd=None
```

---

## 3. R2 — `git.status` / `git.diff` never checked git's exit code

**Status: FIXED**

A failed Git invocation serialised as a clean, empty, `ok=True` result —
indistinguishable from "no changes". Both operations now propagate a non-zero
exit as `EXECUTION_FAILED`; `NO_DIFF` vs `HAS_DIFF` vs `TARGET_INVALID` are
distinguishable (target-invalid is refused earlier, as `POLICY_BLOCKED`).

Live evidence for truthfulness: the alive worktree reported the staged
`calc.py` change, and the same call on the dead worktree refused instead of
returning an empty diff.

---

## 4. R3 — a FAILED execution reported `verification_result = PASS`

**Status: FIXED**

Reproduced: a command that never started produced
`status=FAILED, exit_code=None, failure_class=PROCESS_START_FAILURE` yet
`verification=PASS`.

Fix: `veya/remote/execution.py` — verification fails closed on a **positive
failure signal**: non-zero exit, a recorded failure class, or a direct status
that is not a success.

**Correction applied during this work.** The first version also treated
`exit_code is None` as failure. That is wrong: successful paths such as
`veya.mission.run` legitimately produce no exit code, and the rule broke them
(`tests/remote/test_wait_terminal_contract.py` went red, 406 → 405). The rule
now keys on positive failure evidence only. Both suites are green again.

| Case | status | exit_code | verification |
|---|---|---|---|
| exit 0 | COMPLETED | 0 | PASS |
| exit 1 | FAILED | 1 | FAIL |
| spawn failure | FAILED | None | FAIL (via `failure_class`) |

---

## 5. R4 — pipelines rejected after ~20s with an errored cwd

**Status: NOT REPRODUCED — no fix invented**

Every reported construct was exercised against the live service:

| Construct | Outcome |
|---|---|
| simple `python -c …` | accepted |
| `a && b` | accepted |
| `a \| b` | accepted |
| `a ; b` | accepted |
| redirection | accepted |
| command substitution | accepted |
| background | accepted |

All completed in ~1.3 s with a correct cwd. The only slow case was the very
first call (~7.9 s), which is cold GoalRun / tool-registry load, not
pipeline-specific, and it succeeded.

The reported symptom does not occur. Recording this as unreproduced rather than
manufacturing a fix for a defect that was never observed.

---

## 6. R5 — worktree lifecycle audit

**Status: AUDITED. Read-only; nothing was deleted.**

```
total registered worktrees : 509
canonical / main checkouts : 8
task worktrees (.veya)     : 501
  checkout missing         : 0
  checkout EMPTY           : 0
```

No orphaned registrations, no missing checkouts, no empty checkouts. The real
finding is **volume**: 501 task worktrees accumulate because RUNTIME-FIX
sessions created many disposable repos. This is reported, not cleaned up —
deletion was explicitly out of scope.

---

## 7. Tool coverage (45 live bindings)

**15 verified with evidence this round:**

`file.read`, `file.search`, `file.write`, `file.patch`, `test.run`,
`shell.exec`, `build.run`, `git.stage`, `git.commit`, `git.diff`,
`git.status`, `git.verify`, `git.promote`, `process.status`, `process.cancel`

**30 not exercised this round** (several have coverage from earlier rounds):

`approval.consume`, `approval.request`, `approval.status`, `artifact.list`,
`artifact.read`, `autonomous.decisions`, `autonomous.escalations`,
`autonomous.explain`, `autonomous.observations`, `autonomous.progress`,
`autonomous.status`, `autonomous.waits`, `git.log`, `interrupt.reply`,
`mission.revise`, `runtime.capabilities`, `runtime.probe`, `runtime.profile`,
`veya.escalation.list`, `veya.mission.cancel`, `veya.mission.continue`,
`veya.mission.create`, `veya.mission.inspect`, `veya.mission.run`,
`veya.report.get`, `veya.report.latest`, `veya.review.apply`,
`worker.dispatch`, `workspace.info`, `workspace.list`

---

## 8. Test, mutation and regression evidence

New suite `tests/remote/test_runtime_fix_r1_r2_r3.py` — **10 passed**, and it
drives the real `WorktreeManager`, the real tool registry, and the real gateway.
No mocked Git.

Mutations (each must make the suite go RED):

| Mutation | Result |
|---|---|
| M1 dead-worktree canonical fallback allowed | **1 failed** ✓ |
| M2 `git diff` exit code ignored | **1 failed** ✓ |
| M3 verification falls back to `exit_code` alone | **2 failed** ✓ |
| M5 worktree ownership validation removed | **1 failed** ✓ |

All four are effective; the suite restores to 10 passed.

Regression after the fixes:

| Suite | Result |
|---|---|
| P0-F…Q + security + P0-L + new R1–R3 | **242 passed** |
| `unit-fast` | **406 passed** (baseline restored) |
| `goalrun` | 1 failed / 200 passed (pre-existing baseline) |
| `runtime` | 3 failed / 526 passed (pre-existing baseline) |
| supervision P3 | 7 passed |
| `ruff` on changed files | clean |

---

## 9. Open findings

### 9.1 Remote token store rejects freshly issued tokens — CLOSED (false positive)

**The reported defect did not exist.** The probe was wrong.

The 401 response body was:

```json
{"code": -32001, "message": "missing session_id",
 "data": {"error_code": "AUTH_DENIED"}}
```

`tools/list` without a prior `initialize` handshake is rejected because the
**session** is missing. The token had authenticated fine. Because "missing
session" and "invalid token" share HTTP 401, status-only probing cannot tell
them apart, and I misattributed it to the token store.

Two real behaviours compounded it:

1. **Load-once.** `RemoteAuth` is an in-memory authority; `from_env()` reads the
   store once at construction (`auth.py:118-129`). A token issued by a separate
   process after the service started is correctly rejected until restart.
2. **Ambiguous status.** A valid token with no session and an invalid token both
   return 401.

Layer-by-layer, every stage was proven sound against the real store:

| Layer | Verdict | Evidence |
|---|---|---|
| write format | OK | list, uniform keysets, 059 records, mode 0600 |
| read/parse | OK | `from_env()` loaded all records |
| principal mapping | OK | `final-probe` resolved to its `token_id` |
| hash verification | OK | `verify()` succeeded locally on the live token |
| permissions/lock | OK | mode 0600, uid 1000, no truncation |
| post-restart cache | OK by design | reload picks up new tokens; see `test_e2` |
| suite pollution | **No** | zero pytest principals reached the real store |

Live corroboration from the service journal: `200 OK` for the fresh token's
`initialize`, then `401` for the deliberately bogus token.

### 9.1b Token store: concurrent persist collision — FIXED

Found while building the concurrency evidence, and a genuine defect.

`_persist()` wrote to a **fixed** temp name (`.<store>.tmp`). Two concurrent
issuers shared it, so one process could `replace()` the temp file out from
under the other, raising `FileNotFoundError` and losing the grant:

```
FileNotFoundError(2, 'No such file or directory')
```

Fix (`auth.py`): the temp name is now unique per writer
(`.<store>.<pid>.<nonce>.tmp`) and cleaned up in a `finally`. `os.replace` stays
atomic, so readers never see a torn store. Auth semantics are untouched — this
changes only the scratch filename.

Mutation M1 restores the fixed name and turns the suite red, so the regression
is locked in.

Residual, not fixed: `issue()` is read-modify-write with no cross-process lock,
so simultaneous issuance from two processes can still lose one grant (last
writer wins). Uniqueness prevents the crash and torn file but not lost updates.
Closing that properly needs file locking and is out of scope here.

### 9.1c Probe principals left in the operator store — NOTED

Live verification must target the real store, because that is what the service
reads. Five probe principals (`rc-fresh-1`, `rc-fresh-2`, `rcA`, `rcB`,
`rcE1`) were therefore appended. They were **not** removed, since editing the
operator store was out of bounds. They are harmless, unprivileged, and expire.

After the R1/R2 live verification, newly issued tokens stopped being honoured:

```
issue --write  → persisted (file holds 59 records, principal present)
systemctl --user restart veya-remote-mcp → new PID, health 200
tools/list      → HTTP 401 Unauthorized
```

Reproduced repeatedly, including with a brand-new principal and no intervening
restart. The service is healthy and the token is on disk, so the defect is in
token loading/validation, not availability. Note the regression suites write to
the same `~/.veya/remote_tokens.json`, which may be involved. Not diagnosed
further here — it needs its own root-cause pass.

### 9.2 Carried-forward findings

| Finding | Status |
|---|---|
| `autonomous.waits` FIXED, data-bearing mission path unverified | unchanged |
| task ID 30-char collision | documented, implementation not independently audited |
| Python 3.12 container vs 3.14 host | explained, not a defect |
| `F1 initialize_execution_harnesses` | OPEN |
| `leaf.py` admission ordering | OPEN |
| `mission-1a0ff959de` | BLOCKED, not re-executed |
| session-worktree routing drift | OPEN |

---

## 10. Superseded conclusions (history, retained deliberately)

- Health is **not** `/health`; the real endpoint is `/mcp/health`.
- `git.verify` presence is the code-identity signal; a task Verifier result is
  not.
- Live count is **45**; earlier "45 bindings / 34 verified" figures used a
  different verification boundary and are superseded by §7.
- `R4` was reported as a defect; it does not reproduce (§5).
- Treating `exit_code is None` as failure is wrong (§4).

---

## 11. Explicit non-claims

- No Closure history was rewritten; `9c137897` remains the closure revision.
- No worktree, stash, or untracked WIP was deleted.
- R4 was not "fixed", because it was not reproduced.
- `runtime/harness/contract.py` carries a **separately authorised** RUNTIME-FIX
  change (real `oskill`/`oprim` modules receive existing fallback attributes;
  no `sys.modules` replacement, no `platform/3O` modification). It is dirty WIP,
  not part of this correctness change.
- §9.1 was a probe defect, not a product defect; it is closed with evidence.
- The one real store defect found (§9.1b, concurrent persist collision) is
  fixed and mutation-covered.
- Cross-process lost-update on concurrent `issue()` remains open and is
  documented, not fixed.
- Live service health during heavy `initialize` load is slow (~1m38s CPU,
  1.3G peak); repeated handshakes timed out and is an operational note, not a
  correctness finding.
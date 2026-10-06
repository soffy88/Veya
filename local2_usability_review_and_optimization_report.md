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

### 9.1 Remote token store rejects freshly issued tokens — OPEN, HIGH

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
- The 401 in §9.1 is unresolved. Runtime correctness for R1–R3 is verified
  independently of it.
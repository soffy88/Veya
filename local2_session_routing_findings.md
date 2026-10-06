# Local2 Session-Worktree Routing Findings

**STATUS: SR-002 CLOSED · SR-004 CLOSED · SR-001 CLOSED · SR-003 CLOSED ·
SR-005 OPEN** (by design — separate phase).

Phase baseline for SR-001/SR-003: `15e7a639`. SR-002 and SR-004 are implemented, closure-safe, and
mutation-covered. The SR-001/003/005 work attempted earlier was reverted and is
recorded below; `execution_id` precedence was never modified.

Reproduction: `tests/remote/test_session_worktree_routing.py` (13 tests, not in
CI). Against baseline: **8 failed, 5 passed**.

---

## SR-001 — Explicit `CANONICAL_WORKTREE` reads are answered from the session worktree

**Root cause.** `file.read` / `file.search` reach `_base_dir()` (tool_adapter.py:2191)
and `_resolve_target()` (2471), neither of which accepted `execution_target`.
`_resolve_target` remapped into `session.worktrees[...]` unconditionally:

```python
worktree = session.worktrees.get(resolution.repo_root)
if worktree:
    target = Path(worktree) / target.relative_to(...)
```

**Repro.** A file written only into the worktree was returned by a canonical read.
**Negative test.** `test_r1_canonical_read_is_not_served_from_session_worktree`.
**Affected seam.** read path only; Git was a separate seam (SR-003).

## SR-002 — `CURRENT_SESSION_WORKTREE` did not exist

**Root cause.** `EXECUTION_TARGETS` (825) had no such member, so
`resolve_execution_target()` rejected it as `INVALID_ARGUMENT`. R2 was
unimplementable, and callers had no way to *ask* for their session tree —
which is how the silent substitution in SR-001/SR-003 went unnoticed.

**Negative test.** `test_r2_current_session_worktree_target_is_supported`.

## SR-003 — Dead session worktree silently degraded to canonical

**Root cause.** `_base_dir()` popped the dead registration and returned the
workspace:

```python
if Path(mapped).exists():
    return mapped
session.worktrees.pop(str(repo), None)   # then falls through to workspace
```

A read against a deleted worktree returned **canonical content**, `ok=True`.

**Negative test.** `test_r3_dead_session_worktree_never_falls_back_to_canonical`.

## SR-004 — Non-checkout directory accepted as a Git target

**Root cause.** `_invalid_git_target_reason()` (201) only checked
`exists()` / `is_dir()`. An empty directory passed, so git ran against whatever
git resolved — i.e. the parent repository.

**Negative test.** `test_r4_target_validator_rejects_non_checkout`.

## SR-005 — `execution_id` could relocate a pinned request

`_direct_workdir()` resolved `execution_id` unconditionally, before any target
consideration. **Not yet closed** — the added test
(`test_r8b_execution_id_cannot_override_explicit_target`) passes on baseline only
because a fabricated id raises rather than silently relocating; the seam is not
yet proven to prefer an explicit target.

---

## Why the fix was reverted

A working fix was implemented (thread `execution_target` through
`_base_dir` / `_resolve_target` / `_direct_workdir`, add
`CURRENT_SESSION_WORKTREE`, require `.git`, derive `repo_root` from the target).
It turned the suite fully green (14/14) but **broke 9 tests in the P0-L / P0-Q
closure chain** (`test_promote_target_is_not_chosen_by_execution_id`,
`test_qn9_fake_execution_id_manufactures_no_authority`,
`test_canonical_is_untouched_until_promotion`, …).

Those tests encode `CLOSURE_GATE=PASS`. Shipping a routing fix that reopens the
closure chain is a worse outcome than shipping no fix, so the change was
reverted (`git checkout -- veya/remote/tool_adapter.py`) and the baseline
re-verified green.

**Next step must be:** land SR-001..SR-004 one seam at a time, re-running
P0-L / P0-Q after each, so the closure chain is never broken mid-flight. The
P0-Q coupling is specifically `execution_id` precedence — that invariant must be
preserved deliberately, not overwritten.

## Mutation gate status: 4/8 RED — INCOMPLETE

| Mutation | Result |
|---|---|
| M1 canonical → session substitution | RED |
| M3 dead → canonical fallback | RED |
| M4 empty dir accepted as checkout | RED |
| M6 remap ignores explicit canonical | RED |
| M2 session → canonical substitution | GREEN — mutated an unreachable `_base_dir` branch |
| M5 git.diff failure → NO_DIFF | GREEN — mutation edited a message string, not the exit-code branch |
| M7 reported `repo_root` ≠ actual | not applied — pattern matched 5 sites |
| M8 `execution_id` overrides target | GREEN — no coverage existed; test added, seam still unproven |

Per §13, GREEN mutations mean incomplete coverage, not success.

## Not run

`unit-fast`, `goalrun`, `runtime`, `supervision`, `ledger` were **not** re-run
after the revert; the targeted remote subset was verified green (77 passed).
No worktree was deleted, no untracked file removed, no stash touched.


---

# Phase 2 — SR-002 + SR-004 (landed)

Only the two seams with zero closure coupling were touched.

## SR-002 — `CURRENT_SESSION_WORKTREE`

* Registered in `EXECUTION_TARGETS`.
* Resolved in `_direct_workdir` to the session's registered worktree, or
  `POLICY_BLOCKED` when the session has none. No canonical / cwd fallback.
* Placed **after** the `execution_id` branch so SR-005 precedence stays
  byte-identical (verified: the `execution_worktrees.resolve(...)` line has no
  `+`/`-` in the diff).
* Error-code note: the stable taxonomy has no `TARGET_INVALID` member, so
  `POLICY_BLOCKED` is used, consistent with the existing dead-worktree refusal.
  No new public error code was invented.

## SR-004 — non-checkout directory

`_invalid_git_target_reason` now requires Git working-tree identity via the
`.git` entry: a **directory** in a normal repository, a **file** in a linked
worktree. A linked worktree's pointer (`gitdir: ...`) is parsed and its
recorded metadata must exist, so a dangling pointer is refused instead of
degrading. No upward search: a non-git child of a valid repository is refused.

## Tests — 14 passed

`tests/remote/test_sr002_sr004.py`. Routing is proven with
`sentinel_worktree_only.txt`, which exists **only** in the session worktree;
`calc.py` (present in both trees) is never used as a routing oracle.

## Mutations — 4/4 RED

| Mutation | Result |
|---|---|
| M-SR2a remove `CURRENT_SESSION_WORKTREE` registration | RED |
| M-SR2b session target falls back to canonical | RED |
| M-SR4a allow empty dir to resolve parent repo | RED |
| M-SR4b accept dangling linked-worktree pointer | RED |

M-SR4b was initially GREEN; the dangling-pointer branch had no test. A test was
added rather than the branch deleted, then the mutation went red.

## Closure safety and regression

| Suite | Baseline | Result |
|---|---|---|
| P0-L + P0-Q | green | **28 passed** |
| unit-fast | 406 | **406 passed** |
| goalrun | 1F / 200P | **1F / 200P** |
| supervision | 7 | **7 passed** |
| runtime | 3F / 526P | **3F / 526P** |
| ledger | 24 | **24 passed** |
| ruff | clean | clean |

One pinned assertion in `test_p0j_wip_preservation.py` listed the exact
`EXECUTION_TARGETS` set and was updated to include the new identifier; the
default-asymmetry that test documents is unchanged.
`test_the_phase_commit_touches_only_its_own_paths` fails identically at baseline
and is pre-existing, not caused here.

Routing suite went 8 → 4 failures; the remaining 4 are exactly the
out-of-scope SR-001 / SR-003 seams.

## WIP preserved

`runtime/harness/contract.py` sha `b3f747b422136bf8` unchanged · stash 1 ·
509 worktrees · no untracked file removed · only addition is this phase's test
suite. No `reset` / `clean` / `stash` / worktree deletion.


---

# Phase 3 — SR-001 + SR-003 (landed, `15e7a639` -> HEAD)

## SR-001 — explicit canonical is no longer substituted

`_base_dir`, `_resolve_target` and `_direct_workdir` now all receive
`execution_target`. An explicit `CANONICAL_WORKTREE` / `HOST` resolves to the
canonical root on both the read and the git seam. The canonical branches sit
**after** the `execution_id` branch in each function, so SR-005 precedence is
untouched.

## SR-003 — a dead session worktree is refused, never degraded

New module-level helper `_session_worktree_unusable()` reuses the SR-004
`_invalid_git_target_reason` check, so a session worktree must be a *usable
checkout*, not merely present. That closes the shapes presence-only checking
missed:

| Case | Previously | Now |
|---|---|---|
| D1 missing path | fell back | POLICY_BLOCKED |
| D2 empty dir | fell back | POLICY_BLOCKED |
| D3 `.git` deleted | fell back | POLICY_BLOCKED |
| D4 dangling gitdir | fell back | POLICY_BLOCKED |
| D5 metadata missing | fell back | POLICY_BLOCKED |
| D6 registered, checkout invalid | fell back | POLICY_BLOCKED |

The old `session.worktrees.pop(...)` + fall-through-to-workspace behaviour is
gone, so a dead registration cannot silently become canonical.

## Routing oracles

Two sentinels, each in exactly one target: `canonical_only.txt` (canonical) and
`session_only.txt` (session worktree). Ordering is load-bearing — a linked
worktree inherits committed files, so `canonical_only.txt` is written *after*
the worktree exists. `calc.py` is never used as an oracle.

Tests: `tests/remote/test_sr001_sr003.py`, **19 passed**.
Routing suite: 14 tests, **13 passed** (was 4 failing).

## Response truthfulness — PARTIAL, documented

The `repo_root` reported by git responses is still `ws_binding.repo_root`. Making
it the actual Git root (`git_repo_root(target)`) fixes the last routing-suite
failure but **broke 8 tests including `test_promotion_receipt_claims_match_observed_git_state`**, so per the closure rule it was reverted.

Remaining known gap: `test_r8_reported_target_matches_reality` — for
`CURRENT_SESSION_WORKTREE` the reported `repo_root` is the canonical root while
the actual cwd is the worktree. Recorded as **SR-006**, not silently dropped.

## Mutation results — 8 RED, 3 not effective

RED: M1 canonical->session (read seam), M1b (git seam), M1c, M2 ignored
`execution_target`, M2'' remap ignores canonical, M4 empty dir -> parent, M5
invalid gitdir, M3-real dead session -> canonical.

**Not effective (GREEN), reported rather than counted:**

* mutating the `_base_dir` implicit dead-worktree branch
* mutating either `_resolve_target` dead-worktree guard

All three are defense-in-depth: the observable refusal is produced earlier (at
`_base_dir`'s explicit-session branch), so no governed-surface test can observe
these branches being removed. They are unproven, not proven-correct.

Per §2, `M-SR1c` (letting `execution_id` override an explicit canonical target)
is an SR-005 seam and is therefore **left open by design**, not fixed here.

## Closure protection and regression

| Suite | Baseline | Result |
|---|---|---|
| P0-L + P0-Q + SR-001/003 + SR-002/004 | green | **61 passed** |
| routing suite | 4 failed | **13 passed / 1 failed (SR-006)** |
| unit-fast | 406 | **406 passed** |
| goalrun | 1F / 200P | **1F / 200P** |
| supervision | 7 | **7 passed** |
| runtime | 3F / 526P | **3F / 526P** |
| ledger | 24 | **24 passed** |
| ruff | clean | clean |

`test_execution_defaults_to_the_canonical_worktree_by_design` still pins the
default as canonical; the default target was **not** changed (§12).

## WIP preserved

`runtime/harness/contract.py` sha `b3f747b422136bf8` unchanged · stash 1 ·
509 worktrees · no untracked file removed · no reset / clean / stash / worktree
deletion.


---

# Phase 4 — SR-005 CLOSED · SR-006 BLOCKED

## SR-005 — explicit target outranks execution_id (landed)

`_base_dir` and `_direct_workdir` now share one precedence order:

    explicit execution_target > execution_id > session implicit > default

An `execution_id` still identifies execution context but cannot relocate a
pinned request. **Both "no explicit target" matrix rows are preserved**: with no
explicit target the historical `execution_id` resolution runs exactly as before.

Tests: `tests/remote/test_sr005_precedence.py`, 6 passed, including the two
preservation rows. Closure after the seam: P0-L + P0-Q + SR-001/003 + SR-002/004
= **61 passed**.

Regression-neutrality was measured, not assumed: the three suspect suites
(`test_nested_repo_resolution`, `test_direct_fast_path`,
`test_local2_existing_worktree_wiring`) return an **identical 15 failed / 29
passed both with and without** the SR-005 change, and identically at baseline
`9d2bd0ca`. Those 15 failures are pre-existing and unrelated.

## SR-006 — BLOCKED, reverted, root cause identified

**Where the lie is.** For `CURRENT_SESSION_WORKTREE` the git payload reports:

| field | value | verdict |
|---|---|---|
| `cwd` | session worktree | truthful |
| `repo_root` | canonical root | repository *identity* — correct as such |
| `resolution.resolved_repo_root` | **canonical root** | **wrong** |

So the defect is not `repo_root`. It is the field literally named
*resolved*_repo_root, which is computed by `resolve_repo_target()` against the
**workspace binding**, before the git seam has chosen its target. Observation and
target identity therefore disagree.

**Why the two obvious fixes both fail.**

1. `payload["repo_root"] = git_repo_root(target)` — redefines an identity field
   as an observation field. Broke 8 tests including
   `test_promotion_receipt_claims_match_observed_git_state`.
2. Re-resolving the observation from the target via a new
   `_observation_for(target, ...)` — verified to fix SR-006 (the observation
   then names the worktree), but broke **16 tests**, because re-resolving
   re-selects the repository and therefore mis-resolves **nested repositories**.

Option 2 was implemented, measured, and **reverted per §13**. The nesting
constraint and the observation-identity requirement are in genuine tension: the
nested-repo selector depends on the binding, while observation identity depends
on the resolved target. Resolving that properly needs the resolved target to
become a first-class input to repo selection — a structural change to
`resolve_repo_target` and its callers, not a field edit. That is a separate phase
and must not be smuggled in here.

`test_r8_reported_target_matches_reality` is left **failing on purpose**: it
asserts the observation field against `git rev-parse --show-toplevel` and
therefore documents the open defect. Routing suite is 13/14, not 14/14.

## Not completed in this phase

* §4 sentinel matrix (`execution_only_<nonce>`) and gateway-level execution_id
  routing proof — SR-005 is verified at resolver level only.
* §5 git mutation precedence (stage/commit/verify/promote under a pinned target
  plus execution_id).
* §10 receipt Cases A–D.
* §12 mutation gates — **not run**. No mutation evidence exists for this phase,
  so none is claimed.
* §14 full regression beyond unit-fast / ledger / supervision.

# Local2 Session-Worktree Routing Findings

**STATUS: SR-002 PASS · SR-004 PASS** (committed). SR-001 / SR-003 / SR-005
remain open by design.

Phase baseline `82a0c89f`. SR-002 and SR-004 are implemented, closure-safe, and
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

# Local2 Session-Worktree Routing Findings

**STATUS: BLOCKED / PARTIAL** — drift confirmed and root-caused; the fix is
blocked by closure-chain regressions and is **not** committed.

Baseline `412e855b` (`LOCAL2_COMPLETE=YES`, `CLOSURE_GATE=PASS`).
`veya/remote/tool_adapter.py` is **unmodified**; the green baseline is intact.

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

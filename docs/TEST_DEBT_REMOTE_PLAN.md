# Remote test-debt cleanup plan (fix/test-debt)

## Goal and acceptance

Make the 20 in-scope files under `tests/remote/` reflect verified current runtime
behavior, with genuine external-service requirements expressed as precise
conditional skips rather than unconditional skips or deletions. Acceptance:
each changed file passes with `/data/soffy/projects/veya/.venv/bin/python -m
pytest <file>` from this worktree with a 600s timeout; unchanged files are
reported with their classification; no source outside `tests/remote/` is edited.

## Constraints

- Only the 20 named test files may be modified. No production code, no
  `local2`, `veya/remote/*`, `scripts/check_no_direct_io.py`, `apps/web`, or
  `/data/soffy/projects/veya` (except its `.venv/bin/python`).
- No push, deploy, restart, unconditional skip, or test deletion. Genuine
  external prerequisites may use conditional skips tied to a specific probe.
- Other agents may be editing other worktrees; never revert their changes.

## Approach

1. Read each failure against current production code and reproduce locally.
   Classify every file as: stale test (API/contract drift), real defect
   (production behavior violates a defensible contract; report, don't edit
   production), external dependency (binary/service/credential required), or
   hang (unbounded wait). One file may have failures of more than one class;
   report per file and per test.
2. For stale assertions, update the test to current verified behavior while
   preserving the original invariant's intent. For environment-dependent
   assertions, gate on an explicit capability probe with a precise reason.
   For unfixable production defects, leave the test intact and report the
   defect with file:line evidence.
3. Verify: run each changed file individually, then run the whole changed set
   together; compare against `/tmp/vt/base.tsv` summaries. Use `ruff check`
   and `ruff format --check` on changed files.
4. Handoff: final answer lists changed paths, classification per file, and
   any out-of-scope defects that remain.

## Fragile assumption

The assumption is that current production code in this worktree is correct
unless a test demonstrates a defensible behavioral regression. If a failing
test encodes a user-confirmed invariant that production broke (for example
credential redaction or WIP preservation), it is classified as a real defect
and reported rather than rewritten.

## Rejected alternatives

- Rewriting production code: outside scope and violates the frozen mainline
  approval rule.
- Marking whole files `skip`: destroys coverage and is explicitly forbidden.
- Pinning tests to historical commits: does not verify current behavior.

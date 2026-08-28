# Coding Harness Contract

Every Veya coding worktree receives a
`.veya/runs/<task_id>/inputs/harness_contract.json` contract before code
execution.

The contract records:

- `workspace_id` and the guide source paths available to the task;
- required and optional sensor ids;
- permission and observability profiles;
- the workspace memory scope;
- the task-scoped artifact policy.

The contract answers which guides may be read, which deterministic sensors must
pass, which writes are limited to the isolated worktree, which approvals are
needed, where artifacts are written, and what evidence is sufficient for a
verified result. A failed acceptance produces a Ratchet candidate with evidence;
it never changes a guide or sensor by itself.

`coding_finalize_patch` includes the contract and sensor report in the existing
ArtifactManifest and writes `verification_report.json`, `sensor_report.json`,
`patch.diff` and `summary.md`. Commit, push and remote PR operations remain
outside this P0 harness layer.

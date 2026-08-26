# Veya Execution Runtime 1.0 — Crash-Safe Durable Execution

This implementation is the Veya Execution Runtime 1.0 crash-safe durable
authority. The semantic path remains:

```text
MasterAgent → GoalRun → durable work item → WorkerHost → Fan-In/finalization
```

## Implemented foundation

- `runtime/execution/durable.py`: PostgreSQL/SQLite repository API for goal runs,
  work items, attempts, leases, fencing tokens, completion deduplication,
  cancellation, outbox, and event cursors.
- `runtime/execution/schema.py` (schema version 3) and
  `runtime/execution/migrations/001_execution_runtime.sql`:
  durable queue, lease, attempt, side-effect, artifact, event/outbox, recovery,
  finalization, migration-comparison, and worker tables.
- `runtime/personal/runtime.py`: durable MemoryRecord v2 candidates/commit and
  correction, SkillSpec v2 version/run/rollback, ContinuitySnapshot projection,
  LearningRecord threshold/eval gate, and personal event/outbox projection.
- `runtime/execution/worker.py`: claim → start → heartbeat → complete/fail worker
  boundary; stale owners cannot commit.
- `runtime/execution/reconciler.py`: bounded startup/periodic expired-lease
  reconciliation with explicit retry/quarantine/recovery decisions.
- `runtime/execution/side_effects.py`: record-before-call operation-key protocol,
  request-hash conflict detection, provider probe, and unknown-outcome hold.
- Finalization is a durable logical work item with immutable Fan-In snapshot,
  checkpoint stages, idempotent result commit, and resume support.

## Production activation

Production durable authority is PostgreSQL. The deployment injects a dedicated
Veya database and least-privilege role through the ignored root `.env`; the
password and DSN are never committed. Start/restart with the root environment
explicitly selected:

```text
VEYA_DURABLE_EXECUTION=1
VEYA_EXECUTION_DATABASE_URL=postgresql://...
VEYA_EXECUTION_PRODUCTION=1
VEYA_EXECUTION_DURABLE_QUEUE_READ=1
VEYA_EXECUTION_DURABLE_QUEUE_CLAIM=1
VEYA_EXECUTION_LEASE_FENCING=1
VEYA_EXECUTION_SIDE_EFFECT_LEDGER=1
VEYA_EXECUTION_RECONCILER=1
VEYA_EXECUTION_FINALIZATION_RESUME=1
VEYA_EXECUTION_EVENT_OUTBOX=1
```

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d --no-build backend
```

Startup runs the schema migration and reconciliation before any durable claim.
The runtime rejects a production configuration without a PostgreSQL DSN and
rejects SQLite when `production=true`. SQLite remains available for unit tests,
local development, and single-process fallback only; it does not provide the
production multi-process guarantee.

Health: `GET /health/execution-runtime` reports `enabled`, `authority`,
`db_connected`, `schema_version`, `queue_depth`, `active_leases`,
`expired_leases`, `pending_outbox`, `quarantined_count`, reconciler status, and
shared durable counters. A healthy production response must include
`enabled=true`, `authority=postgresql`, `db_connected=true`, and
`healthy=true`.

## 1.0 durability contract

- Queue delivery is at-least-once. Completion is idempotent by logical work
  item and result hash; duplicate completion never replaces the first result.
- Every claim has a lease, heartbeat, process-incarnation worker identity, and
  monotonically increasing fencing token. All lease-bound mutations validate
  owner and token. A stale owner receives `STALE_FENCE`.
- A side-effecting operation uses a stable operation key and request hash.
  `committed` results are read from the ledger, unknown results are probed when
  the provider supports it, and an inconclusive probe is quarantined/manual
  review. Unknown side effects are never blindly replayed.
- Completed child results, evidence, checkpoints, and immutable artifact
  references survive sibling failure or process loss. Fan-In reads durable
  snapshots rather than process memory.
- Finalization is a durable logical work item. Its checkpoints include the
  snapshot hash and stage; a restart resumes finalization without reopening
  research or spawning a new semantic branch.
- State changes and the append-only execution event are committed together
  with an outbox row. The publisher may retry; the existing EventStore/SSE
  projection deduplicates by event id. `Last-Event-ID`/cursor replay must be
  used after reconnect.

## Crash recovery and operations

On startup, the reconciler takes a database advisory lock, scans expired
leases, inspects attempts/side effects/artifacts, and records one explicit
decision: `COMPLETED_FROM_EVIDENCE`, `RETRY_SAFE`, `IDEMPOTENT_RETRY`,
`QUARANTINED_UNKNOWN`, `COMPENSATION_REQUIRED`, or `MANUAL_REVIEW`. It never
deletes queue rows, attempts, artifacts, side-effect records, or events.

Useful checks:

```bash
curl -fsS http://127.0.0.1:8767/health/execution-runtime
docker exec hevi-postgres psql -U veya_runtime -d veya_runtime -c \
  "select version from execution_schema_meta order by version desc;"
docker exec hevi-postgres psql -U veya_runtime -d veya_runtime -c \
  "select state,count(*) from work_items group by state;"
docker exec hevi-postgres psql -U veya_runtime -d veya_runtime -c \
  "select count(*) from execution_outbox where published_at is null;"
```

If PostgreSQL is unavailable, stop claims and restore the database; do not set
the DSN to SQLite. After recovery, restart the backend and let reconciliation
repair dangling work. A stale worker is expected to fail closed. For an
unknown provider result, restore the provider/status probe or follow the
compensation/manual-review procedure; do not delete the ledger row.

## Personal Agent Runtime

Personal state uses the same PostgreSQL authority and schema version as GoalRun.
Production authority is therefore PostgreSQL; SQLite is only an isolated
local/development/test fallback and has no production cross-process guarantee.

Memory follows `candidate → normalize/dedup/conflict → commit`. An active
MemoryRecord includes scope, type, confidence, source event/session/task
references, provenance, timestamps, and lifecycle links. Correction creates a
new active record and marks the old record `superseded`; forget marks
`forgotten` without silently erasing audit history. Normal retrieval excludes
candidate and forgotten records and is only performed when MasterAgent calls
`memory_search`; there is no hidden all-memory prompt injection.
`GET /api/v1/memory/doctor` reports duplicate rate, orphan sources, invalid
scopes, low-confidence facts, and supersede cycles.

Skills use `skill_records` plus immutable `skill_versions`. Teaching creates a
candidate; explicit confirmation runs the deterministic safety gate and is
required before an executable version becomes `active/trusted`. Every run
records version, input, result status, acceptance, duration, artifacts, and
evidence. A failed run never rewrites a skill. Rollback selects an existing
audited version and preserves all history. Existing SkillHub AST, permission,
capability-scope, and sandbox controls remain the security authority; an LLM
semantic scan is advisory only.

Continuity is a projection assembled from shared history, task state, durable
memory/skill references, artifact manifests, checkpoints, and canonical events.
It is not a new fact source or coordinator. `GET /api/v1/continuity` returns
structured candidates and `POST /api/v1/tasks/{task_id}/continue` passes an
explicit continuation context to the existing MasterAgent path.

Learning has a hard threshold of three independent task IDs unless the user
explicitly teaches a behavior. A candidate must pass an offline/replay
comparison before it can be applied; one failure cannot change a prompt or
skill. `learning_records` and `learning_evals` preserve observation,
hypothesis, evidence, baseline/candidate references, and gate status.
If an applied learning later fails an evaluation, it becomes `degraded`; an
auditable `learning_rollback` capability can disable it while retaining the
evaluation evidence. No learning path silently rewrites a Skill or prompt.

The Personal Context page displays Remembered, Skills, Continue where you left
off, and Learning candidates. It exposes correction/forget and never displays
synthetic thinking states. Personal events are written transactionally with a
personal outbox; outbox replay projects them into the existing SSE/EventStore
with event-id deduplication.

Personal health is `GET /health/personal-runtime` and reports `enabled`,
`backend`, `authority`, `healthy`, `schema_version`, `pending_outbox`, durable
counts, and feature flags. It never returns a password or DSN.

Operator rules: inspect a memory's source before correcting or forgetting it;
keep unsafe skills `blocked/review_required`; preserve pending outbox rows and
restart/repair the publisher; and let structured continuation candidates flow
to MasterAgent instead of adding a keyword router. Legacy JSON stores remain
compatibility inputs only during the bounded migration drain and are not the
production authority after the v2 read switch.

Rollback is cohort-scoped: stop new durable claims, preserve the durable
records, allow active leases to expire/reconcile, and route only explicitly
compatible new work through the time-bounded 0.9 compatibility flag. Never
delete durable history or enable SQLite as a shared production queue.

## Release evidence

The production validation covers PostgreSQL enqueue/claim/heartbeat/completion,
global multi-process concurrency, stale fencing, side-effect probe and
quarantine cases, outbox replay after a process crash, artifact path/hash
re-association, and `SIGKILL` recovery of both child execution and finalization.
The tested runtime keeps the single MasterAgent path; GoalRun is only the
durable execution authority. Remote worker execution is a compatible
post-1.0 extension and is not a 1.0 release gate.

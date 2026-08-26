-- Veya Execution Runtime 1.0 durable schema.
-- The Python repository is the executable migration source; this SQL mirror
-- is provided for operators and migration tooling that require a SQL artifact.
-- Timestamps are UTC epoch seconds (BIGINT/DOUBLE compatible with the
-- repository); event and payload JSON is validated/canonicalized by Python.

CREATE TABLE IF NOT EXISTS execution_schema_meta (
    version BIGINT PRIMARY KEY,
    applied_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS goal_runs (
    id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    root_run_id TEXT NOT NULL,
    master_agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_version BIGINT NOT NULL DEFAULT 1,
    budget_json TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    cancellation_requested_at DOUBLE PRECISION,
    finalization_state TEXT NOT NULL DEFAULT 'not_started',
    finalization_item_id TEXT,
    result_artifact_id TEXT,
    revision BIGINT NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL REFERENCES goal_runs(id),
    parent_work_item_id TEXT,
    logical_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    dependency_json TEXT NOT NULL,
    parallel_intent TEXT NOT NULL DEFAULT 'serial',
    payload_json TEXT NOT NULL,
    input_manifest_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    side_effect_policy TEXT NOT NULL DEFAULT 'none',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    next_ready_at DOUBLE PRECISION NOT NULL,
    lease_owner TEXT,
    lease_token BIGINT,
    lease_expires_at DOUBLE PRECISION,
    last_heartbeat_at DOUBLE PRECISION,
    result_json TEXT,
    result_hash TEXT,
    error_json TEXT,
    checkpoint_id TEXT,
    recovery_state TEXT NOT NULL DEFAULT 'none',
    revision BIGINT NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE (goal_run_id, logical_key)
);

CREATE TABLE IF NOT EXISTS work_attempts (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(id),
    attempt_no INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    process_id TEXT NOT NULL,
    lease_token BIGINT NOT NULL,
    state TEXT NOT NULL,
    started_at DOUBLE PRECISION,
    last_heartbeat_at DOUBLE PRECISION,
    ended_at DOUBLE PRECISION,
    input_hash TEXT NOT NULL,
    result_hash TEXT,
    error_json TEXT,
    unknown_reason TEXT,
    usage_json TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE (work_item_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS execution_leases (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id),
    owner_id TEXT NOT NULL,
    token BIGINT NOT NULL,
    acquired_at DOUBLE PRECISION NOT NULL,
    heartbeat_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    released_at DOUBLE PRECISION,
    release_reason TEXT
);

CREATE TABLE IF NOT EXISTS side_effects (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL REFERENCES goal_runs(id),
    work_item_id TEXT NOT NULL REFERENCES work_items(id),
    operation_key TEXT NOT NULL UNIQUE,
    operation_type TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    state TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    provider_request_id TEXT,
    probe_policy TEXT,
    probe_result_json TEXT,
    compensation_json TEXT,
    first_seen_at DOUBLE PRECISION NOT NULL,
    last_seen_at DOUBLE PRECISION NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL REFERENCES goal_runs(id),
    work_item_id TEXT,
    kind TEXT NOT NULL,
    content_uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    mime_type TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'internal',
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE (goal_run_id, content_hash, content_uri)
);

CREATE TABLE IF NOT EXISTS artifact_manifests (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL REFERENCES goal_runs(id),
    version BIGINT NOT NULL,
    manifest_hash TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE (goal_run_id, version),
    UNIQUE (goal_run_id, manifest_hash)
);

CREATE TABLE IF NOT EXISTS execution_events (
    id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    goal_run_id TEXT NOT NULL,
    sequence_no BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL DEFAULT 1,
    event_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    trace_id TEXT,
    occurred_at DOUBLE PRECISION NOT NULL,
    UNIQUE (aggregate_type, aggregate_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS execution_outbox (
    event_id TEXT PRIMARY KEY REFERENCES execution_events(id),
    destination TEXT NOT NULL,
    published_at DOUBLE PRECISION,
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_decisions (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    attempt_id TEXT,
    observed_state TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE (work_item_id, attempt_id, decision)
);

CREATE TABLE IF NOT EXISTS finalization_checkpoints (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    stage TEXT NOT NULL,
    output_hash TEXT,
    included_child_sequence BIGINT NOT NULL DEFAULT 0,
    checkpoint_json TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE (goal_run_id, snapshot_hash, stage)
);

CREATE TABLE IF NOT EXISTS runtime_migrations (
    id TEXT PRIMARY KEY,
    flag TEXT NOT NULL,
    phase TEXT NOT NULL,
    cohort TEXT NOT NULL,
    started_at DOUBLE PRECISION NOT NULL,
    ended_at DOUBLE PRECISION,
    operator TEXT NOT NULL,
    rollback_marker TEXT
);

CREATE TABLE IF NOT EXISTS shadow_comparisons (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL,
    legacy_hash TEXT NOT NULL,
    durable_hash TEXT NOT NULL,
    diff_class TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS dual_run_comparisons (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL,
    authoritative_hash TEXT NOT NULL,
    shadow_hash TEXT NOT NULL,
    artifact_diff TEXT NOT NULL,
    latency_ms BIGINT NOT NULL DEFAULT 0,
    usage_json TEXT NOT NULL,
    classification TEXT NOT NULL,
    reviewer_status TEXT NOT NULL DEFAULT 'pending',
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_registry (
    worker_id TEXT PRIMARY KEY,
    process_id TEXT NOT NULL,
    state TEXT NOT NULL,
    incarnation_id TEXT NOT NULL,
    started_at DOUBLE PRECISION NOT NULL,
    last_seen_at DOUBLE PRECISION NOT NULL,
    draining_at DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS work_items_ready_idx ON work_items (state, next_ready_at, priority, created_at);
CREATE INDEX IF NOT EXISTS work_items_goal_idx ON work_items (goal_run_id, state);
CREATE INDEX IF NOT EXISTS work_items_lease_idx ON work_items (lease_expires_at);
CREATE INDEX IF NOT EXISTS attempts_item_idx ON work_attempts (work_item_id, attempt_no);
CREATE INDEX IF NOT EXISTS leases_expiry_idx ON execution_leases (expires_at);
CREATE INDEX IF NOT EXISTS outbox_ready_idx ON execution_outbox (published_at, next_attempt_at);
CREATE INDEX IF NOT EXISTS events_goal_idx ON execution_events (goal_run_id, occurred_at, sequence_no);
CREATE INDEX IF NOT EXISTS recovery_pending_idx ON recovery_decisions (goal_run_id, created_at);

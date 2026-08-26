"""Schema definitions for the crash-safe execution repository.

The repository stores JSON payloads as text in both backends.  This keeps the
SQLite test backend and PostgreSQL adapter byte-for-byte compatible while the
repository validates and hashes structured payloads at its boundary.  The
PostgreSQL migration is intentionally standalone so it can later be promoted
to the project's migration runner without changing the runtime API.
"""

from __future__ import annotations

# v2 widens PostgreSQL epoch columns from REAL to DOUBLE PRECISION. A 32-bit
# PostgreSQL REAL cannot represent sub-minute lease precision at current Unix
# epoch values and would silently shorten leases, breaking heartbeat/fencing.
SCHEMA_VERSION = 2

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS execution_schema_meta (
        version INTEGER PRIMARY KEY,
        applied_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS goal_runs (
        id TEXT PRIMARY KEY,
        parent_run_id TEXT,
        root_run_id TEXT NOT NULL,
        master_agent_id TEXT NOT NULL,
        status TEXT NOT NULL,
        plan_version INTEGER NOT NULL DEFAULT 1,
        budget_json TEXT NOT NULL,
        acceptance_json TEXT NOT NULL,
        cancellation_requested_at REAL,
        finalization_state TEXT NOT NULL DEFAULT 'not_started',
        finalization_item_id TEXT,
        result_artifact_id TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_items (
        id TEXT PRIMARY KEY,
        goal_run_id TEXT NOT NULL,
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
        next_ready_at REAL NOT NULL,
        lease_owner TEXT,
        lease_token INTEGER,
        lease_expires_at REAL,
        last_heartbeat_at REAL,
        result_json TEXT,
        result_hash TEXT,
        error_json TEXT,
        checkpoint_id TEXT,
        recovery_state TEXT NOT NULL DEFAULT 'none',
        revision INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (goal_run_id, logical_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_attempts (
        id TEXT PRIMARY KEY,
        work_item_id TEXT NOT NULL,
        attempt_no INTEGER NOT NULL,
        worker_id TEXT NOT NULL,
        process_id TEXT NOT NULL,
        lease_token INTEGER NOT NULL,
        state TEXT NOT NULL,
        started_at REAL,
        last_heartbeat_at REAL,
        ended_at REAL,
        input_hash TEXT NOT NULL,
        result_hash TEXT,
        error_json TEXT,
        unknown_reason TEXT,
        usage_json TEXT,
        created_at REAL NOT NULL,
        UNIQUE (work_item_id, attempt_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_leases (
        work_item_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        token INTEGER NOT NULL,
        acquired_at REAL NOT NULL,
        heartbeat_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        released_at REAL,
        release_reason TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS side_effects (
        id TEXT PRIMARY KEY,
        goal_run_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        operation_key TEXT NOT NULL UNIQUE,
        operation_type TEXT NOT NULL,
        target_ref TEXT NOT NULL,
        state TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        provider_request_id TEXT,
        probe_policy TEXT,
        probe_result_json TEXT,
        compensation_json TEXT,
        first_seen_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY,
        goal_run_id TEXT NOT NULL,
        work_item_id TEXT,
        kind TEXT NOT NULL,
        content_uri TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        mime_type TEXT NOT NULL,
        visibility TEXT NOT NULL DEFAULT 'internal',
        created_at REAL NOT NULL,
        UNIQUE (goal_run_id, content_hash, content_uri)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_manifests (
        id TEXT PRIMARY KEY,
        goal_run_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        manifest_hash TEXT NOT NULL,
        artifact_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (goal_run_id, version),
        UNIQUE (goal_run_id, manifest_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_events (
        id TEXT PRIMARY KEY,
        aggregate_type TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        goal_run_id TEXT NOT NULL,
        sequence_no INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        event_version INTEGER NOT NULL DEFAULT 1,
        event_json TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        trace_id TEXT,
        occurred_at REAL NOT NULL,
        UNIQUE (aggregate_type, aggregate_id, sequence_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_outbox (
        event_id TEXT PRIMARY KEY,
        destination TEXT NOT NULL,
        published_at REAL,
        publish_attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        next_attempt_at REAL NOT NULL
    )
    """,
    """
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
        created_at REAL NOT NULL,
        UNIQUE (work_item_id, attempt_id, decision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS finalization_checkpoints (
        id TEXT PRIMARY KEY,
        goal_run_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL,
        stage TEXT NOT NULL,
        output_hash TEXT,
        included_child_sequence INTEGER NOT NULL DEFAULT 0,
        checkpoint_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (goal_run_id, snapshot_hash, stage)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_migrations (
        id TEXT PRIMARY KEY,
        flag TEXT NOT NULL,
        phase TEXT NOT NULL,
        cohort TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL,
        operator TEXT NOT NULL,
        rollback_marker TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shadow_comparisons (
        id TEXT PRIMARY KEY,
        goal_run_id TEXT NOT NULL,
        legacy_hash TEXT NOT NULL,
        durable_hash TEXT NOT NULL,
        diff_class TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dual_run_comparisons (
        id TEXT PRIMARY KEY,
        goal_run_id TEXT NOT NULL,
        authoritative_hash TEXT NOT NULL,
        shadow_hash TEXT NOT NULL,
        artifact_diff TEXT NOT NULL,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        usage_json TEXT NOT NULL,
        classification TEXT NOT NULL,
        reviewer_status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_registry (
        worker_id TEXT PRIMARY KEY,
        process_id TEXT NOT NULL,
        state TEXT NOT NULL,
        incarnation_id TEXT NOT NULL,
        started_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        draining_at REAL
    )
    """,
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS work_items_ready_idx ON work_items (state, next_ready_at, priority, created_at)",
    "CREATE INDEX IF NOT EXISTS work_items_goal_idx ON work_items (goal_run_id, state)",
    "CREATE INDEX IF NOT EXISTS work_items_lease_idx ON work_items (lease_expires_at)",
    "CREATE INDEX IF NOT EXISTS attempts_item_idx ON work_attempts (work_item_id, attempt_no)",
    "CREATE INDEX IF NOT EXISTS leases_expiry_idx ON execution_leases (expires_at)",
    "CREATE INDEX IF NOT EXISTS outbox_ready_idx ON execution_outbox (published_at, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS events_goal_idx ON execution_events (goal_run_id, occurred_at, sequence_no)",
    "CREATE INDEX IF NOT EXISTS recovery_pending_idx ON recovery_decisions (goal_run_id, created_at)",
)

SQLITE_SCHEMA = (*_TABLES, *_INDEXES)

# PostgreSQL must not use REAL for epoch timestamps: at 1.7e9 seconds its
# precision is several minutes. Keep SQLite's portable schema and widen the
# PostgreSQL DDL independently so lease TTL/heartbeat values remain exact.
POSTGRES_SCHEMA = tuple(
    statement.replace(" REAL", " DOUBLE PRECISION") for statement in SQLITE_SCHEMA
)

POSTGRES_UPGRADES = (
    "ALTER TABLE execution_schema_meta ALTER COLUMN applied_at TYPE DOUBLE PRECISION USING applied_at::double precision",
    "ALTER TABLE goal_runs ALTER COLUMN cancellation_requested_at TYPE DOUBLE PRECISION USING cancellation_requested_at::double precision",
    "ALTER TABLE goal_runs ALTER COLUMN created_at TYPE DOUBLE PRECISION USING created_at::double precision",
    "ALTER TABLE goal_runs ALTER COLUMN updated_at TYPE DOUBLE PRECISION USING updated_at::double precision",
    "ALTER TABLE work_items ALTER COLUMN next_ready_at TYPE DOUBLE PRECISION USING next_ready_at::double precision",
    "ALTER TABLE work_items ALTER COLUMN lease_expires_at TYPE DOUBLE PRECISION USING lease_expires_at::double precision",
    "ALTER TABLE work_items ALTER COLUMN last_heartbeat_at TYPE DOUBLE PRECISION USING last_heartbeat_at::double precision",
    "ALTER TABLE work_items ALTER COLUMN created_at TYPE DOUBLE PRECISION USING created_at::double precision",
    "ALTER TABLE work_items ALTER COLUMN updated_at TYPE DOUBLE PRECISION USING updated_at::double precision",
    "ALTER TABLE work_attempts ALTER COLUMN started_at TYPE DOUBLE PRECISION USING started_at::double precision",
    "ALTER TABLE work_attempts ALTER COLUMN last_heartbeat_at TYPE DOUBLE PRECISION USING last_heartbeat_at::double precision",
    "ALTER TABLE work_attempts ALTER COLUMN ended_at TYPE DOUBLE PRECISION USING ended_at::double precision",
    "ALTER TABLE work_attempts ALTER COLUMN created_at TYPE DOUBLE PRECISION USING created_at::double precision",
    "ALTER TABLE execution_leases ALTER COLUMN acquired_at TYPE DOUBLE PRECISION USING acquired_at::double precision",
    "ALTER TABLE execution_leases ALTER COLUMN heartbeat_at TYPE DOUBLE PRECISION USING heartbeat_at::double precision",
    "ALTER TABLE execution_leases ALTER COLUMN expires_at TYPE DOUBLE PRECISION USING expires_at::double precision",
    "ALTER TABLE execution_leases ALTER COLUMN released_at TYPE DOUBLE PRECISION USING released_at::double precision",
    "ALTER TABLE side_effects ALTER COLUMN first_seen_at TYPE DOUBLE PRECISION USING first_seen_at::double precision",
    "ALTER TABLE side_effects ALTER COLUMN last_seen_at TYPE DOUBLE PRECISION USING last_seen_at::double precision",
    "ALTER TABLE artifacts ALTER COLUMN created_at TYPE DOUBLE PRECISION USING created_at::double precision",
    "ALTER TABLE artifact_manifests ALTER COLUMN created_at TYPE DOUBLE PRECISION USING created_at::double precision",
    "ALTER TABLE execution_events ALTER COLUMN occurred_at TYPE DOUBLE PRECISION USING occurred_at::double precision",
    "ALTER TABLE execution_outbox ALTER COLUMN published_at TYPE DOUBLE PRECISION USING published_at::double precision",
    "ALTER TABLE execution_outbox ALTER COLUMN next_attempt_at TYPE DOUBLE PRECISION USING next_attempt_at::double precision",
    "ALTER TABLE recovery_decisions ALTER COLUMN created_at TYPE DOUBLE PRECISION USING created_at::double precision",
    "ALTER TABLE finalization_checkpoints ALTER COLUMN created_at TYPE DOUBLE PRECISION USING created_at::double precision",
    "ALTER TABLE runtime_migrations ALTER COLUMN started_at TYPE DOUBLE PRECISION USING started_at::double precision",
    "ALTER TABLE runtime_migrations ALTER COLUMN ended_at TYPE DOUBLE PRECISION USING ended_at::double precision",
    "ALTER TABLE shadow_comparisons ALTER COLUMN created_at TYPE DOUBLE PRECISION USING created_at::double precision",
    "ALTER TABLE dual_run_comparisons ALTER COLUMN created_at TYPE DOUBLE PRECISION USING created_at::double precision",
    "ALTER TABLE worker_registry ALTER COLUMN started_at TYPE DOUBLE PRECISION USING started_at::double precision",
    "ALTER TABLE worker_registry ALTER COLUMN last_seen_at TYPE DOUBLE PRECISION USING last_seen_at::double precision",
    "ALTER TABLE worker_registry ALTER COLUMN draining_at TYPE DOUBLE PRECISION USING draining_at::double precision",
)

-- Veya Personal Agent Runtime schema v3.
-- The live bootstrap is runtime.execution.schema.POSTGRES_SCHEMA; this file is
-- kept as the reviewable migration artifact for operators and fresh installs.
-- All JSON payloads are text to preserve SQLite/PostgreSQL ABI parity.

CREATE TABLE IF NOT EXISTS memory_records (
  id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
  memory_type TEXT NOT NULL, content TEXT NOT NULL,
  source_event_ids TEXT NOT NULL, source_session_ids TEXT NOT NULL,
  source_task_ids TEXT NOT NULL, provenance_json TEXT NOT NULL,
  confidence DOUBLE PRECISION NOT NULL, created_at DOUBLE PRECISION NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL, last_verified_at DOUBLE PRECISION,
  status TEXT NOT NULL, supersedes TEXT NOT NULL, superseded_by TEXT NOT NULL,
  tags TEXT NOT NULL, canonical_fingerprint TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 2
);
CREATE INDEX IF NOT EXISTS memory_scope_idx ON memory_records(scope_type, scope_id, status, updated_at);

CREATE TABLE IF NOT EXISTS memory_candidates (
  id TEXT PRIMARY KEY, proposed_content TEXT NOT NULL,
  scope_type TEXT NOT NULL, scope_id TEXT NOT NULL, memory_type TEXT NOT NULL,
  source_event_ids TEXT NOT NULL, source_session_ids TEXT NOT NULL,
  source_task_ids TEXT NOT NULL, confidence DOUBLE PRECISION NOT NULL,
  reason TEXT NOT NULL, conflicts_with TEXT NOT NULL,
  canonical_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 2
);
CREATE TABLE IF NOT EXISTS memory_edges (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
  edge_type TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
  UNIQUE(source_id, target_id, edge_type)
);
CREATE TABLE IF NOT EXISTS skill_records (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL, current_version INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL, UNIQUE(name, scope_type, scope_id)
);
CREATE TABLE IF NOT EXISTS skill_versions (
  id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, version INTEGER NOT NULL,
  description TEXT NOT NULL, trigger_examples TEXT NOT NULL,
  parameters_schema TEXT NOT NULL, execution_type TEXT NOT NULL,
  execution_ref TEXT NOT NULL, source_event_ids TEXT NOT NULL,
  source_task_ids TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
  trust_status TEXT NOT NULL, success_count INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0, partial_count INTEGER NOT NULL DEFAULT 0,
  last_run_at DOUBLE PRECISION, last_success_at DOUBLE PRECISION,
  parent_version INTEGER, status TEXT NOT NULL, safety_manifest TEXT NOT NULL,
  safety_scan TEXT NOT NULL, UNIQUE(skill_id, version)
);
CREATE TABLE IF NOT EXISTS skill_runs (
  id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, version INTEGER NOT NULL,
  task_id TEXT, trace_id TEXT, input_params TEXT NOT NULL,
  result_status TEXT NOT NULL, acceptance_result TEXT NOT NULL,
  duration_ms INTEGER NOT NULL DEFAULT 0, cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  artifacts TEXT NOT NULL, evidence TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS continuity_snapshots (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT,
  snapshot_json TEXT NOT NULL, source_event_cursor TEXT NOT NULL,
  generated_at DOUBLE PRECISION NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS learning_records (
  id TEXT PRIMARY KEY, pattern_id TEXT NOT NULL, scope TEXT NOT NULL,
  evidence_task_ids TEXT NOT NULL, evidence_trajectory_ids TEXT NOT NULL,
  observation TEXT NOT NULL, hypothesis TEXT NOT NULL,
  confidence DOUBLE PRECISION NOT NULL, candidate_type TEXT NOT NULL,
  proposed_change TEXT NOT NULL, eval_result TEXT NOT NULL,
  status TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
  applied_at DOUBLE PRECISION, UNIQUE(pattern_id, scope)
);
CREATE TABLE IF NOT EXISTS learning_evals (
  id TEXT PRIMARY KEY, learning_record_id TEXT NOT NULL, baseline_ref TEXT NOT NULL,
  candidate_ref TEXT NOT NULL, result_json TEXT NOT NULL, passed INTEGER NOT NULL,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS personal_events (
  id TEXT PRIMARY KEY, event_type TEXT NOT NULL, trace_id TEXT,
  session_id TEXT, task_id TEXT, workspace_id TEXT, payload_json TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1, occurred_at DOUBLE PRECISION NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS personal_outbox (
  event_id TEXT PRIMARY KEY, published_at DOUBLE PRECISION,
  publish_attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  next_attempt_at DOUBLE PRECISION NOT NULL
);

INSERT INTO execution_schema_meta(version, applied_at)
VALUES (3, EXTRACT(EPOCH FROM clock_timestamp()))
ON CONFLICT(version) DO NOTHING;

-- Additive mirror of the per-tenant audit hash chain (Postgres deployments).
-- The live DDL is applied idempotently from schema.POSTGRES_ADDITIVE_DDL;
-- this file is a reviewable artifact and never rewrites existing tables.

CREATE TABLE IF NOT EXISTS audit_chain (
    tenant TEXT NOT NULL,
    seq BIGINT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    object_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant, seq)
);

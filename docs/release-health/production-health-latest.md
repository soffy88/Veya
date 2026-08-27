# Production Health Baseline

Checked at `2026-08-27T06:24:24Z` against commit
`bdeb1a825167c70eac65284cc10b6934842b61f1` (`origin/main` matched).

## Result

**Production durable health: PASS**

The production backend was verified through the local `8767` gateway backed by
the running `veya-backend` container. The public MCP and Web routes were also
verified independently.

| Check | Result | Evidence |
|---|---:|---|
| Backend `/health` | PASS | HTTP 200, `{"status":"ok","version":"0.5.1"}` |
| MCP health | PASS | Internal/public HTTP 200; `tools_count=170` |
| Web | PASS | Internal/public HTTP 200 |
| Durable enabled | PASS | `enabled=true` |
| Durable authority | PASS | `backend=postgres`, `authority=postgresql` |
| Database | PASS | `db_connected=true` |
| Schema | PASS | `schema_version=3` |
| Queue/leases | PASS | `queue_depth=0`, `active_leases=0` |
| Outbox/quarantine | PASS | `pending_outbox=0`, `quarantined_count=0` |
| Reconciler | PASS | `reconciler=true`, last report has no pending decisions |
| Personal Runtime | PASS | `enabled=true`, PostgreSQL, `healthy=true` |
| Gold benchmark | PASS | `personal-agent-gold-v1`, approved `170`, gate `PASS` |

Durable counters observed during the probe: `jobs_enqueued=114`,
`jobs_claimed=64`, `lease_expired=16`, `lease_reclaimed=20`,
`fencing_rejected=4`, `duplicate_completion_suppressed=1`,
`outbox_replayed=72`, and `finalization_resumed=3`. Current pending and
quarantine counts were all zero.

Personal Runtime Gold was present in the response at `metrics.gold_benchmark`
with eval run `personal-gold-12beca1fe0594232afc251749f27f102`. The top-level
`gold_benchmark` field is not populated by the current health payload; no value
was invented or substituted.

## Public route note

The public hostname returns HTTP 500 for `/health` and
`/health/execution-runtime` because the SvelteKit edge does not expose the
backend root health routes. This is recorded as an edge route visibility note,
not as a durable-authority failure: the actual production backend gateway at
`127.0.0.1:8767` returned HTTP 200 with the complete durable fields, while the
public MCP health and Web root both returned HTTP 200.

No credentials, DSN, or secret values are included in this report.

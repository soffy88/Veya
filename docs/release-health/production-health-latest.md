# Production Health Baseline

Checked at `2026-08-27T06:52:54Z` against deployed web fix commit
`4bcf412f3b03e79b55cc92b4e8f6dfbb84139ce7`.

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
| Public `/health` | PASS | HTTP 200; real backend/durable/Personal probes |
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

## Public `/health`

The former HTTP 500 came from the SvelteKit default route because Caddy sends
the Veya hostname's non-API traffic to the Web service. The new
`apps/web/src/routes/health/+server.ts` route probes `/health`,
`/health/execution-runtime`, and `/health/personal-runtime` through the
configured `VEYA_GATEWAY` and returns a sanitized aggregate. It returns HTTP
200 only for a healthy aggregate and HTTP 503 with `status=degraded` when a
dependency cannot be reached; it does not hard-code green status.

Observed public response:

```json
{"status":"ok","web":"ok","gateway":"ok","backend":"ok","durable":"ok","personal_runtime":"ok","schema_version":3,"gold_gate":"PASS"}
```

The public MCP health and Web root also returned HTTP 200. The backend direct
health endpoint at `127.0.0.1:8767` remained HTTP 200.

No credentials, DSN, or secret values are included in this report.

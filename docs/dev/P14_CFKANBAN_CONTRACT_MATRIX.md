# P14 cfKanban contract matrix

Status: Wave 8 contract/placement only. No provider adapter, write path,
binding, event intake, or worker was implemented by this wave.

## Pinned source

| Item | Value |
|---|---|
| Repository | `breakstring/cfKanban` |
| Contract commit | `aff6b5ac21ccd3b5ce580795743157922518567b` |
| OpenAPI path | `contracts/openapi.json` |
| OpenAPI blob | `01ca57795e444b4400cb757c94f581b846eedd7b` |
| OpenAPI version | `3.1.0` |
| API version | `0.1.0` |
| API server | `/` (configured instance origin) |
| API spec | `docs/specs/2026-08-28-api-schema-spec.md` |
| Foundation spec | `docs/specs/2026-08-26-agent-native-kanban-foundation-spec.md` |

The matrix was derived from the pinned OpenAPI document and checked against
the two pinned specification documents. Floating `main` is not used.

## Instance discovery and authentication

| Veya operation | operationId | Method/path | Auth | Contract result |
|---|---|---|---|---|
| Discover instance | `discoverInstance` | `GET /.well-known/cfkanban-instance.json` | none | `InstanceDiscovery`; `Cache-Control: no-store`; `X-Request-ID` |
| Health | `getHealth` | `GET /healthz` | none | `Health`; `X-Request-ID` |
| Read API metadata | `getMeta` | `GET /api/v1/meta` | `BearerCredential` or `WebSession` | `Meta`; `X-Request-ID` |
| Read identity | `getMe` | `GET /api/v1/me` | `BearerCredential` or `WebSession` | `CurrentPrincipal`; `X-Request-ID` |

`InstanceDiscovery` requires `discovery_version=1`, `instance_id`,
`service_version`, `observed_origin`, `preferred_api_origin`,
`origin_version`, and `updated_at`. `Health` requires `service_version`,
`schema_version`, and `d1` (`reachable|unavailable`). Veya must validate these
before authenticated writes. The adapter must fail closed if the runtime
OpenAPI/meta/instance identity is incompatible with this pinned revision.

Agent/API authentication is:

```text
Authorization: Bearer <cfk_v1 opaque credential>
```

The credential is never placed in URLs, logs, Mission, prompts, artifacts, or
browser state. Cookie `WebSession` is a first-party web path and is not the
provider credential path for Veya.

## Read operations

| Veya operation | operationId | Method/path | Request/query contract | Response |
|---|---|---|---|---|
| List projects | `listProjects` | `GET /api/v1/workspaces/{workspace_id}/projects` | `workspace_id: Uuid`; `deleted=exclude|only`; `cursor`; `limit 1..100` (default 20) | `ProjectListResult` |
| Get project | `getProject` | `GET /api/v1/workspaces/{workspace_id}/projects/{project_id}` | UUID path values; `deleted=exclude|only` | `ProjectActiveRead` or `ProjectTombstoneRead` |
| List issues | `listIssues` | `GET /api/v1/issues` | `blocked=only|exclude`; `deleted`; project/workspace arrays max 20; status max 5; assignee max 20; `q`; cursor; limit 1..100 | `IssueListResult` |
| List project issues | `listProjectIssues` | `GET /api/v1/workspaces/{workspace_id}/projects/{project_id}/issues` | Same issue filters plus required workspace/project UUIDs | `IssueListResult` |
| Get issue | `getIssue` | `GET /api/v1/issues/{identifier}` | `identifier` must match `^CFK-[1-9][0-9]*$`; `deleted=exclude|only` | `IssueFullDetail` or `IssueTombstone` |
| List comments | `listComments` | `GET /api/v1/issues/{identifier}/comments` | `deleted`; cursor; limit 1..100 | `CommentListResult` |
| List events | `listEvents` | `GET /api/v1/events` | project/workspace arrays max 20; opaque `after`; limit 1..100 | `EventListResult` |

Issue reads expose the stable `identifier`, internal `id`, `number`, project
and workspace, title/body, status, priority, labels, assignee, blocked state,
and `version`. Issue body, comments, labels, attachments, and context are
untrusted input and do not grant Veya authority.

## Mutation operations

Every listed command write requires `Idempotency-Key` (1–128 printable ASCII
characters), `Authorization: Bearer`, and JSON body. `X-CSRF-Token` is only
required for the WebSession branch. Every write response includes
`event_cursor`, `idempotent_replay`, `resource`, and `X-Request-ID`.

| Veya operation | operationId | Method/path | Auth | Request schema | Response |
|---|---|---|---|---|---|
| Assign current Veya Principal | `assignIssueToMe` | `POST /api/v1/issues/{identifier}/commands/assign-to-me` | writer | `ExpectedVersionRequest` | `ActiveIssueWriteResult` |
| Assign explicit Principal | `updateIssue` | `PATCH /api/v1/issues/{identifier}` | writer | `UpdateIssueRequest` with `expected_version` and optional `assignee_principal_id` | `ActiveIssueWriteResult` |
| Non-terminal status transition | `updateIssue` | `PATCH /api/v1/issues/{identifier}` | writer | `UpdateIssueRequest` with `expected_version` and `status_key=backlog|todo|in_progress|canceled` | `ActiveIssueWriteResult` |
| Add comment | `createComment` | `POST /api/v1/issues/{identifier}/comments` | writer | `CreateCommentRequest` (`body` required, optional reply UUID) | `CommentWriteResult` |
| Report blocked | `reportIssueBlocked` | `POST /api/v1/issues/{identifier}/commands/report-blocked` | writer | `ReportBlockedRequest` (`expected_version`, `reason` 1–4096 chars) | `ActiveIssueWriteResult` |
| Clear blocked | `clearIssueBlocked` | `POST /api/v1/issues/{identifier}/commands/clear-blocked` | writer | `ExpectedVersionRequest` | `ActiveIssueWriteResult` |
| Complete issue | `completeIssue` | `POST /api/v1/issues/{identifier}/commands/complete` | writer | `CompleteIssueRequest` | `CompletedIssueWriteResult` |

`ExpectedVersionRequest` is exactly:

```json
{"expected_version": 3}
```

`Version` is an integer >= 1. `UpdateIssueRequest` requires
`expected_version`, has at least one additional property, and does not allow
`status_key=done`. The specification states that `done` is entered only by
`completeIssue`.

`CompleteIssueRequest` requires `expected_version`; `summary` is optional and
bounded to 8192 characters; `verification` and `follow_ups` are bounded string
arrays; `artifacts` is a bounded array of `{kind: url|path|commit|other,
value}`. Completion atomically creates the immutable completion comment,
changes the Issue to done, and emits an Event.

There is no separate `reopen` operation in the pinned OpenAPI contract. Veya
must not invent one. Reopen behavior is `IMPLEMENTATION=BLOCKED_BY_CONTRACT`
until cfKanban explicitly defines the supported operation and semantics.

## CAS, idempotency, and cursor rules

| Concern | Pinned contract |
|---|---|
| Idempotency header | `Idempotency-Key`, required for commands/non-natural writes; printable ASCII, 1–128 chars, no secret |
| CAS body | PATCH/command writes carry required JSON `expected_version` |
| Version conflict | HTTP 409, code `VERSION_CONFLICT`, `retryable=false`, recovery `refresh_resource`, details may include `current_version` |
| Write readback | Use returned `event_cursor`, `idempotent_replay`, and resource version; do not infer success from transport status alone |
| Event cursor | Opaque `after` query value from `EventListResult.next_cursor` or write `event_cursor`; never decode or synthesize |
| Page cursor | Opaque `cursor`/`next_cursor`, bound to normalized scope/filter; mismatch is `CURSOR_SCOPE_MISMATCH`, invalid/unknown is `INVALID_CURSOR` |
| Pagination | `{items,next_cursor,has_more,resolved_scope?}`; max page size 100 |
| Retry | Only retry when idempotency and operation/readback rules prove safety; never retry CAS, validation, or cursor conflicts as-is |

The API spec also defines `operation_id`, `operation_commits`, and unique
`(operation_id,event_index)` Event protection. A future adapter must preserve
the provider's returned operation/readback data in its operation ledger; it
must not treat a local timeout as provider failure without reconciliation.

## Error contract and Veya normalization

All Worker JSON errors use the exact envelope:

```json
{
  "code": "VERSION_CONFLICT",
  "category": "conflict",
  "source": "service",
  "message": "Issue version changed.",
  "request_id": "uuid",
  "retryable": false,
  "recovery": "refresh_resource",
  "details": {}
}
```

`X-Request-ID` equals the body `request_id`. Safe 429/503 responses may also
carry `Retry-After` and `retry_after_seconds`; the values must agree.

| HTTP/category/code from cfKanban | Veya normalized error | Recovery rule |
|---|---|---|
| 401 / `authentication` | `AUTH_ERROR` | Fail closed; credential boundary/re-authentication |
| 403 / `authorization` | `FORBIDDEN` | Do not retry; do not expand Project Grant/scope |
| 404 / `not_found` | `NOT_FOUND` | Re-read only if operation semantics permit; do not fabricate resource |
| 409 / `VERSION_CONFLICT` or other `conflict` | `VERSION_CONFLICT` or `INVALID_STATE` based on exact provider `code` | CAS conflict refetch/compare; state conflict requires supervisor decision |
| 409 / `PROJECT_*_LIMIT_REACHED` or `business_quota` | `QUOTA_EXCEEDED` | No blind retry; surface capacity/owner recovery |
| 429 / `RATE_LIMITED` | `RATE_LIMITED` | Respect `Retry-After`; bounded retry only |
| 503 / `PLATFORM_QUOTA_EXCEEDED` | `QUOTA_EXCEEDED` | Wait for explicit platform reset when retryable; preserve local Mission |
| 503 / `PLATFORM_UNAVAILABLE` or `platform_failure` | `PROVIDER_UNAVAILABLE` | Bounded provider retry/reconcile; never complete locally |
| 400 / `validation` | `INVALID_STATE` only when exact provider code is state-related; otherwise preserve validation detail | No guessed correction or retry |

The adapter must retain `provider_code`, provider `request_id` as
`provider_request_id`, and `recovery_hint` from the normalized envelope. For
Cloudflare-generated HTML/1027/edge failures without a provider envelope, it
must use the client-normalized source/category rules in the specification and
must not treat Cloudflare Ray ID as the service `request_id`.

## P14 placement decision

| P14 capability | Existing authority to reuse | Wave 8 decision |
|---|---|---|
| HTTP provider transport and provider errors | Existing 3O provider substrate | Extend only after this matrix; no adapter yet |
| Credentials | Existing secret substrate | `CFKANBAN_INSTANCE_ORIGIN` is configuration; credential remains secret-store-only |
| Mission | `veya.supervision.models.Mission` / `MissionStore` | Add binding metadata only; no second Mission runtime |
| Durable operation/cursor state | Existing canonical durable/event-outbox substrate | Add provider operation ledger through existing substrate; no second event bus |
| Issue ↔ Mission identity | New P14-only `IssueMissionBinding` | Business binding allowed; unique active `(instance_id, issue_id)` required |
| Execution/supervision | Existing GoalRun/Mission runtime | Provider only coordinates and observes; it never executes tools or routes supervision |

The future adapter must first perform, in order:

```text
GET /.well-known/cfkanban-instance.json
GET /openapi.json
GET /healthz
authenticated GET /api/v1/meta and /api/v1/me
```

It may issue authenticated writes only after instance identity, service/API
compatibility, health, credential identity, and authorized Project scope pass.
Runtime OpenAPI incompatibility produces:

```text
CFKANBAN_CONTRACT_COMPATIBILITY=BLOCKED
```

## Wave 8 qualification

```text
P14_WAVE8_CONTRACT_PIN=PASS
P14_CFKANBAN_CONTRACT_MATRIX=PASS
P14_PROVIDER_PLACEMENT=PASS

CFKANBAN_AUTH_CONTRACT=PASS
CFKANBAN_CAS_CONTRACT=PASS
CFKANBAN_IDEMPOTENCY_CONTRACT=PASS
CFKANBAN_EVENT_CURSOR_CONTRACT=PASS
CFKANBAN_ERROR_CONTRACT=PASS

NO_GUESSED_ENDPOINTS=PASS
NO_GUESSED_FIELDS=PASS
SECOND_RUNTIME_CREATED=NO
SECOND_REGISTRY_CREATED=NO
P14_PROVIDER_IMPLEMENTATION_STARTED=NO

COMMIT_NOW=NO
PUSHED=NO
```

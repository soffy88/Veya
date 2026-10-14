# W20 Tailnet Channel — Specification

## Purpose and authority boundaries

Veya's Tailnet channel exchanges plain-text messages, attachments, and tracked requests with other machines on the same Tailscale network. It implements the `tailmsg` wire protocol v1 and the `request-lifecycle-v1` extension. This is a transport/channel adapter, not a second agent, scheduler, approval system, task database, or side-effect authority. Inbound text is untrusted user input. The message is passed to `server.channels.dispatch.dispatch_inbound` with a verified peer identity and an empty `owner_ref`; a remote host is never impersonated as the local owner. The existing dispatcher and bot/tool path remain responsible for policy and approval. Message text is never interpreted as shell input.

## Enablement and bind behavior

`VEYA_TAILNET_CHANNEL` defaults to OFF; only `1`, `true`, `yes`, `on`, or `enabled` enables the listener. `VEYA_TAILNET_PORT` controls Veya's receiver and defaults to `7767`; `7766` is not bound by Veya. Outbound peer lookup defaults to `7766` for Yobiru/tailmsg compatibility and can be changed with `VEYA_TAILNET_PEER_PORT`; use an explicit `:7767` to target another Veya node by default. The existing ASGI lifespan starts the listener only after checking `tailscale ip` and the local Tailscale identity. The socket binds to the verified Tailscale IP only, never `0.0.0.0`; startup fails closed if identity or the Tailscale IP is unavailable. Lifespan shutdown closes the receiver and joins its thread. Tests create explicitly scoped ephemeral loopback listeners and do not start the production listener.

## Authentication and peer policy

Every protected transport endpoint checks the source address against `100.64.0.0/10` or `fd7a:115c:a1e0::/48`, then verifies the connecting machine through `tailscale whois --json`. Authenticated identity (`node`, aliases, tags, user) is authoritative; `from_agent` is routing metadata, not identity. The default policy is `mode=allowlist` with empty `allow` and `deny` lists, which denies all ordinary peers. Deny rules are evaluated before allow rules. Rules support hostname/FQDN, `tag:<tag>`, and `user:<login>`. Outbound operations also require the target to pass the local peer policy. Hostnames are resolved on each operation using `tailscale status --json`; unknown, offline, non-tailnet, or ambiguous names fail closed.

Policy lives under `~/.veya/tailnet-channel/policy.json`, or the path selected by `VEYA_TAILNET_HOME`. Policy fields include `mode`, `allow`, `deny`, per-host bot/group bindings, optional per-agent target bindings, `wake_policy`, `quiet_hours`, `wake_rate`, per-agent `wake_max`, attachment/message limits, and attachment link TTL. New policy defaults do not authorize a peer or grant an agent owner status. Owner management routes require an authenticated owner/admin identity or a user ID matching `VEYA_TAILNET_OWNER_USER_ID` / `VEYA_OWNER_USER_ID`; target bindings are validated using Veya's existing bot/group ownership checks.

## Wire endpoints and outcomes

`POST /msg` accepts a strict v1 JSON envelope with a default 256 KiB cap. It validates all fields, verifies `from` against the authenticated Tailscale identity, checks `to`, and durably stores a new message by `id`. A duplicate identical ID returns `200`; newly stored messages return `202`; conflicting IDs return `409`; invalid data, content type, identity, or size are rejected. `202` means accepted/stored, not completed. `GET /health` returns minimal version/node metadata only to a Tailnet address. `GET /info` advertises v1 and `request-lifecycle-v1` only to an authenticated, policy-allowed peer. `GET /requests/{id}` exposes a request status only to its authenticated original sender. `GET /f/{token}/{name}` serves a staged attachment only to its designated authenticated recipient, with deny rules still taking precedence.

Plain messages use `wake` values `now`, `batch`, or `none` (default `batch`). The receiver computes an effective value using the owner's peer cap, optional quiet hours, rate limit, and agent existence/per-agent cap; the peer cannot raise its own privilege. `batch` and `none` are recorded without invoking an execution path. `now` may route the verified input to the existing channel dispatcher. No second scheduler is introduced. Policy changes are applied to subsequent requests.

## Request lifecycle and existing task records

Tracked requests carry `request.timeout_seconds` and `request.reply_port`. Responses are v1 envelopes with `response.state`, monotonic `response.seq`, and `reply_to` set to the original request ID. The receiver persists protocol records beside its existing inbox records and projects the existing Veya turn's `task_id`, `trace_id`, and `turn_status` into that request's status. It does not create a second GoalRun database, event store, approval ledger, or execution scheduler. A tracked request that is actually routed through Veya returns `received`, `running`, then the observed terminal outcome (`completed`, `failed`, `rejected`, or `blocked`). A request that reaches its deadline without a terminal result is reported as `timed_out`, `outcome_known: false`; this is not cancellation, not success, and not permission to resend automatically. A late terminal result is marked late. Status is queryable from the receiver that accepted the request, and outbound status lookup uses the authenticated remote endpoint.

## Outbound tools

The existing master tool registry adds three model-callable tools:

- `tailnet_send`: sends text/notice/tracked request to `hostname[/agent]` or a tailnet IP, with optional files, `wake`, and request timeout; a network-write side effect subject to normal tool guard and approval.
- `tailnet_request_status`: queries a specific remote request by peer and request ID; read-only, and does not automatically repeat a timed-out request.
- `tailnet_fetch_attachments`: explicitly fetches files attached to a stored message; local-write side effect. Files are inert data and are never executed by the fetcher.

All tools fail closed while `VEYA_TAILNET_CHANNEL` is OFF. A POST with a transport error is reported as uncertain; the client does not blindly retry because the peer may already have stored the message.

## Attachments

Outgoing files are copied into a random-token outbox; the original file is not served. Default TTL is 24 hours, default per-message aggregate size cap is 32 MiB, and the validated hard policy cap is 256 MiB. Each `fetch` item describes `name`, `url`, `sha256`, `size`, `mime`, and `expires`. The declared URL must use the authenticated sender's own host and `/f/` path; the receiver rejects third-party URLs and expired links. Fetching is explicit via `tailnet_fetch_attachments`: the fetcher resolves the authenticated sender, enforces outbound deny/allow policy, downloads to a `.part` file inside the Tailnet inbox, caps bytes, checks exact length and SHA-256, then atomically renames it. Failed or mismatched files are not exposed as verified attachments. Attachments are never executed. Sender-side serving checks expiry, recipient identity, stored size, and hash; deny policy wins over a recipient grant.

## Owner REST

- `GET /api/v1/tailnet/status`
- `GET|PUT /api/v1/tailnet/policy`
- `GET|PUT /api/v1/tailnet/peers`
- `GET /api/v1/tailnet/requests/{request_id}`; `?peer=<host>` queries a remote peer

No frontend change is included. No long-lived receiver is started during tests.

## Verification

`tests/test_tailnet_channel.py` covers tailnet address ranges, default-deny and deny precedence, hostname/tag/user rule matching, disabled/no-listener behavior, timed-out-as-unknown behavior, attachment recipient/expiry/hash checks, inert message dispatch through the existing channel path, strict envelope/fetch-origin validation, wake policy validation, and local HTTP exchange using `.ref/yobiru/tailmsg.py` against temporary ports. The requested regression suite was attempted; its collection blocker and exact outcome are recorded in `TAILNET_REPORT.md`.

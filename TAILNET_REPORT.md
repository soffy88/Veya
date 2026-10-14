# W20 Tailnet Channel — Implementation Report

**Date:** 2026-10-14
**Branch:** `feat/tailnet-channel`
**Worktree:** `/data/soffy/projects/veya-tnc`

## Result

Implemented the opt-in Veya Tailnet channel and recorded the protocol/design contract in `TAILNET_SPEC.md`. The implementation is fail-closed by default. Focused Tailnet tests pass. The requested broad regression suite was attempted but is **blocked during test collection** because this worktree's `platform/3O` gitlinks are not mounted; no submodule or other worktree was modified to work around that environmental blocker.

## Delivered changes

- Added `server/tailnet_channel.py`. `VEYA_TAILNET_CHANNEL` defaults to OFF. When enabled by configuration, the receiver binds only to a verified Tailscale address on `VEYA_TAILNET_PORT` (default `7767`) and fails closed if the node identity or interface is unavailable. `7766` is not used as Veya's listener default. Outbound peer lookup defaults to `7766` for Yobiru/tailmsg compatibility; `VEYA_TAILNET_PEER_PORT` overrides it, and another Veya receiver can be addressed explicitly with `:7767`.
- Implemented tailmsg v1 envelope validation, `/health`, `/info`, `/msg`, `/requests/{id}`, and recipient-scoped `/f/{token}/{name}`. The implementation validates tailnet IPv4/IPv6 ranges, verifies peers through `tailscale whois --json`, checks claimed `from` against that identity, and validates message IDs, unknown fields, agent names, response metadata, fetch origins, sizes, hashes, and expiries. Identical messages are idempotent; conflicting reuse of an ID is rejected.
- Default peer policy is an empty allowlist (deny all); deny rules take precedence. Hostname/FQDN, `tag:`, and `user:` rules are supported. Outbound sends/status queries are subject to the local peer policy. Wake policy supports per-peer caps, quiet hours, rate limits, and per-agent caps; a peer cannot promote its requested wake level. `batch`/`none` are stored without starting a task path.
- Inbound messages use the existing `server.channels.dispatch.dispatch_inbound` / bot-turn path, with remote peers represented as non-owner (`owner_ref` is empty). The message body is treated as plain text and is never executed as a shell command. `server/channels/dispatch.py` now projects the existing bot turn's task ID, trace ID, and status so request receipts can reference existing execution evidence instead of inventing a second task runner.
- Added tracked request receipt/result envelopes with `request-lifecycle-v1`, sequence numbers, queryable status, and explicit `timed_out`/`outcome_known: false` semantics. A timeout is not treated as cancellation or success, and uncertain POST delivery is not blindly retried. Lifecycle notifications are best-effort on the current connection; delivery retry across peer outages is not implemented as a separate scheduler/outbox.
- Added attachment staging and explicit `tailnet_fetch_attachments`. Files are copied into a random-token outbox; the default link TTL is 24 hours, default aggregate/file limit is 32 MiB, and the validated policy maximum is 256 MiB. Fetching enforces the authenticated sender/recipient, local outbound policy, declared size, expiry, and SHA-256, writes only beneath the Tailnet inbox, and never executes content.
- Added owner-only REST endpoints at `/api/v1/tailnet/status`, `/policy`, `/peers`, and `/requests/{request_id}`. Policy changes and target bindings require an authenticated owner/admin identity or a user ID configured with `VEYA_TAILNET_OWNER_USER_ID` / `VEYA_OWNER_USER_ID`; bindings re-use existing bot/group ownership checks.
- Registered three model-callable tools in the existing MasterToolRegistry: `tailnet_send` (network write, governed by normal policy), `tailnet_request_status` (read-only), and `tailnet_fetch_attachments` (local write, explicit download). They fail closed while `VEYA_TAILNET_CHANNEL` is OFF.
- Added an append-only Yobiru MIT attribution entry to `THIRD_PARTY_NOTICES.md`. `.ref/yobiru` is an ignored, local-only reference checkout; upstream implementation source was not vendored into Veya.

## Verification evidence

| Check | Result |
|---|---|
| `.venv/bin/python -m py_compile server/tailnet_channel.py server/routes/tailnet.py server/channels/dispatch.py server/tool_registry.py server/app.py tests/test_tailnet_channel.py` | PASS |
| `.venv/bin/ruff check server/tailnet_channel.py server/routes/tailnet.py tests/test_tailnet_channel.py` | PASS |
| `.venv/bin/python -m pytest -q tests/test_tailnet_channel.py` | PASS — 15 passed |
| `git diff --check` | PASS |
| Required broad regression command from `TAILNET_BRIEF.md` | BLOCKED — collection failed with 30 errors rooted in `RuntimeError: 3O main library 'obase' is not mounted`; this worktree's `platform/3O/obase` gitlink is uninitialized. The three-environment-failure allowance for `test_canonical_proxies` does not cover this collection blocker. |
| Live network test against a real Tailscale peer | NOT RUN — no long-lived listener was started and no service was restarted. The interoperability test uses `tailmsg.py` with temporary local test ports. |
| Frontend check | NOT APPLICABLE — no frontend was changed. |

Focused tests cover address ranges, default-deny and deny precedence, hostname/tag/user matching, peer-port defaults, feature-off/no-listener behavior, timeout-as-unknown, attachment scope/expiry/hash validation, non-owner text dispatch, strict envelope/fetch-origin validation, wake policy, and HTTP interoperability using the local Yobiru reference client with temporary ports.

## Configuration and route/tool inventory

- `VEYA_TAILNET_CHANNEL`: default OFF.
- `VEYA_TAILNET_PORT`: default `7767` (Veya inbound listener).
- `VEYA_TAILNET_PEER_PORT`: default `7766` (outbound peer default for tailmsg/Yobiru).
- `VEYA_TAILNET_HOME`: optional Tailnet data-home override; default `~/.veya/tailnet-channel`.
- `VEYA_TAILNET_OWNER_USER_ID` / `VEYA_OWNER_USER_ID`: owner identity accepted by the management API.
- REST routes: `/api/v1/tailnet/status`, `/api/v1/tailnet/policy` (GET/PUT), `/api/v1/tailnet/peers` (GET/PUT), `/api/v1/tailnet/requests/{request_id}` (GET; optional `peer` parameter for a remote query).
- Model tools: `tailnet_send`, `tailnet_request_status`, `tailnet_fetch_attachments`.

## Remaining limitations

1. The broad regression suite still needs rerunning in a worktree/environment where the pinned `platform/3O` submodules are mounted. No changes were made to those protected submodules.
2. No live two-machine Tailscale test was run; transport interoperability is verified only against the reference client's protocol behavior on temporary local ports.
3. Batch/none messages remain durably recorded for later handling through Veya's existing paths; this change does not add a second wake scheduler. Lifecycle response delivery is best-effort if the peer is offline at the time of the response.
4. No UI or production deployment was performed. The listener remains OFF unless explicitly enabled in the runtime environment.

## Safety and repository handling

No push, service restart, production configuration change, or long-lived test listener was performed. Only the requested worktree was used for code edits. The existing `.venv`, `TAILNET_BRIEF.md`, and the unrelated `.third_party_notices.base.tmp` remain un-staged and untouched by the commit.

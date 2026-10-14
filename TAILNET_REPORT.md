# W20 Tailnet Channel — FIX1 Report

**Status: FIX1 done**
**Date:** 2026-10-14
**Branch:** `feat/tailnet-channel`
**Worktree:** `/data/soffy/projects/veya-tnc`

## Fix1 result

Merged local `main` into this worktree and resolved both content conflicts. `THIRD_PARTY_NOTICES.md` retains the pre-existing notices, adds the main-branch Chinese novelist and FastClaw entries, and preserves the W20 Yobiru/tailmsg attribution. Merge markers were removed and checked.

`server/channels/dispatch.py` is based on the merged `main` version. FastClaw's owner/non-owner identification, IM-turn context, non-owner write-root sandbox, and `finally` cleanup are preserved. Tailnet messages are explicitly classified as non-owner even if a chat binding contains owner-like fields, so their turns remain subject to the non-owner sandbox. Only the Tailnet-required projection of the existing bot turn's `task_id`, `trace_id`, and `turn_status` was restored in `DispatchOutcome`, its serialized result, and `_run_bot`; no second scheduler or execution authority was introduced.

The main-branch merge changes and the existing Tailnet implementation/spec/tests are retained in this worktree. The report and merge are committed on this branch; no push was performed.

## Tailnet implementation inventory

- `server/tailnet_channel.py` implements the opt-in Tailnet channel. `VEYA_TAILNET_CHANNEL` defaults to OFF. The receiver binds only to a verified Tailscale address and defaults to `VEYA_TAILNET_PORT=7767`; the default outbound peer port is `7766`, configurable with `VEYA_TAILNET_PEER_PORT`.
- The tailmsg v1 envelope and request-lifecycle-v1 endpoints include `/health`, `/info`, `/msg`, `/requests/{id}`, and recipient-scoped `/f/{token}/{name}`. Peer identity is checked via `tailscale whois`; claimed sender identity is checked against the authenticated peer. Invalid envelopes, conflicting message-ID reuse, invalid fetch origins, and invalid response/attachment metadata fail closed.
- Peer policy defaults to an empty allowlist (deny all), with deny rules taking precedence. Hostname/FQDN, `tag:`, and `user:` rules are supported. Outbound send/status operations also apply local peer policy. The requester cannot promote its own wake level; policy caps, quiet hours, and rate limits are applied.
- Inbound plain-text messages enter the existing `server.channels.dispatch.dispatch_inbound` and bot/group turn path. Message text is data, not a shell command. Existing turn IDs/statuses are reused for receipts.
- Request status/results use the existing Tailnet storage and request lifecycle. Timeout means the result is unknown (`outcome_known: false`), not cancellation or success; uncertain delivery is not blindly retried. Lifecycle notifications are best-effort if the peer is offline; no separate retry scheduler/outbox is introduced.
- Attachments are staged in the Tailnet outbox, with recipient-scoped, expiring links (default TTL 24 hours), SHA-256 checks, and size limits (32 MiB default; policy maximum 256 MiB). Fetching verifies recipient/sender, expiry, size, hash, and local policy; saved content is not executed.
- Owner-only REST routes: `/api/v1/tailnet/status`, `/api/v1/tailnet/policy` (GET/PUT), `/api/v1/tailnet/peers` (GET/PUT), and `/api/v1/tailnet/requests/{request_id}` (GET, optional remote peer query). Management requires an authenticated owner/admin identity or `VEYA_TAILNET_OWNER_USER_ID` / `VEYA_OWNER_USER_ID`.
- Model-callable tools registered in the existing tool registry: `tailnet_send` (network write under normal policy), `tailnet_request_status` (read-only), and `tailnet_fetch_attachments` (explicit local download). All fail closed while the feature flag is OFF.
- `TAILNET_SPEC.md` defines the protocol/design contract. `.ref/yobiru` is a local-only excluded reference checkout; the protocol interoperability test uses its `tailmsg.py` client against temporary test listeners. Upstream MIT attribution is recorded in `THIRD_PARTY_NOTICES.md`; implementation files are not vendored copies.

## Verification

| Check | Result |
|---|---|
| `.venv/bin/python -m py_compile server/channels/dispatch.py server/tailnet_channel.py server/routes/tailnet.py server/tool_registry.py server/app.py tests/test_tailnet_channel.py` | PASS |
| `.venv/bin/ruff check server/channels/dispatch.py server/tailnet_channel.py server/routes/tailnet.py tests/test_tailnet_channel.py` | PASS — all checks passed |
| `.venv/bin/python -m pytest -q tests/test_tailnet_channel.py` | PASS — 15 passed |
| `git diff --cached --check` and `git diff --check` | PASS after resolving merge whitespace and marker issues |
| Conflict-marker scan of `THIRD_PARTY_NOTICES.md` and `server/channels/dispatch.py` | PASS — no conflict markers; no unresolved paths |
| `tests/test_fastclaw_borrow.py` / `tests/test_skill_chinese_novelist.py` | BLOCKED during collection/setup by missing `platform/3O/obase`; a combined run showed the 15 Tailnet tests pass and 6 novelist test setup errors from the same environment issue |
| `tests/channel/test_channel_inbox_notification.py`, `tests/goal_run/test_capability_channels.py`, `tests/test_nano_w7_channels.py` | BLOCKED during collection where channel imports reach the same missing `platform/3O/obase` library |
| Broad regression command specified in `TAILNET_BRIEF.md` | BLOCKED — exit 2 during collection, 32 errors rooted in `RuntimeError: 3O main library 'obase' is not mounted`. This is not covered by the brief's allowance for three known `test_canonical_proxies` environment failures |
| Live two-machine Tailscale test | NOT RUN; no long-lived listener was started and no service was restarted |
| Frontend check | NOT APPLICABLE; no frontend changes were made for this fix |

The `platform/3O` submodules were not initialized or modified to work around the test-collection blocker, in accordance with the protected-worktree rules.

## Environment and remaining limitations

- `VEYA_TAILNET_CHANNEL`: default OFF.
- `VEYA_TAILNET_PORT`: default `7767` for Veya inbound listening; `7766` is not used as the listener default.
- `VEYA_TAILNET_PEER_PORT`: default `7766` for Tailmsg/Yobiru peer compatibility; can be overridden.
- `VEYA_TAILNET_HOME`: optional data-home override; defaults to `~/.veya/tailnet-channel`.
- `VEYA_TAILNET_OWNER_USER_ID` / `VEYA_OWNER_USER_ID`: owner identity configuration for the management API.
- No UI change, production deployment, or live cross-machine test was performed. Listener remains off unless explicitly enabled by runtime configuration. Batch/none messages do not create a second wake scheduler, and lifecycle delivery is best-effort when a peer is offline.
- Remaining verification work is to rerun the main-branch regression tests in an environment where the worktree's pinned `platform/3O` libraries are already available.

## Safety and repository handling

All edits and commands were confined to `/data/soffy/projects/veya-tnc`. No push, service restart, production Caddy/systemd/docker change, long-lived listener, or `rm -rf` was used. Protected `.venv` and `platform/3O` were not modified. As part of the explicitly requested `main` merge, `veya/remote/policy_resolver.py` receives main's 12-line FastClaw policy hook that invokes the existing IM non-owner sandbox enforcement; no other `veya/remote/*` file was changed. Existing untracked `.third_party_notices.base.tmp`, `.venv`, `TAILNET_BRIEF.md`, and `TAILNET_FIX.md` were left untracked and not included in the commit.

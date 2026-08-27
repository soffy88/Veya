# Grok Bot 0.18 Reconstructed — Read-only Audit

- Repository: <https://github.com/b-nnett/grok-bot-0.18-reconstructed>
- Audited commit: `a9f633e09d49a85829b8236331b9e21f7e612634`
- Audited: `2026-08-27`
- Scope: provider router, local provider authentication, MCP/tool routing,
  local Docker sandbox, desktop settings/secrets, deterministic packaging,
  usage tracking, and release artifact verification.
- Change policy: this audit made no implementation change to Veya.

The repository describes itself as an unofficial source-oriented reconstruction,
not the original vendor source or an official release. Its own README documents
the hybrid boundary: readable runtime sources are compiled, while a checksum-
pinned shipped renderer is retained as a build input.

## Executive assessment

| Mechanism | Status | Evidence | Assessment |
|---|---|---|---|
| Provider router | HAVE | `source/shared/inference-router.ts:1-24`; `source/node-agent-coordinator/inference-router.ts:52-183` | Explicit provider selection for Cursor, Claude Code, Codex, and OpenRouter; per-agent serialized local turns and atomic local transcript persistence. No durable cross-process routing or automatic provider failover. |
| Local authenticated provider bridge | HAVE | `source/host/extensions/inference/provider-session.ts:67-131,167-224,247-283`; `source/shared/node/inference-router-local.ts:18-52` | Codex ChatGPT OAuth credentials are validated and refreshed; Claude Code uses its local executable/login; OpenRouter uses env or persisted secret. Provider transports are explicit. |
| MCP/tool routing across providers | HAVE | `source/node-agent-coordinator/routed-mcp-bridge.ts:39-87`; `source/host/extensions/inference/provider-session.ts:153-186,230-254` | A loopback, per-request-secret JSON-RPC MCP bridge lists and calls connected tools for Claude; Codex/OpenRouter receive tool definitions and call IDs. The read-only/idempotent annotations are heuristic and must not be treated as a security authority. |
| Local Docker sandbox lifecycle | HAVE | `source/electron-main/box/local-docker-host-connector.ts:13-17,64-111,120-215,218-266` | Owned container label, image/schema/runtime hashes, loopback ports, readiness check, content-addressed runtime staging, restart and force-recreate paths. It is macOS/desktop-oriented and mounts local auth directories read-only. |
| Desktop settings/secrets bridge | HAVE | `source/electron-main/secrets/secret-store.ts:115-214,245-303`; `source/electron-main/secrets/user-secrets-store.ts:66-190`; `source/electron-main/secrets/secrets-ipc.ts:112-168` | Electron safeStorage, account-scoped encrypted records, atomic writes, IPC sender guards, and explicit persistence status. When OS encryption is unavailable it falls back to session memory, which is transparent but not durable. |
| Deterministic packaging/verification | HAVE | `scripts/lib/asar-integrity.mjs:22-88,91-154`; `scripts/verify.mjs:87-150,154-264`; reconstruction manifests | ASAR/file inventories, SHA-256 checks, source-composition/provenance checks, native dependency checks, bundle identity, and code-signature verification are implemented. Full package execution is macOS-only. |
| Usage tracking | PARTIAL | `source/shared/inference-router.ts:4-24`; `source/host/extensions/inference/provider-session.ts:31-32,278`; `source/shared/node/settings/sand-settings-store.ts:161-166` | Per-provider request/token/cache counters and last-use timestamps are persisted locally. README explicitly says these are activity records, not authoritative billing; there is no durable cross-device/tenant ledger. |
| Release artifact verification | PARTIAL | `scripts/verify-publication-tree.mjs:10-39`; `scripts/package-macos.mjs:22-77`; `scripts/verify.mjs:147-264` | Publication-tree equality and packaged artifact verification are strong. Release attestation, CI availability, and macOS package verification require the macOS build environment and were not executable on this Linux host. |

## Provider router and local authentication

The router has a narrow explicit provider enum and a local coordinator router.
Non-Cursor turns are queued per agent, their transcript is persisted through a
temporary file plus rename, and the selected provider receives the same
conversation. Codex is not invoked as a CLI: the implementation reads a
private `~/.codex/auth.json`, validates the ChatGPT token set and file mode,
refreshes expired OAuth credentials, and calls the direct Responses endpoint.
Claude Code is invoked through its resolved executable with `persistSession:
false`; OpenRouter uses the OpenAI-compatible SDK and a stored/env API key.

This is a useful adapter pattern, but it is not a durable provider control
plane. Provider choice is a desktop setting, transcript storage is local JSON,
and there is no provider health state or automatic failover policy in the
router itself. Veya should keep such concerns as capabilities under its single
MasterAgent path and its durable runtime authority.

## MCP and tool routing

The loopback bridge creates a random URL secret, binds only to `127.0.0.1`,
limits request bodies, validates the discovered tool shape, and translates
JSON-RPC `tools/list` and `tools/call` into the host callback. Claude receives
the bridge as a strict MCP server; Codex and OpenRouter receive schema-backed
tool definitions and return tool calls through the same host callback.

The main weakness is the local `isReadOnly()` classifier, which derives
`readOnlyHint`, `idempotentHint`, and `destructiveHint` from names and
descriptions. That is acceptable as a UI hint but unsafe as authorization,
side-effect, or retry policy. Veya's existing explicit capability and
side-effect declarations are the correct authority.

## Local Docker sandbox

The lifecycle is unusually disciplined for a desktop-local connector:

1. It checks Docker availability and refuses unowned or unexpected-image
   containers.
2. It stages host and daemon bundles under a content-addressed directory and
   labels the container with schema and bundle hashes.
3. It uses loopback-only published ports, named volumes, read-only runtime
   mounts, a random gateway token, and a readiness probe.
4. It replaces a container when schema/runtime fingerprints change and has
   explicit stop/restart/force-recreate operations.

The remaining risks are product-boundary risks: the image is an external
mutable tag, local authentication directories are mounted into the container,
and force recreation returns `started-untrackable` rather than a durable
operation result. These should not be copied into Veya's PostgreSQL-backed
Execution Runtime without an artifact/side-effect record.

## Settings and secrets

The implementation separates settings JSON from encrypted secret stores,
supports account-scoped records, uses atomic replacement, and guards Electron
IPC senders. It also exposes whether storage is persistent. The fallback to
in-memory secrets is explicit and emits a warning, but it means restart loses
credentials; it is not equivalent to secure persistence. Linux `basic_text`
selection is an explicit platform-specific tradeoff and should remain a
visible policy decision.

## Packaging and release evidence

The deterministic packaging boundary is the strongest part of the project.
The ASAR packer snapshots staged files and SHA-256 values before packing,
checks archive and unpacked entries after packing, and restores previous output
on failure. The verifier checks required entries, source markers, renderer
provenance, clean-source closure, immutable artifact fallback hashes, native
dependencies, bundle identity, URL registration, and deep code signing.
The publication check also exports HEAD and compares the resulting Git tree.

On the audit host, the reproducible checks that do not require macOS passed:

- `npm test`: **18 pass, 0 fail**;
- `npm run typecheck`: **exit 0**;
- `npm run source:typecheck`: **exit 0**;
- `npm run publication:check`: **2111 files preserved**, tree
  `b68f24972427952c4934e4364736fec62661044f`.

The repository requires Node `>=26.5.0 <27`; the audit host used Node
`26.4.0`, producing an engine warning. `npm run package` and the final
`npm run verify` path were not claimed as executed because they require a
macOS application bundle, macOS signing tools, and the pinned runtime input.

## Do-not-copy list for Veya

1. Do not add a provider-specific coordinator or a second semantic authority;
   the provider bridge must remain a MasterAgent capability.
2. Do not make local JSON transcript/settings or an in-process per-agent queue
   the durable execution authority; Veya must retain PostgreSQL, leases,
   fencing, checkpoints, and outbox semantics.
3. Do not use name/description heuristics as permission, idempotency, retry, or
   destructive-operation authority.
4. Do not mount broad host credential directories into a sandbox; use scoped
   credential issuance and capability-specific access.
5. Do not treat in-memory or weak platform storage as successful persistence;
   expose the degraded state and fail closed for safety-sensitive operations.
6. Do not make force-recreate or other side-effecting lifecycle operations
   `untrackable`; record intent, outcome, and recovery state.
7. Do not call local usage counters billing, and do not treat an ad-hoc code
   signature as a complete release attestation.

## Five possible narrow internalizations

These are compatibility ideas, not implementation work in this audit:

1. A provider-neutral credential/health adapter that reports authenticated,
   unavailable, expired, and degraded states without changing the MasterAgent
   path.
2. A provider-neutral MCP bridge contract using Veya `ToolSpec` side-effect,
   capability, request-id, and redaction metadata instead of heuristics.
3. Sandbox ownership and runtime fingerprint checks mapped onto the existing
   Veya sandbox plus Execution Runtime artifact/lease records.
4. A reusable deterministic artifact verifier that cross-checks immutable
   content hashes, manifests, provenance, and published outputs.
5. A privacy-safe provider usage event adapter that feeds Veya's existing
   telemetry/durable metrics while explicitly separating activity from billing.

## Recommendation

**NO — do not implement the external architecture wholesale now.** Veya's
single MasterAgent and PostgreSQL Execution Runtime already provide the more
important orchestration, durability, side-effect, and audit boundaries. The
Grok Bot reconstruction is valuable as a source of narrow desktop adapter
patterns, especially MCP bridging, sandbox ownership, and artifact
verification. Revisit only when a concrete Veya desktop/provider requirement
needs one of those contracts; preserve the current release freeze and avoid a
second orchestration system.

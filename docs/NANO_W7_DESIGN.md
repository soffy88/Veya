# NANO-W7 Design: Chat-app Channels + Morning Feed

> Superseded in part by `NANO_W7_REPORT.md`, which records what was actually
> built. This document is kept as the design rationale; where the two differ,
> the report is authoritative.

## Overview

Cross-platform chat adapters (Telegram, Feishu/Lark, DingTalk, WeCom) and a
per-user morning feed ("daily brief") driven by the existing routine clock (P4).
Both features are behind env flags `VEYA_CHANNELS` and `VEYA_MORNING_FEED`,
both default OFF (the whole HTTP surface answers 404 while off).

## 3O Layer Mapping

| Element | Layer | Why |
|---------|-------|-----|
| `verify_*_signature` | `veya/obase/channels/__init__.py` | Crypto oprims. Canonical single source (§1.4); the platform clients delegate to them. |
| `chunk_text` | `veya/obase/channels/__init__.py` | One atomic string split, reused by every formatter and by reply delivery. |
| `TelegramClient` / `FeishuClient` / `DingTalkClient` / `WeComClient` | `veya/obase/channels/<platform>.py` | Platform HTTP/crypto clients = external-world adapters (§7). obase never imports a 3O layer. |
| `format_<platform>_message`, `escape_*`, `format_task_*` | `veya/oskill/im/<platform>.py` | Pure formatting: ≥2 oprims composed, stateless, no IO, no framework import, no user_id. |
| `chunk_text_for_platform`, reply chunking | pure `oskill` via `chunk_text` | Long-reply splitting is a pure algorithm, not a service. |
| `daily_brief` | `veya/omodul/daily_brief.py` | Business transaction (render + persist the brief). Standard `(config, input_data, output_dir) -> dict`, declares `_enabled_pillars = {"report", "cost"}`, never raises, re-raises `CancelledError`. Composes `veya.oprim.event.emit_event` + the oskill formatters. |
| `ChannelAdapter` + 4 adapters | `server/channels/<platform>.py` | Transport adapters: verify signature, normalise payload, post chunked replies. Framework-free so they unit-test without a TestClient. |
| `ChannelBindingStore` | `server/channels/bindings.py` | Per-channel binding table `(platform, external_chat_id) -> target + owner_ref`. Service-layer persistence. |
| `CodeStore` / `issue_code` | `server/channels/linking.py` | One-time, expiring, single-use linking codes. Memory-only (bearer credentials). |
| `dispatch_inbound` | `server/channels/dispatch.py` | Routes into the existing `run_bot_turn` / `run_group_turn` paths. Adds no execution authority. |
| `ApprovalChannel` | `server/channels/approvals.py` | Spec item 2 hook only. Renders an already-decided approval; never decides one. |
| webhooks, binding CRUD, feed config | `server/routes/channels.py` | HTTP/auth (§8). Identity only from the bearer token, pseudonymised before storage. |
| `run_morning_feed`, `build_routine_dispatch` | `server/bot_team/morning_feed.py` | Delivery (Web Push P5 + channels), per-user config persistence, and the clock dispatch seam. |
| `assemble_morning_feed` | `server/assembly.py` | ServiceManifest assembly over the existing `sequential_composer` skeleton (§8.7). No new engine. |
| `/settings/channels` | `apps/web/src/routes/settings/channels/` | Project UI (§8). Degrades to a "not enabled" notice when the flag is off. |

## Data Flow

### Inbound message

```
External chat (Telegram/Feishu/DingTalk/WeCom)
  -> server/routes/channels.py        verify signature (fails closed)
  -> server/channels/<platform>.py    parse -> ChannelMessage
  -> ChannelBindingStore              (platform, external_chat_id) -> binding
  -> run_bot_turn / run_group_turn    existing P1/P2 path
  -> adapter.send_reply               chunked, via the obase client
```

### Linking

```
Settings UI -> POST /bind/code -> one-time code (memory, 5 min)
  -> user pastes the code into the external chat
  -> webhook consumes the code, binds that chat, and never executes it
```

### Morning feed

```
Routine clock (existing P4) --dispatch_fn seam--> build_routine_dispatch
  -> server/bot_team/morning_feed.run_morning_feed
      -> gather sections (routines, goals, memory, ...)
      -> daily_brief omodul -> report artifact
      -> Web Push (P5) + channel adapters
```

## Frozen Decisions Respected

- No second scheduler: the feed plugs into the clock's existing `dispatch_fn`.
- No second approval system: `ApprovalChannel` only renders decisions already
  made by SideEffectLedger + ActionGateway + GoalRun state.
- No new execution authority: dispatch reuses `run_bot_turn` / `run_group_turn`.
- Credentials from env/secret store only; never persisted, never in the repo.
- User ids are pseudonymised before they reach any 3O element (§5.5.1).
# NANO-W7 Design: Chat-app Channels + Morning Feed

## Overview
Implement cross-platform chat adapters (Telegram, Feishu/Lark, DingTalk, WeCom) and a per-user morning feed ("daily brief") driven by the existing routine clock (P4). All new features behind env flags `VEYA_CHANNELS` and `VEYA_MORNING_FEED` (default OFF).

## 3O Layer Mapping

| Element | Layer | Why |
|---------|-------|-----|
| `telegram_client` | `veya/obase/channels/telegram.py` (oprim) | Single HTTP client for Telegram Bot API; atomic external call; no business logic |
| `feishu_client` | `veya/obase/channels/feishu.py` (oprim) | Single HTTP client for Feishu Bot API; atomic external call |
| `dingtalk_client` | `veya/obase/channels/dingtalk.py` (oprim) | Single HTTP client for DingTalk API; atomic external call |
| `wecom_client` | `veya/obase/channels/wecom.py` (oprim) | Single HTTP client for WeCom API; atomic external call |
| `channels_common` | `veya/obase/channels/__init__.py` (oprim) | Shared types, signature verification utilities, chunking helpers |
| `format_telegram_message` | `veya/oskill/im/telegram.py` (oskill) | Pure formatting: ≥2 oprims (chunk + escape); stateless; no IO |
| `format_feishu_message` | `veya/oskill/im/feishu.py` (oskill) | Pure formatting: ≥2 oprims; stateless; no IO |
| `format_dingtalk_message` | `veya/oskill/im/dingtalk.py` (oskill) | Pure formatting: ≥2 oprims; stateless; no IO |
| `format_wecom_message` | `veya/oskill/im/wecom.py` (oskill) | Pure formatting: ≥2 oprims; stateless; no IO |
| `daily_brief` | `veya/omodul/daily_brief.py` (omodul) | Business transaction: assembles feed sections (routines, goals, GoalRuns, memory, RSS); declares `report` + `cost` pillars; driven by routine clock |
| `channels_binding_table` | `server/channels/binding.py` (Layer 4) | Per-channel binding: external_chat_id → bot/group + owner_user; one-time code linking flow |
| `channels_router` | `server/routes/channels.py` (Layer 4) | Inbound webhook endpoints per platform; signature verification; dispatch to bot/group |
| `morning_feed_scheduler` | `server/bot_team/routine_clock.py` extension (Layer 4) | Adds "daily brief" routine template to existing clock; no new scheduler |

## Data Flow

### Inbound Message
```
External Chat (Telegram/Feishu/DingTalk/WeCom)
    → server/routes/channels.py (webhook, verify signature)
    → map external chat_id → Veya user + bot/group (binding table)
    → pseudo-anonymize user_id (obase.pseudo_anonymizer)
    → dispatch to existing bot/group chat path (P1/P2)
    → reply posted back via obase.channels client
```

### Morning Feed
```
Routine Clock (existing P4)
    → daily_brief routine template fires at user's configured time/timezone
    → daily_brief omodul assembles:
        - today's routines/goals status
        - recent GoalRun results
        - bot memory highlights
        - optional RSS/URL list
    → output stored as artifact
    → Web Push (P5) + optional channel delivery (via obase.channels)
```

## Feature Flags
- `VEYA_CHANNELS` (default: `false`) — enables channel adapters & binding UI
- `VEYA_MORNING_FEED` (default: `false`) — enables morning feed routine & config UI

## Credentials
All credentials from env/secret store only. Never in repo. Per-channel binding table stores `external_chat_id → {bot_id|group_id, owner_user_id}`.

## Single-Source Guard Tests
For each platform, a test `test_*_single_source` asserts that the obase client + oskill formatter produce identical output to the legacy oskill.im implementation (before migration).

## 3O Compliance Checklist
- [ ] obase.channels: no imports from 3O layers (oprim/oskill/omodul/oservi)
- [ ] oskill.im formatters: ≥2 oprims, stateless, no IO, LLM via injected LLMCaller
- [ ] daily_brief omodul: signature (config, input_data, output_dir) → dict with status/error; declares `_enabled_pillars` = {"report", "cost"}; no omodul→omodul calls
- [ ] No second scheduler/task store; uses existing routine_clock
- [ ] Execution through GoalRun/MasterAgent
- [ ] Web Push reuse (P5)
- [ ] Bot team/groups reuse (P1/P2)
# NANO-W7 Report: Chat-app channels + morning feed

Branch `feat/nano-channels-feed`. Design rationale in
`docs/NANO_W7_DESIGN.md`; this report is authoritative about what was built.

## Starting point: the WIP was broken

The branch arrived at commit `47aa5989` in a non-functional state. This is
recorded because it shaped most of the work:

| Problem | Evidence |
|---|---|
| A bad find/replace had rewritten `from veya.` to `from那些.` in 5 files | 22 corrupt imports; `veya.oskill.im` could not be imported at all |
| `veya/omodul/__init__.py` had lost its original export block while `__all__` still listed the names | `from veya.omodul import VoiceAgent` would `NameError` |
| `oskill/im/{telegram,feishu,dingtalk,wecom}.py` ended with `from .<self> import ...` | self-import; the pure formatters they referenced never existed |
| `oskill/im/wecom.py` was entirely fabricated around that self-import | module unloadable |
| `daily_brief` imported `veya.oservi.*` (R1) and `server.bot_team.push` (R2) | `check_no_reverse_dep` returncode 0 → 1 |
| `create_daily_brief_routine` passed `metadata=` to a `RoutineSpec` that has no such field, and omitted required fields | `TypeError` on first call |
| the routine-clock daily-brief branch keyed on `handoff["metadata"]` | handoffs never carry `metadata`; the branch was unreachable |
| the brief was written into a `TemporaryDirectory` that was then deleted | "stored as an artifact" did not hold |
| `_send_channel` returned `"binding lookup not implemented"` | channel delivery was a stub |
| `PushNotifier.notify(...)` was called | no such method; wrapped in `except Exception`, so Web Push silently no-opped |
| routes took `current_user_id` / `owner_user_id` as **query parameters** | any caller could bind or read a channel as another user |
| the bindings lived in module-level dicts | every binding lost on restart |
| `channels_router` was never included in `app.py` | the whole feature was unreachable |
| Feishu signature verification used `app_secret` as the HMAC key and newline-joined the string | not the documented algorithm |
| module-level `verify_*` duplicated `Client.verify_signature` | §1.4 single-source violation |
| the Telegram secret check returned `True` when no secret was configured | failed open |

## What was built

1. **Channel adapter layer** (`server/channels/`) — framework-free adapters for
   Telegram, Feishu/Lark, DingTalk and WeCom: verify the inbound signature,
   normalise the payload into a `ChannelMessage`, and post chunked replies.
   Plus a persistent binding table, one-time linking codes, an obase client
   factory, dispatch into the existing bot/group path, and the approvals seam.
2. **Morning feed** — the `daily_brief` omodul renders and persists the brief;
   `server/bot_team/morning_feed.py` gathers sections, delivers via the
   existing Web Push (P5) and the channel adapters, and supplies the routine
   clock dispatch seam. `assemble_morning_feed` assembles the existing
   `sequential_composer` skeleton via `ServiceManifest`.
3. **HTTP surface** (`server/routes/channels.py`, mounted in `app.py`) —
   webhooks, binding CRUD, feed config. Flag-gated and auth-bound.
4. **UI** — `/settings/channels` (bind/unbind, per-platform status, feed config).
5. **Approval hook** (spec item 2) — render-only, no dependency on W6.

## Element → layer → why

| Element | Layer | Why this layer |
|---|---|---|
| `verify_telegram_signature` / `verify_feishu_signature` / `verify_dingtalk_signature` / `verify_wecom_signature` / `verify_wecom_url_signature` | obase `veya/obase/channels/__init__.py` | Crypto oprims: one atomic operation, no business meaning. Canonical single source (§1.4); `DingTalkClient.verify_signature` and `WeComClient.verify_signature` delegate to them. |
| `chunk_text` | obase `veya/obase/channels/__init__.py` | One atomic string split shared by every formatter and by reply delivery; a `max_len` override keeps per-platform sizing in one place. |
| `ChannelMessage` / `ChannelReply` / `ChannelBinding` | obase `veya/obase/channels/__init__.py` | Transport data shapes — "what the data looks like" belongs to obase, not to an algorithm layer (§7.4). |
| `TelegramClient`, `FeishuClient`, `DingTalkClient`, `WeComClient` | obase `veya/obase/channels/<platform>.py` | External-world adapters with heavy HTTP dependency (§7.2/§7.3); obase imports no 3O layer. |
| `escape_markdown`, `escape_html`, `format_telegram_code_block`, `format_telegram_mention`, `format_telegram_message` | oskill `veya/oskill/im/telegram.py` | Pure formatting composing ≥2 oprims (`chunk_text` + escaping); stateless, no IO, no `user_id`. |
| `build_text_block` / `build_at_block` / `build_link_block`, `format_feishu_post_content`, `format_feishu_message` | oskill `veya/oskill/im/feishu.py` | Feishu `post` documents are pure data shaping. |
| `escape_dingtalk_markdown`, `format_dingtalk_{text,markdown,link}_message`, `format_dingtalk_message` | oskill `veya/oskill/im/dingtalk.py` | Payload envelopes + heading normalisation are pure. |
| `escape_wecom_markdown`, `format_wecom_{text,markdown,news}_message`, `format_wecom_text_reply_xml` | oskill `veya/oskill/im/wecom.py` | Message shaping, including the callback XML reply. Moved out of obase so formatting has one home. |
| `format_slack_message` and friends | oskill `veya/oskill/im/slack.py` | Already pure; `max_len` was dead (the platform table always won) and is now honoured. |
| `emit_event` call inside the brief | oprim `veya/oprim/event.py` | One atomic event emit; the omodul composes it (§5.1). |
| `daily_brief`, `DailyBriefConfig`, `DailyBriefInput`, `compute_fingerprint_for` | omodul `veya/omodul/daily_brief.py` | An end-to-end business transaction with completion/failure semantics: standard `(config, input_data, output_dir) -> dict`, `_enabled_pillars = {"report", "cost"}` as `ClassVar`, returns `status`/`error`, never raises on failure, re-raises `CancelledError`. Composes the oprim above plus ≥4 oskill formatters. |
| `ChannelAdapter` + `TelegramAdapter` / `FeishuAdapter` / `DingTalkAdapter` / `WeComAdapter` | Layer 4 `server/channels/` | Transport adapters that need credentials and the FastAPI-era async context. Kept free of FastAPI so they unit-test directly. |
| `ChannelBindingStore` | Layer 4 `server/channels/bindings.py` | Per-channel binding table; service layer owns persistence. Uses the `SubscriptionStore` discipline already in `bot_team` (atomic write + lock + tolerant load). |
| `CodeStore`, `issue_code`, `claim_code` | Layer 4 `server/channels/linking.py` | Linking codes are bearer credentials for a bind operation: single-use, expiring, memory-only. |
| `dispatch_inbound`, `DispatchOutcome`, `anonymous_ref` | Layer 4 `server/channels/dispatch.py` | Routing a chat to a bot/group is service concern; it adds no execution authority and reuses P1/P2. |
| `ApprovalChannel`, `register_approval_handler` | Layer 4 `server/channels/approvals.py` | Spec item 2 hook. Renders already-decided approvals only — no second approval system. |
| `client_for` | Layer 4 `server/channels/clients.py` | Credential ownership and client lifecycle. |
| webhooks / binding CRUD / feed config | Layer 4 `server/routes/channels.py` | HTTP, auth, feature gating (§8). Thin; identity from the bearer token only. |
| `run_morning_feed`, `gather_sections`, `build_routine_dispatch`, `MorningFeedConfig` | Layer 4 `server/bot_team/morning_feed.py` | Delivery is credentialed side-effect work (Web Push, channel post) and belongs to the service layer, not to the omodul. |
| `assemble_morning_feed` | Layer 4 `server/assembly.py` | The assembly point where concrete elements are injected into an engine skeleton (§8). Reuses the existing `sequential_composer`; no new engine. |
| `/settings/channels`, `$lib/channels/api.ts` | Layer 4 `apps/web/` | Project UI (§8). |

## Tests run and results

| Command | Result |
|---|---|
| `pytest -q tests/test_nano_w7_channels.py` | **33 passed** |
| `pytest -q tests/test_lorca_p1_bots.py` | 1 failed, 13 passed, 3 errors — **failure set byte-identical to the parent commit** |
| `pytest -q tests/test_lorca_p2_groups.py` | 2 failed, 2 passed, 7 errors — **identical to parent** |
| `pytest -q tests/test_p3a_bot_isolation.py` | collection error — **identical to parent** (`platform/3O` imports a bare `oskill`) |
| `pytest -q tests/test_product_shell.py` | 4 failed, 9 passed — **identical to parent** |
| `pytest -q tests/test_layer4_service.py` | 1 failed, 15 passed, 13 errors — baseline was 1 failed, **14** passed; the 13 errors are pre-existing |
| `pytest -q tests/guardians/test_3o_migration_guards.py` | 1 failed, 9 passed — **identical to parent** |
| `pnpm run check` (NODE_OPTIONS=--max-old-space-size=2048) | **0 errors**; 31 warnings, all pre-existing in `BrowserPanel.svelte` and `routes/+page.svelte` |
| `pnpm run build` | **OK** |

### Mutation checks (proving the suite is not vacuous)

Each mutation was applied, run, and reverted:

| Mutation | Result |
|---|---|
| Telegram secret check fails open when unconfigured | `test_telegram_without_configured_secret_fails_closed` FAILS |
| Loosen the feed time validator to `\d{2}:\d{2}` | `test_feed_config_rejects_bad_time` FAILS |
| Post the reply to a different chat id | `test_dispatch_routes_to_bot_and_replies` FAILS |

### Defects the tests found (and fixed)

- `_try_claim` bound the `external_chat_id` recorded when the code was issued
  (always empty) instead of the chat the code actually arrived in, so a
  completed binding could never be resolved again.
- The feed time validator accepted `25:99`.

### Defects mypy found (and fixed)

- `WeComClient.decrypt_message` still called `verify_signature` with the old
  keyword set (`encoding_aes_key=`, `payload=`), so every encrypted WeCom
  callback would raise `TypeError` before decryption.
- `morning_feed._gather_memory` called `PersonalRuntimeStore.list_memory_candidates`,
  which does not exist, and ignored that the real API is async — the memory
  section of the brief was silently always empty.
- `FeishuClient.send_message` typed its text argument `str` while the post path
  passes a document; `create_webhook`'s payload was inferred `dict[str, str]`
  and then assigned a list.

## Flags

| Flag | Default | Effect |
|---|---|---|
| `VEYA_CHANNELS` | **off** | Channel webhooks + binding UI. Every route returns 404 while off. |
| `VEYA_MORNING_FEED` | **off** | Feed config routes + the clock dispatch seam. Returns 404 while off. |

Additional environment (never committed, never persisted): `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_SECRET_TOKEN`, `FEISHU_APP_ID`, `FEISHU_APP_SECRET`,
`FEISHU_ENCRYPT_KEY`, `FEISHU_VERIFICATION_TOKEN`, `DINGTALK_APP_KEY`,
`DINGTALK_APP_SECRET`, `DINGTALK_WEBHOOK_URL`, `DINGTALK_WEBHOOK_SECRET`,
`WECOM_CORP_ID`, `WECOM_CORP_SECRET`, `WECOM_AGENT_ID`, `WECOM_WEBHOOK_URL`,
`WECOM_TOKEN`, `WECOM_ENCODING_AES_KEY`, plus `VEYA_CHANNELS_ROOT` and
`VEYA_MORNING_FEED_ROOT` for local state.

## New model-callable tools

**None.** No tool was added to `server/tool_registry.py` or any other registry.
The channel layer and the morning feed are reachable only through HTTP
(webhooks, settings routes) and the existing routine clock. Nothing new is
exposed to the model.

## 3O gates

| Gate | Before this work | After |
|---|---|---|
| `check_no_reverse_dep.py` (baseline) | 0 | **0** |
| `check_oskill_pure.py` (baseline) | 0 | **0** (38 files scanned) |
| `check_no_direct_io.py` (baseline) | 1 (60 findings) | 1 (60 findings) — **FAIL list byte-identical to the parent commit** |
| `OPRIM_PEER_CALLS` | INFO=10 TYPES=2 VIOLATIONS=0 | unchanged |
| `OMODUL_DIRECT_CALLS` | VIOLATIONS=0 EXCEPTIONS=4 | unchanged |
| `DIRECT_MEMORY_WRITE_VIOLATIONS` | 0 | **0** |
| `DIRECT_IO_NEW_VIOLATIONS` | 60 | **60 (zero new)** |

**Zero new violations.** `check_3o_gates.py --strict` still exits 1, solely
because `check_no_direct_io.py` baseline mode is red on this branch — that
failure is present on the parent commit `ed16d068` with the identical finding
list, and none of the 60 findings are in NANO-W7 files. Likewise
`test_direct_io_checker_passes_with_baseline` fails identically before and after.

## Known gaps and limitations

1. **`server.app` does not import on this branch** (pre-existing, unrelated):
   `platform/3O/omodul/closed_loop_intervene.py` imports a bare `oskill`,
   which is not on `sys.path`. This makes 13 errors in `test_layer4_service.py`,
   the collection error in `test_p3a_bot_isolation.py`, and blocks importing
   the whole app. Verified identical on the parent commit; not fixed here
   because it is outside this task and touches frozen paths. The channels
   router is therefore mounted in `app.py` but exercised in tests standalone.
2. **`check_no_direct_io.py` is red** for the pre-existing reason above.
3. **Feed sections are partially wired.** Routines and memory highlights are
   gathered; `goals`, `goalruns` and `rss` are returned empty. The omodul
   renders all five sections correctly (covered by tests); the service layer
   does not yet query those three sources.
4. **RSS/URL list is configured but not fetched.** `rss_urls` is persisted and
   validated, but no fetcher is wired; the section renders empty. The spec's
   "optional RSS/URL list" is therefore config-only today.
5. **WeCom AES decryption is untested** end to end. The signature path is
   covered; `decrypt_message` requires `pycryptodome` and a real AES key. The
   `TypeError` defect above was found statically, not by a failing test.
6. **No background delivery scheduler.** `due_configs()` exists to find due
   users, but nothing calls it on an interval; delivery happens when the
   routine clock dispatches a `daily_brief_*` routine. Registering those
   routines per user is the remaining wiring.
7. **Group delivery is untested.** The bot path is covered by
   `test_dispatch_routes_to_bot_and_replies`; `run_group_turn` is wired but has
   no NANO-W7 test.
8. **`veya/im/` facade does not list `wecom`** in `_SUBMODULES`
   (`veya/im/__init__.py:20`), so `veya.im.wecom` is not aliased. The canonical
   path `veya.oskill.im.wecom` works.

## Notes on scope

`server/app.py` carries exactly three changes (morning-feed dispatch seam,
channels router import, `include_router`). An earlier `ruff --fix` had
reordered four unrelated router imports and split an artifacts import; both were
reverted, and the diff against the pre-WIP commit was re-verified. The
remaining lint delta versus the parent commit is `+1 E402` in `app.py`, which
is the unavoidable cost of adding one router import to the bottom-of-file block
where three others already carry the same finding.
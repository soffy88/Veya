# FCB Specification: FastClaw-Inspired Clean-Room Features

## 1. Overview & Clean-Room Statement
This specification defines two features inspired by concepts in FastClaw (https://github.com/fastclaw-ai/fastclaw):
1. **IM Sandbox Enforcement**: Mandatory execution sandbox routing or denial for tool calls triggered by non-owner IM chatters.
2. **Bot Fork + Public Chat Link**: Reusable bot configuration cloning (forking) and isolated public chat access.

Both features are designed and implemented clean-room without copying source code, adhering strictly to Veya's existing single-authority architecture (`ToolGuard`, `PolicyResolver`, `BotRegistry`, `ConversationStore`, `BotMemoryStore`).

---

## 2. Feature 1: IM Sandbox Enforcement

### 2.1 Feature Flag
- Flag name: `VEYA_IM_SANDBOX_ENFORCE`
- Default: `False`
- Authority: `server/feature_flags.py`

### 2.2 Inbound Chatter Identification
In `server/channels/dispatch.py`:
- Inbound `ChannelMessage` received from channel adapters (Telegram, Feishu, DingTalk, WeCom, Slack, Discord, etc.).
- Binding resolution checks whether `message.external_user_id` matches the owner:
  - Match if `message.external_user_id == binding.get("external_user_id")` or `anonymous_ref(...) == binding.get("owner_ref")` or matching `spec.owner_id`.
  - If match: `is_owner = True`.
  - If mismatch: `is_owner = False`.
- User identity context carries `is_owner` and `is_im_channel = True`.
- Request context binds `im_turn_context(is_im=True, is_owner=is_owner)`.

### 2.3 Sandbox Routing & Denial Rules
When `VEYA_IM_SANDBOX_ENFORCE` is active and `is_im=True` and `is_owner=False`:
1. **Execution / Shell Tools**:
   - `run_in_sandbox`: Permitted when executing locally via existing `omodul.sandbox_scope("chat_verify")`. Denied if targeting a remote SSH host (`host` set).
   - `run_backtest_coprocessor`: Permitted in isolated quant coprocessor sandbox.
   - Unsandboxed shell/exec (`system_secure_exec`, `system_shell_exec`, `ssh_run`, `ssh_bg_start`, `agent_loop_run`, `coding_task_run` without sandbox): **DENIED**.
   - MCP `shell.exec` / `test.run` / `build.run`: **DENIED** unless explicitly routed to an isolated worktree with a restricted sandbox profile (`local_restricted`).
2. **File-Write Tools**:
   - `write_file`: Denied if touching host (`host` set) or protected configuration (`is_protected_config_path`). Local writes permitted only within isolated per-chatter sandbox directory (`~/.veya/sandbox/<user_id>/work`), otherwise denied.
   - In-repo file mutation (`edit_hashline`, `ast_grep_rewrite`, `edit_diagram`, `produce_wechat_article`): **DENIED** against owner workspace.
3. **Owner Turns & Flag OFF**:
   - When `is_owner=True` or `VEYA_IM_SANDBOX_ENFORCE=False`: Zero behavior change; execution follows existing policy chains.

---

## 3. Feature 2: Bot Fork + Public Chat Link

### 3.1 Feature Flag
- Flag name: `VEYA_BOT_PUBLIC_SHARE`
- Default: `False`
- Authority: `server/feature_flags.py`

### 3.2 Bot Forking
- Endpoint: `POST /api/v1/bots/{bot_id}/fork`
- Permissions: Owner-only. Non-owner or non-existent bot returns 404 (prevents bot enumeration). If flag OFF -> 404.
- Behavior:
  - Deep-copies persona/identity: `name`, `description`, `instructions`, `avatar`, `personality`.
  - Copies skills: `available_skill_ids`, `available_playbook_ids`.
  - Copies model defaults: `provider`, `model`, `runner_id`, `model_source`, `model_source_config`.
  - **Explicitly Excludes**:
    - Memory (`MEMORY.md` and topic files in `BotMemoryStore`).
    - Conversations / sessions (`messages.json` in `ConversationStore`).
    - Secrets (`bot_secret_store`).
  - Sets `owner_id = current_user.user_id`, `public_share = False`.

### 3.3 Public Share Toggle
- `BotSpec.public_share: bool = False` added to `server/goal_run/bot_identity.py`.
- Endpoint: `POST /api/v1/bots/{bot_id}/public_share`
- Body: `{"enabled": bool}`
- Permissions: Owner-only. Non-owner returns 404. If flag OFF -> 404.

### 3.4 Public Chat Endpoints
1. `GET /api/v1/bots/{bot_id}/public`:
   - Returns public bot metadata (id, name, description, avatar, public_share) if `public_share=True`.
   - Requires authenticated user. Returns 404 if bot not found, not public, or flag OFF.
2. `GET /api/v1/bots/{bot_id}/public/messages`:
   - Returns conversation messages for the calling signed-in user only (isolated per chatter).
3. `POST /api/v1/bots/{bot_id}/public/messages`:
   - Runs one bot turn using persona and skills from owner's bot.
   - **Isolation**:
     - Chatter gets a separate conversation partition: `ConversationStore` scoped per chatter.
     - Chatter gets separate memory: `BotMemoryStore` scoped per chatter.
     - Secrets: Owner's bot secrets are NOT injected or accessible to public chatters.
     - Tools: Non-owner chatters are subject to the same sandbox enforcement as Feature 1 (cannot touch owner's workspace, hosts, secrets, or unsandboxed commands).

### 3.5 Minimal Frontend
In `apps/web/src/routes/team/bot/[bot_id]/SettingsTab.svelte`:
- "Fork Bot" button triggering `POST /api/v1/bots/{bot_id}/fork`.
- "Public Access" toggle switch triggering `POST /api/v1/bots/{bot_id}/public_share`.
- "Copy Public Link" button when public access is enabled.
- UI elements gracefully hidden or disabled when server flag is OFF.

---

## 4. Verification & Regression Plan
- Unit & integration tests in `tests/test_fastclaw_borrow.py`:
  - Feature 1: IM non-owner turn sandboxed/denied; IM owner turn allowed; flag OFF no-op.
  - Feature 2: Fork copies persona/skills/model and excludes memory/sessions/secrets; owner checks enforced; public toggle works; public chat isolation across chatters; flag OFF returns 404.
- Full regression suite execution (1,140+ tests).
- Frontend typecheck (`pnpm check`) and build (`pnpm build`).

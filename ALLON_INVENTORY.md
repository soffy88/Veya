# ALLON_INVENTORY.md — 新增但未开功能盘点

分支 `feat/all-on`，基于 canonical main `408f3c84`。对照 `server/feature_flags.py`（唯一权威表）
与 `deploy/bot_landing_flags.env`（生产容器已开开关）。

判定口径：注册表 `default=False` 且 **未** 出现在 `bot_landing_flags.env` 中的开关，即为
"新增但未开"。已写进 env 的视为生产已开，不再动。

> 安全约束：本文件只盘点 + 列出缺口。补接线与开关开启在后续步骤落到代码与 env，
> 不触碰 `.venv` / `platform/3O` / 其他 worktree / 生产数据库。

---

## 1. 生产已开（env 中已是 1，无需处理）

| 开关 | 说明 |
|---|---|
| VEYA_APPROVAL_GATES | 审批闸门 |
| VEYA_CHANNELS (NANO-W7) | 频道 |
| VEYA_PROVIDER_REGISTRY (NANO-W8) | 供应商注册表 |
| VEYA_PROMPT_CACHE / VEYA_CONTEXT_STEWARD / VEYA_MEMORY_REVIEW / VEYA_READ_OWN_CHAT / VEYA_PERSISTENT_WORKERS | NANO-W10 A/C |
| VEYA_AUTO_REVIEW / VEYA_GROUP_ROLES / VEYA_BOT_SECRETS | NANO-W10 Group C |
| VEYA_A11Y_FIRST / VEYA_DOM_READER / VEYA_CHANNEL_LADDER | NANO-W10 Group D |
| VEYA_TURN_QUEUE / VEYA_WAKE_ROUTER / VEYA_LOOP_MODE / VEYA_FINISH_CHECKPOINT | NANO-W10 Group A |
| VEYA_BOT_MEMORY_MD | bot 记忆 md |
| VEYA_BRAIN_BREAKER / VEYA_SUPERVISOR (W12) | 监工 |
| VEYA_REMOTE_CONTROL (W13) | 远端控制 |
| VEYA_MCP_PRESETS (W14) | MCP 预设包 |

---

## 2. 新增未开 — 本轮要打开（默认 OFF 且不在 env）

| # | 开关 | 波次 | 后端路由（现状） | 前端入口（现状） | 缺口 | 决策 |
|---|---|---|---|---|---|---|
| 1 | VEYA_ROLE_SKILL_PRESETS | W15 | `server/routes/role_skill_presets.py`：`GET /api/v1/presets/roles`、`GET /api/v1/presets/skills`、`POST /api/v1/bots/{bot_id}/presets/apply`（owner 校验，flag 关 409）。已挂 `app.include_router` | `apps/web/src/lib/team/rolePresets.ts` + `routes/team/bot/[bot_id]/PresetPicker.svelte`（flag 关隐藏） | 无——路由 + 前端都齐 | **开** |
| 2 | VEYA_SSH_HOSTS | W17 | `server/routes/remote_control.py:387` SSH hosts 段（flag 关 404）；`server/remote_control/ssh_tools.py` 工具。`coordinator_master.py:575` 注册。已挂 router | `apps/web/src/lib/remote/api.ts` + `routes/remote/+page.svelte`（flag 关显示禁用） | 无 | **开** |
| 3 | VEYA_PROJECT_BRAIN | W16 | `server/routes/projects.py` 完整投影（list/create/brief/messages/tasks/action，flag 关走内存回退）。`project_brain/*` 工具已接 `master_tools` | `apps/web/src/lib/projects/api.ts` + `routes/projects/+page.svelte` 及 `[id]` 页 | 无（前端已写，flag 关隐藏） | **开** |
| 4 | VEYA_BOT_PUBLIC_SHARE | FastClaw | `server/routes/bot_team.py:543/602/643/671/694` fork / toggle / public / public messages（全部 flag 关 404，owner 校验；访客隔离在 `sandbox_enforce.py` 与 `bot_team.py:702`） | `routes/team/bot/[bot_id]/SettingsTab.svelte`（fork / public share 开关，已接 `forkBot` `toggleBotPublicShare`） | 无——单 bot 须 owner 显式 toggle 才公开，已满足"显式生成链接"语义（公开即生成可访问链接）。访客隔离未放松 | **开**（保持显式） |
| 5 | VEYA_SKILL_PROGRESSIVE | clean-room | `server/skill_progressive.py`；在 `server/bot_team/turns.py:97` 注入 skills block；`tool_registry.py:4917` / `skill_hub.py` 读取 | 无 UI | 后端行为由 prompt 注入控制，无独立配置 UI 需求；可不加 UI（属"开关关闭时前端优雅隐藏"——本就无入口）。**不强加 UI** | **开**（无前端，可接受） |
| 6 | VEYA_WORKSPACE_URI | clean-room | `server/workspace_uri.py`；`server/routes/fs.py:142/194` 用 `veya://workspace` 虚拟路径 | 无 UI（是 fs 层虚拟 URI，前端通过现有文件 API 间接受益） | 无独立 UI 需求 | **开**（无前端，可接受） |
| 7 | VEYA_MEMORY_UI | clean-room | `server/bot_team/turns.py:110`、`server/bot_team/memory.py:260`（USER_GLOBAL.md 读/写注入） | `routes/team/bot/[bot_id]/MemoryTab.svelte` 已存在（list/put memory），但无显式"memory UI"开关入口——功能靠 prompt 注入触发 | 可选：加一个设置项显示"用户全局背景已启用"。不阻断开启 | **开**（无新增前端入口，复用 MemoryTab） |
| 8 | VEYA_APPROVAL_ASK_ONCE | W17 | `server/ask_once_cache.py`：`is_ask_once_enabled()`；在 approvals 路径缓存会话级"已问一次"。`test_w17_ask_once_cache.py` 已测 | 无 UI（是审批后端去重，对用户体验透明） | 无 UI 需求 | **开** |
| 9 | VEYA_IM_SANDBOX_ENFORCE | FastClaw | `server/channels/sandbox_enforce.py`：`check_im_sandbox_tool` 拦截非 owner IM/公开聊天的 exec/写/秘钥工具；与 VEYA_BOT_PUBLIC_SHARE 联动 | 无 UI（安全策略，前端无需开关页） | 无 UI 需求；开启后收紧 IM/公开聊天执行隔离 | **开**（安全，保持显式收紧） |
| 10 | VEYA_TAILNET_CHANNEL | W20 | `server/routes/tailnet.py`（owner-only status/policy/peers/requests；已挂 router）；`server/tailnet_channel.py` 监听器（默认 deny，bind 仅 Tailscale IP，端口 7767） | 无 UI（spec 明确"No frontend change"） | **需配置**：`VEYA_TAILNET_OWNER_USER_ID` 须设置（从 owner 解析/DB 读取，不改数据）；policy 默认值须只允许 `soffy88@gmail.com` 的 Tailscale login（deny 优先）。见步骤 3 | **开 + 配置**（特殊） |
| 11 | VEYA_DEEPEVAL | W18 | `server/deepeval_adapter.py` + `server/supervisor/reporting.py:122`；`available()` 检测可选依赖 `deepeval`，未装 → 全 no-op 降级 | 无 UI | 可选依赖未装 → 优雅降级，UI 显示"未安装"。**不**往共享 `.venv` 安装 | **开**（依赖缺失即降级，不安装） |

---

## 3. 排除项（本轮**不**开，单独标注原因）

| 开关 | 默认 | 原因 | 备注 |
|---|---|---|---|
| VEYA_MORNING_FEED (NANO-W7) | OFF | 不在任务清单重点，且依赖频道早报配置；非"新增未开"核心项 | 如需后续单独开 |
| VEYA_HANDS (NANO-W8) | OFF | 实验性多端手势/接管，未在前端接线，风险不明 | 实验 |
| VEYA_CLI_HUB (NANO-W8) | OFF | CLI 中枢，需本地 CLI 代理，环境相关 | 实验 |
| VEYA_RUNNER_SELFUPDATE (NANO-W8) | OFF | 自我更新 runner，属"调度/副作用"敏感面，违反"不新增副作用权威"精神，且需写权限 | 危险/敏感 |
| VEYA_DEVICE_PAIRING | OFF | 设备配对，安全敏感，需配对流程与密钥 | 安全，未接线 |
| VEYA_DEVICE_PAIRING 之外的 DEFAULT-ON 注册表项 | True | 生产已默认开，不动 | — |

> 说明：`VEYA_DEEPEVAL` 视为"需可选依赖"项：开启开关，但 `deepeval` 未装时后端 `available()==False` 自动降级，UI/报告标注"未安装"，全程零付费、不触碰 `.venv`。

---

## 4. 缺口汇总（需补接线 / 配置）

1. **前端**：W15/W16/W17/FastClaw 前端入口**已存在**，仅受 flag 隐藏——开启后自动显现，无需新写（确认 `pnpm check` 0 error）。
   - VEYA_SKILL_PROGRESSIVE / VEYA_WORKSPACE_URI / VEYA_MEMORY_UI / VEYA_APPROVAL_ASK_ONCE / VEYA_IM_SANDBOX_ENFORCE / VEYA_DEEPEVAL 无独立 UI，按"开关关闭时前端优雅隐藏"原则本就无入口，可接受。
2. **Tailnet（W20）配置**：`VEYA_TAILNET_OWNER_USER_ID` 须写入 env（步骤 3 取 owner user_id）；默认 policy 已 deny-all（allowlist 空），需确认默认仅放行 `soffy88@gmail.com` Tailscale login——通过写入初始 policy（mode=allowlist，allow=["user:soffy88@gmail.com"]）或依赖 `tailnet` mode；**deny 优先**，端口 7767，仅监听 Tailscale IP（代码已保证）。
3. **公开分享隔离**：已满足 owner 显式 toggle + 访客隔离（sandbox_enforce + 单 bot 公开页）。开启即可。
4. **noop 无代码改动项**：上述所有开关的后端路由与模型工具均已实现并挂入 app；开启只是把 flag 由 OFF→ON。

---

## 5. 新增的模型可调用工具（开关开启后注册进 master_tools）

| 工具 | 开关 | 来源 |
|---|---|---|
| tailnet_send / tailnet_request_status / tailnet_fetch_attachments | VEYA_TAILNET_CHANNEL | `server/tailnet_channel.py`（flag 关全部失败时关闭） |
| W17 ssh host 工具（ssh_run / ssh_bg_start / ...，transport=ssh） | VEYA_SSH_HOSTS + VEYA_REMOTE_CONTROL | `server/remote_control/ssh_tools.py` |
| W16 Project Brain 工具（project_brief_read/update、project_task_start/status/steer/cancel、project_log_append） | VEYA_PROJECT_BRAIN | `server/project_brain/tools.py` |
| W15 预设应用（非模型工具，是 REST apply） | VEYA_ROLE_SKILL_PRESETS | `server/routes/role_skill_presets.py` |

> 注：W12/W13/W14/W15/W16/W17 的"新增工具"均为既有 master_tools 注册路径在 flag ON 时启用；不新建第二套工具注册中心。

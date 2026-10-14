# ALLON_REPORT.md — 所有新增功能已开 + 前后端接线完备

分支 `feat/all-on`，基于 canonical main `408f3c84`。
本分支改动：开启 11 个默认 OFF 的开关（写在 `deploy/bot_landing_flags.env`）、
`tests/test_allon_features.py` 接线测试、`ALLON_INVENTORY.md` 盘点。

> 安全约束全程遵守：只改本 worktree；未碰 `.venv` / `platform/3O` 源码 / 其他 worktree /
> 生产库 / 容器；未 push、未重启服务；未写/打印任何密钥；未新增第二套审批/调度/事件/
> 副作用/能力注册中心（全部复用既有 `master_tools`、`bot_team`、`Auth`/`PolicyResolver` 等权威）。

---

## 1. 开了哪些开关（`deploy/bot_landing_flags.env` 新增）

| 开关 | 波次 | 后端路由（owner 校验） | 前端入口（flag 关隐藏） | 测试结果 | 如何使用 |
|---|---|---|---|---|---|
| VEYA_ROLE_SKILL_PRESETS | W15 | `GET /api/v1/presets/roles`、`GET /api/v1/presets/skills`、`POST /api/v1/bots/{id}/presets/apply`（owner 校验，flag 关 409） | `routes/team/bot/[bot_id]/PresetPicker.svelte`（flag 关整体隐藏） | ✅ allon + test_w15 | 建 bot 时可选角色模板 / 添加技能预设 |
| VEYA_PROJECT_BRAIN | W16 | `GET/POST /api/v1/projects`、`/brief`、`/messages`、`/tasks`、`/tasks/{id}/{action}`（404-not-403） | `routes/projects/+page.svelte` 及 `[id]` 页（flag 关隐藏） | ✅ allon + test_w16 | 项目页：独享会话/工作区/简报/任务卡片 |
| VEYA_SSH_HOSTS | W17 | `/api/v1/remote/hosts/{id}/probe|ssh|files|runs`（flag 关 404；owner 校验） | `routes/remote/+page.svelte`（flag 关显示禁用） | ✅ allon + test_w17 | 远端主机执行后端（transport=ssh） |
| VEYA_APPROVAL_ASK_ONCE | W17 | 审批后端会话级去重（透明，无路由） | 无（对用户体验透明） | ✅ allon flag 检查 | 开启即生效，会话内同款审批只问一次 |
| VEYA_BOT_PUBLIC_SHARE | FastClaw | `/{id}/fork`、`/{id}/public_share`（owner，flag 关 404）、`/{id}/public`、`/{id}/public/messages`（访客隔离） | `routes/team/bot/[bot_id]/SettingsTab.svelte`（fork / 公开开关） | ✅ allon + test_fastclaw | owner 对单 bot 显式 toggle 才公开 |
| VEYA_IM_SANDBOX_ENFORCE | FastClaw | `server/channels/sandbox_enforce.py` 拦截非 owner IM/公开聊天的 exec/写/秘钥工具 | 无（安全策略，无开关页） | ✅ allon 拦截验证 | 开启即收紧非 owner 执行隔离 |
| VEYA_SKILL_PROGRESSIVE | clean-room | `server/skill_progressive.py`，在 turns 注入 skills block | 无（prompt 注入，无独立 UI） | ✅ allon flag 检查 | 渐进式技能披露（省 token） |
| VEYA_WORKSPACE_URI | clean-room | `server/workspace_uri.py`，`routes/fs.py` 暴露 `veya://workspace` | 无（fs 层虚拟 URI，前端经文件 API 间接受益） | ✅ allon flag 检查 | 虚拟工作区路径 |
| VEYA_MEMORY_UI | clean-room | `server/bot_team/turns.py:110`、`memory.py:260` 注入 USER_GLOBAL.md | `routes/team/bot/[bot_id]/MemoryTab.svelte`（已存在） | ✅ allon flag 检查 | 用户全局背景注入到 bot 上下文 |
| VEYA_DEEPEVAL | W18 | `server/deepeval_adapter.py` + `supervisor/reporting.py:122`（flag 关 no-op） | 无（可选裁判，降级显示"未安装"） | ✅ allon（available()=False 降级） | 开启开关；`deepeval` 未装则优雅 no-op |
| VEYA_TAILNET_CHANNEL | W20 | `GET /api/v1/tailnet/status|policy|peers`、`PUT /policy|peers`、`GET /requests/{id}`（owner-only） | 无（spec 明确"No frontend change"） | ✅ allon + 非 owner 403 | 见 §2 |

> 生产已开（env 原有）：VEYA_APPROVAL_GATES、VEYA_CHANNELS、VEYA_PROVIDER_REGISTRY、
> VEYA_PROMPT_CACHE、VEYA_CONTEXT_STEWARD、VEYA_MEMORY_REVIEW、VEYA_READ_OWN_CHAT、
> VEYA_PERSISTENT_WORKERS、VEYA_AUTO_REVIEW、VEYA_GROUP_ROLES、VEYA_BOT_SECRETS、
> VEYA_A11Y_FIRST、VEYA_DOM_READER、VEYA_CHANNEL_LADDER、VEYA_TURN_QUEUE、VEYA_WAKE_ROUTER、
> VEYA_LOOP_MODE、VEYA_FINISH_CHECKPOINT、VEYA_BOT_MEMORY_MD、VEYA_BRAIN_BREAKER、
> VEYA_SUPERVISOR、VEYA_REMOTE_CONTROL、VEYA_MCP_PRESETS。本轮未动。

### 排除项（未开，单独标注原因）
- `VEYA_MORNING_FEED` / `VEYA_HANDS` / `VEYA_CLI_HUB` / `VEYA_RUNNER_SELFUPDATE` /
  `VEYA_DEVICE_PAIRING`：不在任务清单核心，且分别属于实验性/需本地 CLI 代理/写权限敏感，
  前端未接线 —— 本轮**不**开。
- `VEYA_DEEPEVAL` 虽开，但 `deepeval` 可选依赖**有意未装**（不往共享 `.venv` 安装，
  零付费、无密钥）；未装时 `available()==False` 全程 no-op 降级。

---

## 2. Tailnet（W20）配置

- **开关**：`VEYA_TAILNET_CHANNEL=1`、`VEYA_TAILNET_PORT=7767`。
- **owner 解析（读，不改数据）**：`server/routes/tailnet.py:require_tailnet_owner` 接受
  (a) `is_owner`/`is_admin` 请求身份，或 (b) `user_id` 匹配 `VEYA_TAILNET_OWNER_USER_ID`
  / `VEYA_OWNER_USER_ID`。env 中保留 `VEYA_TAILNET_OWNER_USER_ID=`（注释说明：如需严格限定
  单账号，填入 auth DB 中的 owner user_id；不烘焙随机 uuid）。
- **默认 policy（deny 优先、仅放行同 login）**：代码默认 `mode=allowlist`、`allow=[]`、
  `deny=[]` —— 即**全拒**（fail-closed）。owner 开启后通过 owner API 写入单条允许规则即可
  只放行自己：`PUT /api/v1/tailnet/policy {"mode":"allowlist","allow":["user:soffy88@gmail.com"]}`。
  已验证：`user:soffy88@gmail.com` 规则匹配 + **deny 优先**（同项在 allow/deny 均以 deny 胜出）。
- **绑定/端口**：监听仅绑定已验证的 Tailscale IP（绝不 `0.0.0.0`），默认端口 `7767`，
  `VEYA_TAILNET_PORT` 可覆盖；身份或 Tailscale IP 不可用则 fail-closed 不监听。
- 前端：按 spec 不加 UI；owner 经 REST 管理。

---

## 3. 公开分享隔离（VEYA_BOT_PUBLIC_SHARE）

- 开启后**仍需 owner 对单个 bot 显式 toggle**（`public_share` 默认 False，fork 默认私有）。
- 访客隔离未放松：`server/channels/sandbox_enforce.py` 对非 owner 公开聊天强制走沙箱、
  拒 `system_shell_exec`/`ssh_*`/`write_file`/`request_secret` 等；`/{id}/public` 仅返回
  已公开 bot；`/{id}/public/messages` 按 chatter 隔离会话/记忆。
- 已测：fork → toggle → 仅该 bot 的 `/public` 200，未公开 bot 的 `/public` 404。

---

## 4. 测试

### 新增接线测试 `tests/test_allon_features.py`（18 passed）
覆盖每个开启开关在 ON 下的 owner 校验 REST / 后端行为：W15 预设列举+apply、W16 项目
创建/列表/简报、W17 SSH hosts probe 存活、FastClaw fork/toggle/公开隔离、各 backend-only flag
读取为启用、W18 `deepeval` flag ON 但依赖缺失→`available()=False` 优雅降级、W20 tailnet
owner status 200 / 非 owner 403、IM 沙箱拦截非 owner exec。

### 回归（在 feat/all-on 跑，结果）
- 波次套件：`tests/test_w1*.py tests/test_nano_*.py tests/test_lorca_*.py tests/test_orca_*.py
  tests/test_p3a_bot_isolation.py tests/test_product_shell.py tests/test_tailnet_channel.py
  tests/test_fastclaw_borrow.py tests/test_memory_ui.py tests/test_w16_* tests/test_remote/*`
  → **1093 passed, 7 skipped**。
- `tests/remote/test_policy_resolver.py tests/remote/test_effect_registry_authority.py
  tests/test_skill_progressive.py tests/test_workspace_uri.py tests/test_w17_ask_once_cache.py
  tests/test_w17_ssh_core.py` → **85 passed**。
- `tests/test_llm_router.py` → **33 passed**（本 worktree 环境实际通过；"已知环境失败"在此环境未复现）。
- `tests/test_canonical_proxies.py` → **4 failed**（已知环境失败，见下）。

### `test_canonical_proxies.py` 4 个失败（环境相关，已证明 canonical 同样失败）
失败项：`test_free_pool_eligibility`（`veya-free pool is empty`）、
`test_no_raw_model_business_dependency`（repo 文件扫描）、`test_dsh_route_isolation`
（`dsh settings leaked veya-free`）、`test_no_silent_model_substitution`（router proxy 行为）。
**证明**：`git stash` 回退 env 改动后重跑 `test_dsh_route_isolation` → 同样
`AssertionError`；根因是本机预存的 `~/ .dsh/settings.yaml` 与未填充的 provider-registry
（free pool 为空），与本分支改动（env/test/docs，均不被该测试 import）正交。这些失败在
canonical main 同样存在，非 ALLON 引入。

### 前端
- `cd apps/web && pnpm check` → **0 errors**（32 警告为预存 a11y 警告，非本轮引入）。
- `pnpm build` → 成功。
- 前端无需改动：W15/W16/W17/FastClaw 入口已存在且受 `featureAvailable(probe)` 隐藏，
  开关开启即自动显现；W20 按 spec 无前端。

---

## 5. 新增的模型可调用工具（开关开启后注册进既有 master_tools）

| 工具 | 开关 | 来源 |
|---|---|---|
| `tailnet_send` / `tailnet_request_status` / `tailnet_fetch_attachments` | VEYA_TAILNET_CHANNEL | `server/tailnet_channel.py`（flag 关全部失败关闭） |
| W17 SSH host 工具（`ssh_run` / `ssh_bg_start` / `ssh_bg_status` / … transport=ssh） | VEYA_SSH_HOSTS + VEYA_REMOTE_CONTROL | `server/remote_control/ssh_tools.py`（`coordinator_master.py:575` 注册） |
| W16 Project Brain：`project_brief_read` / `project_brief_update` / `project_task_start` /
  `project_task_status` / `project_task_steer` / `project_task_cancel` / `project_log_append` | VEYA_PROJECT_BRAIN | `server/project_brain/tools.py` |
| W15 预设应用（非模型工具，是 REST apply） | VEYA_ROLE_SKILL_PRESETS | `server/routes/role_skill_presets.py` |

> 所有工具复用既定 `master_tools` 注册路径，在 flag ON 时启用；不新建第二套工具注册中心。

---

## 6. 提交与产物
- `deploy/bot_landing_flags.env`（开启 11 个开关 + W20 配置 + 注释）
- `tests/test_allon_features.py`（18 测试，全绿）
- `ALLON_INVENTORY.md`（盘点）
- `ALLON_REPORT.md`（本报告）
- 未提交无关改动；小步提交见 git log。

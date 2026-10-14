# FastClaw 概念净室借鉴实现报告 (FCB_REPORT)

## 1. 任务概述与 Clean-Room 说明
本项目受 FastClaw (https://github.com/fastclaw-ai/fastclaw) 启发，对两项关键功能进行了纯净室（Clean-Room）设计与实现：
1. **IM 沙箱强制拦截 (IM Sandbox Enforcement)**：针对来自 IM 渠道（Telegram、Discord、Slack、微信、飞书等）的非 Bot 所有者会话，强制执行现有沙箱隔离或直接拒绝未经沙箱的执行/写操作；所有者会话保持原样。
2. **Bot 克隆与公开对话链接 (Bot Fork + Public Chat Link)**：Bot 所有者可将现有 Bot 的人设与技能克隆为新 Bot（不泄露记忆、会话与密钥）；并可开启公开访问，允许其他登录用户通过独立且沙箱隔离的会话与 Bot 对话。

**严格遵守安全与架构红线：**
- **Clean-room 实现**：未阅读、复制或引入任何 FastClaw 源码，完全基于需求规范进行净室设计与实现。
- **单一权威原则（Single Authority）**：未新增第二套调度器、审批流、事件存储、能力注册表或权限系统，完整复用 Veya 既有的 `ToolGuard`、`PolicyResolver`、`BotRegistry`、`ConversationStore`、`BotMemoryStore`。
- **特性开关默认 OFF**：新特性均置于特性标志保护下（`VEYA_IM_SANDBOX_ENFORCE` 和 `VEYA_BOT_PUBLIC_SHARE`，默认均为 `False`），关闭时行为与原有系统 100% 保持一致。

---

## 2. 特性 1：IM 沙箱强制拦截 (Feature 1: IM Sandbox Enforcement)

### 2.1 特性标志与调度接入
- **标志定义**：`server/feature_flags.py` 中注册 `VEYA_IM_SANDBOX_ENFORCE`（默认 `False`，类型 `safety`）。
- **非所有者身份识别**：在 `server/channels/dispatch.py` 中解析入站 `ChannelMessage`，比对 `external_user_id` 与绑定关系的 `external_user_id` / `owner_ref`；若不匹配则标记 `is_owner=False`。
- **回合上下文绑定**：`server/channels/sandbox_enforce.py` 提供 `bind_im_turn(...)` 与 `is_im_non_owner_turn()`，通过 `contextvars` 贯穿整个会话回合生命周期。

### 2.2 统一拦截规则
当 `VEYA_IM_SANDBOX_ENFORCE` 为 `True` 且为非 owner 回合时：
1. **执行与终端工具**：
   - `run_in_sandbox`（本地执行）：放行，走现有的 `omodul.sandbox_scope("chat_verify")` 沙箱容器。若指定了远程 `host` 则拒绝。
   - `run_backtest_coprocessor`：放行（量化隔离协处理器）。
   - 未沙箱化的执行工具（`system_shell_exec`、`system_secure_exec`、`ssh_run`、`agent_loop_run`、`coding_task_run` 等）：**直接拒绝**。
   - MCP 主机命令（`shell.exec` 等，目标为 `HOST` 或 profile 为 `l0_host`）：**直接拒绝**。
2. **文件写入与工作区变更**：
   - `write_file`：远程主机写、受保护配置文件（`is_protected_config_path`）、未绑定沙箱目录时针对工作区根目录的写入均被**直接拒绝**；仅允许写入为该用户独立绑定的临时沙箱目录（`~/.veya/sandbox/im/<user_ref>`）。
   - 工作区侵入性修改工具（`edit_hashline`、`ast_grep_rewrite`、`edit_diagram` 等）：**直接拒绝**。
3. **密钥凭据**：
   - 密钥查询工具（`request_secret`、`list_secrets`）：**直接拒绝**。

### 2.3 双重权威校验集成
- **ToolGuard 策略层**：在 `server/tool_guard_policies.py` 中注册 `im_sandbox_policy`，未走治理流程的常规 Bot 会话直接在 `MasterToolRegistry` 前置守卫处拦截并报错 `ToolDenied`。
- **PolicyResolver 安全层**：在 `veya/remote/policy_resolver.py` 的 `SecurityPolicy.evaluate` 中挂载 `is_im_non_owner_turn()` 判定，任务治理链中的非 owner 调用统一给出 `LayerOutcome.DENY`。

---

## 3. 特性 2：Bot 克隆与公开对话链接 (Feature 2: Bot Fork + Public Chat Link)

### 3.1 特性标志与模型扩展
- **标志定义**：`server/feature_flags.py` 中注册 `VEYA_BOT_PUBLIC_SHARE`（默认 `False`，类型 `safety`）。
- **BotSpec 扩展**：`server/goal_run/bot_identity.py` 中增加 `public_share: bool = False`，并在 `from_dict` 与 `to_dict` 保持完全向后兼容。

### 3.2 REST API 端点设计
所有端点均定义于 `server/routes/bot_team.py`：
1. `POST /api/v1/bots/{bot_id}/fork` (所有者专用)：
   - 权限：校验调用者必须为该 Bot 的 `owner_id`，否则返回 404（防枚举）。开关为 OFF 时亦返回 404。
   - 克隆范围：深度复制人设信息（`name`, `description`, `instructions`, `avatar`, `personality`）、模型配置（`provider`, `model`, `runner_id` 等）及关联技能（`available_skill_ids`, `available_playbook_ids`）。
   - **严格隔离**：**不克隆**历史会话、Bot 记忆（`BotMemoryStore`）及私有凭据密钥（`BotSecretStore`）。
2. `POST /api/v1/bots/{bot_id}/public_share` (所有者专用)：
   - 权限：所有者专用，支持启用或停用公开共享。
3. `GET /api/v1/bots/{bot_id}/public` (登录用户公开)：
   - 获取公开 Bot 的安全元数据（名称、简介、头像），仅在 `public_share=True` 时可见。
4. `GET /api/v1/bots/{bot_id}/public/messages` 与 `POST /api/v1/bots/{bot_id}/public/messages`：
   - 允许任意登录用户与公开 Bot 独立对话。
   - **多租户隔离**：对话记录通过代理 Bot ID（`pub_{chatter_id}_{hash}`）隔离存储在独立分区中，互不可见且不污染所有者主 Bot 的会话与记忆。
   - **工具沙箱继承**：自动激活 `bind_im_turn(is_im=True, is_owner=False, is_public_chat=True)`，公开访客无法调用主机命令或篡改所有者工作区。

### 3.3 前端极简交互 (Minimal Frontend)
- **API 封装** (`apps/web/src/lib/team/api.ts`)：导出 `forkBot` 与 `toggleBotPublicShare`。
- **设置面板** (`apps/web/src/routes/team/bot/[bot_id]/SettingsTab.svelte`)：
  - 新增「公开共享与克隆」配置卡片。
  - 提供「克隆 Bot (Fork)」操作按钮，成功后自动跳转至新生成的克隆 Bot。
  - 提供「公开访问」开关控件及一键复制公开对话链接（含复制成功状态反馈）。

---

## 4. 新增模型可调用工具清单

根据项目指示，在此明确声明：
- **新增模型可调用工具数量：0 个**
- **说明**：本次功能完全复用既有的系统工具集（`run_in_sandbox`、`write_file`、`system_shell_exec`、`ssh_run` 等），不引入任何新的模型工具面；通过统一的 `ToolGuard` 和 `PolicyResolver` 对非所有者回合实施沙箱重定向与拒绝拦截。

---

## 5. 验证与回归测试

### 5.1 专项测试 (`tests/test_fastclaw_borrow.py`)
编写了 11 个高覆盖率的集成与单元测试，全部通过（11 passed in 3.40s）：
- `test_feature1_flag_default_off`: 默认 flag OFF。
- `test_feature1_flag_off_no_denial`: flag OFF 时不实施拦截。
- `test_feature1_flag_on_owner_turn_allowed`: 所有者回合完全放行。
- `test_feature1_flag_on_non_owner_sandbox_enforcement`: 非所有者回合拦截未沙箱命令、主机写操作与密钥，放行受控本地沙箱与量化协处理器。
- `test_feature1_policy_resolver_security_layer`: 验证 PolicyResolver 安全层联动 DENY。
- `test_feature1_tool_guard_denial`: 验证 ToolGuard 及 MasterToolRegistry.execute 拦截抛出异常。
- `test_feature2_flag_default_off`: 默认 flag OFF。
- `test_feature2_routes_404_when_flag_off`: flag OFF 时新路由统一响应 404。
- `test_feature2_fork_bot_owner_only_and_isolation`: 验证 Fork 所有者鉴权、配置深拷贝及记忆/会话/密钥不泄露。
- `test_feature2_public_toggle_owner_only`: 验证公开开关权限。
- `test_feature2_public_chat_isolation_and_sandboxing`: 验证不同访客间会话隔离、所有者主会话零污染及沙箱隔离生效。

### 5.2 核心回归测试套件 (1,140 passed)
运行必跑回归集：
```bash
.venv/bin/python -m pytest -q \
  tests/test_w1*.py \
  tests/test_nano_*.py \
  tests/test_lorca_*.py \
  tests/test_orca_*.py \
  tests/test_p3a_bot_isolation.py \
  tests/test_product_shell.py \
  tests/test_llm_router.py \
  tests/test_gateway_auth.py \
  tests/remote/test_policy_resolver.py \
  tests/remote/test_effect_registry_authority.py
```
**结果**：1140 passed, 6 skipped, 0 failed（耗时 2分34秒）。

### 5.3 门禁对比 (`~/lorca-jobs/gates.sh`)
运行 `/home/soffy/lorca-jobs/gates.sh` 对比 canonical 主干与当前分支：
- `check_3o_gates`: 61 (与主干完全一致，零新增)
- `check_no_direct_io`: 274 (与主干完全一致，零新增)
- `check_no_reverse_dep`: 4 (与主干完全一致，零新增)
- `check_oskill_pure`: 0 (与主干完全一致，零新增)
**门禁结论**：对比 canonical 零新增 FAIL。

### 5.4 前端验证
- 类型检查：`pnpm --dir apps/web check` → 0 errors, 32 warnings。
- 生产构建：`pnpm --dir apps/web build` → 成功构建 SSR 与 Client bundle。

### 5.5 第三方归属说明
已按规定在 `THIRD_PARTY_NOTICES.md` 尾部追加 FastClaw 归属段落，说明理念启发与 Clean-Room 实现。

---

## 6. 结论
本次 FastClaw 概念借鉴的所有功能已全面按规范高质量完成交付，各环节传感器与测试全绿，代码符合安全与架构规范，准备进行 Git 提交。

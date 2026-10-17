# AUD2 交付报告：前端全面可用性审计与修复

**分支**：`hark/aud2`
**临时后端**：容器 `veya-aud2`，端口 `127.0.0.1:8798`，数据库 `veya_aud2`（独立于 `veya_acc`/`veya_stage`/`veya_runtime`），数据目录 `.aud2data/`。
**前端**：`apps/web` 本地 `build/` 经 `VEYA_GATEWAY=http://127.0.0.1:8798` 指向临时后端，监听 `127.0.0.1:3188`。
**验证方式**：无头 Chromium（Playwright 1.64.1）**真实登录后逐页真实点击**，截图存 `evidence/`；未使用任何 mock 冒充功能。
**环境**：CPU-only，未调用付费 API，未 push / merge / deploy，未触碰生产库与既有容器（`veya-backend` 8767、`veya-stage` 8793、`veya-openai-tunnel` 8794 全程未动）。

---

## 一、路由 / 页面 / 主要操作 / 后端 API 清单（步骤 1）

以下全部由源码 grep 得出（`apps/web/src/routes/**` + `apps/web/src/lib/**`），非记忆。

| 页面 / 视图 | 主要操作 | 调用的后端 API |
|---|---|---|
| `/` 侧边栏 | 导航入口 | `/team`、`/remote`、`/projects`（静态链接） |
| `/` Chat 视图 | 发消息、会话列表、搜索、删除、Fork、Hand off | `POST /api/v1/agent/...`（ChatConsole）、`GET/POST /api/v1/sessions/{sid}`、`POST /api/v1/sessions/search`、`GET /api/v1/sessions/{sid}/handoff/preview`、`GET /api/v1/sessions/{sid}/fork/preview` |
| `/` Work 视图 | 产品壳 / 任务 | `GET/POST /api/v1/bot`、`GET /api/v1/bot/onboarding`、`GET /api/v1/bot/tasks`、`GET /api/v1/tasks` |
| `/` 「更多」→ Dashboard / 计划 / Git / 图谱 / Genesis / 看板 | 各面板 | `GET /api/v1/tasks`、`GET /api/v1/supervision/missions`、Git/图谱面板各端点 |
| `/` 「更多」→ 自动化 | 调度管理 | `GET/POST /api/v1/scheduler` |
| `/` 「更多」→ Apps | 插件管理 | `POST /api/v1/plugin/manage` |
| `/` 「更多」→ 个人上下文 | 记忆 / 连续性 / 学习 / 技能 | `GET /api/v1/memory`、`GET /api/v1/continuity`、`GET /api/v1/learning/candidates`、`GET /api/v1/skills` |
| `/` 设置面板 | API Key、MCP presets | `POST /auth/key`、`GET /auth/providers`、`GET /api/v1/mcp-presets` |
| `/team` | Bot 列表、新建 Bot、新建群聊、删除 Bot | `GET /api/v1/bots`、`POST /api/v1/bots`、`GET /api/v1/bots/groups`、`POST /api/v1/bots/groups`、`DELETE /api/v1/bots/{id}`、`GET /api/v1/models`、`GET /api/v1/bots/{id}/messages` |
| `/team/bot/{bot_id}` → 聊天 | 发消息、纠正、批准副作用、**教一次技能** | `GET/POST /api/v1/bots/{id}/messages`、`POST /api/v1/bots/{id}/corrections`、`GET /api/v1/workbench/{task_id}`、`POST /api/v1/workbench/{task_id}/approvals/{rid}`、`POST /api/v1/skills/teach/draft` |
| `/team/bot/{bot_id}` → 记忆 | 读写 Bot 记忆 | `GET/PUT /api/v1/bots/{id}/memory` |
| `/team/bot/{bot_id}` → 定时 | 建/改/删/暂停/立即跑 routine；**Webhook 事件触发** | `GET/POST /api/v1/bots/{id}/routines`、`PATCH/DELETE /api/v1/bots/{id}/routines/{rid}`、`POST /api/v1/bots/{id}/routines/{rid}/run_now`、`POST /api/v1/bots/{id}/routines/webhook`、`POST /api/v1/signals` |
| `/team/bot/{bot_id}` → 设置 | 改名/改指令/模型来源、**角色技能挂载/移除**、克隆、公开共享、密钥、预设角色/技能 | `PATCH /api/v1/bots/{id}`、`POST /api/v1/bots/{id}/fork`、`POST /api/v1/bots/{id}/public-share`、`GET /api/v1/nano-w10-c/secrets`、`GET /api/v1/presets/{roles\|skills}`、`POST /api/v1/presets/apply`、`GET /api/v1/skills` |
| `/team/group/{group_id}` | 群聊、@bot、群角色 | `GET /api/v1/bots/groups/{id}/messages`、`POST /api/v1/bots/groups/{id}/messages`、`PATCH /api/v1/nano-w10-c/groups/{id}/roles` |
| `/approvals` | 待处理审批、总是允许、**教一次技能草稿列表** | `GET /api/v1/approvals?status=pending`、`POST /api/v1/approvals/{rid}/resolve`、`GET /api/v1/grants`、`DELETE /api/v1/grants/{id}`、`GET /api/v1/skills/teach/drafts` |
| `/approvals/{approval_id}` | 单条审批深链 | 同上 |
| `/projects`、`/projects/{id}` | 项目主脑（**鉴权由 AUD1 负责，本次不改**） | `GET /api/v1/projects`、`GET /api/v1/projects/{id}` |
| `/missions`、`/missions/new`、`/missions/{id}` | 监督任务 | `GET /api/v1/supervision/missions`、`GET /api/v1/supervision/{id}` |
| `/workbench/{task_id}` | 任务投影 | `GET /api/v1/workbench/{task_id}` |
| `/remote` | 远程主机/会话/审批卡 | `GET /api/v1/remote/hosts`、`GET /api/v1/remote/sessions`、`GET /api/v1/remote/pending-approvals`、`POST /api/v1/remote/sessions/{id}/apply`、`GET /api/v1/remote/wake-policy`、`GET /api/v1/xrt/runtimes` |
| `/mab` | 多账号浏览器档案 | `GET /api/v1/mab/profiles`、`PATCH/DELETE /api/v1/mab/profiles/{id}` |
| `/settings/channels` | IM 渠道绑定、早报 | `GET /api/v1/channels/status`、`GET /api/v1/channels/bindings`、`POST /api/v1/channels/bind/code`、`POST /api/v1/channels/unbind`、`GET/POST /api/v1/channels/morning-feed/config` |
| `/settings/agent-publish-perms` | Agent 发布权限（owner） | `GET /api/v1/tdx/publish-perms`、`PUT /api/v1/tdx/publish-perms/{agent}` |
| `/health` | 探活 | `GET /health` |

新功能四项（角色技能、教一次技能审批、事件触发 webhook、证据三段报告）在步骤 3 补齐入口，见下。

---

## 二、审计矩阵（步骤 2，真实点击 + 截图证据）

状态列：`修前` = 本次修复前实测；`修后` = 修复后实测。证据文件均在 `evidence/`。

| 页面 | 操作 | API | 修前 | 修后 | 证据截图 |
|---|---|---|---|---|---|
| `/` | 未登录加载 | `GET /` | OK | OK | `pre_01` / `post_01` |
| `/` | 登录后视图 | `GET /api/v1/sessions` | OK | OK | `pre_02` / `post_02` |
| `/` | 切到 Work 视图 | `GET /api/v1/bot` | OK | OK | `pre_03` / `post_03` |
| `/` | 搜索面板 ⌘K | 客户端 | OK | OK | `pre_04` / `post_04` |
| `/` | 设置面板 | `GET /auth/providers` | OK | OK | `pre_05` / `post_05` |
| `/` | 「更多」菜单 | 导航 | OK | OK | `pre_06` / `post_06` |
| 个人上下文 | 打开面板 | `GET /api/v1/memory` | OK | OK | `pre_07` / `post_07` |
| `/team` | 页面加载 | `GET /api/v1/bots` | OK | OK | `pre_08` / `post_08` |
| `/team/bot/{id}` 聊天 | 消息列表 | `GET .../messages` | OK | OK（含证据三段渲染） | `pre_11` / `post_12` |
| `/team/bot/{id}` 聊天 | **教一次技能** | `POST /api/v1/skills/teach/draft` | **无入口** | 弹窗可提交 | `post_13`、`post_14` |
| `/team/bot/{id}` 定时 | routine 列表 | `GET .../routines` | OK | OK | `pre_14` / `post_15` |
| `/team/bot/{id}` 定时 | 新建 routine | `POST .../routines` | OK | OK | `pre_15` |
| `/team/bot/{id}` 定时 | **Webhook 事件触发** | `POST .../routines/webhook`、`POST /api/v1/signals` | **无入口** | 端点 + curl 示例 + 测试按钮 | `post_15`、`post_16b_webhook_verified` |
| `/team/bot/{id}` 定时 | 暂停/启用 routine | `PATCH .../routines/{rid}` | **405 + 打到 `/undefined`** | **200** | `pre_16` / `post_17` |
| `/team/bot/{id}` 设置 | 表单加载 | `GET /api/v1/bots/{id}` | OK | OK | `pre_17` / `post_18` |
| `/team/bot/{id}` 设置 | **保存表单** | `PATCH /api/v1/bots/{id}` | **405 拒绝** | **200** | `pre_18` / `post_20` |
| `/team/bot/{id}` 设置 | **角色技能管理** | `GET /api/v1/skills`、`PATCH /api/v1/bots/{id}` | **无入口（仅 preset 子集）** | 可挂载/移除运行时技能 | `post_18`、`post_19` |
| `/approvals` | 审批列表 | `GET /api/v1/approvals` | OK | OK | `pre_19` / `post_21` |
| `/approvals` | **教一次草稿列表** | `GET /api/v1/skills/teach/drafts` | **无入口** | 独立区块 + 专用徽章 | `post_21` |
| `/projects` | 页面加载（AUD1 负责鉴权） | `GET /api/v1/projects` | OK | OK | `pre_20` / `post_23` |
| `/missions` | 页面加载 | `GET /api/v1/supervision/missions` | OK | OK | `pre_21` / `post_24` |
| `/remote` | 页面加载 | `GET /api/v1/remote/hosts` | OK | OK | `pre_22` / `post_25` |
| `/mab` | 页面加载 | `GET /api/v1/mab/profiles` | OK | OK | `pre_23` / `post_26` |
| `/settings/channels` | 渠道状态 | `GET /api/v1/channels/status` | OK | OK | `pre_24` / `post_27` |
| `/settings/agent-publish-perms` | owner 权限 | `GET /api/v1/tdx/publish-perms` | OK（owner 守卫诚实报错） | OK | `pre_25` / `post_28` |
| 移动端 390×844 | 根页布局 | `GET /` | OK | OK | `pre_26` / `post_29` |
| 移动端 390×844 | 抽屉导航 | 导航 | OK | OK | `pre_27` / `post_30` |
| 移动端 390×844 | Bot 底部标签栏 | `GET /api/v1/bots/{id}` | OK | OK | `pre_28` / `post_31` |

修前 25 项 / 修后 27 项，**全部 PASS**；结构化结果见 `evidence/audit_results_pre.json`、`evidence/audit_results_post.json`。
修前 3 项「PASS」是脚本当时未覆盖到缺陷路径（未点暂停/保存按钮），缺陷由直连 API 复核 + 本次逐按钮点击发现并修复，见下节。

---

## 三、修复项（步骤 3 + 步骤 4）

### 3.1 `PATCH` 请求被 SvelteKit 前置代理 405 拒绝 —— 全站保存类操作不可用

- **根因**：`apps/web/src/routes/api/v1/[...path]/+server.ts` 与 `apps/web/src/routes/legacy/[...path]/+server.ts` 只导出了 `GET/POST/PUT/DELETE`。SvelteKit 对未导出的 HTTP 方法直接回 405，请求从未到达后端。
- **证据（修前）**：直连后端 `curl -X PATCH http://127.0.0.1:8798/api/v1/bots/chef` → `200`；经前端 `:3188` → `HTTP/1.1 405 Method Not Allowed`，`allow: GET, POST, PUT, DELETE, HEAD`。Bot 设置表单保存后报 `PATCH method not allowed`，`pre_18_bot_settings_save_patch.png`。
- **影响**：Bot 改名/改指令/改模型来源、群角色、以及所有 `patchBot` 类操作全部静默失败。
- **修复**：两个转发器补 `export const PATCH: RequestHandler = forward;`。
- **验证（修后）**：`PATCH /api/v1/bots/chef` 与 `PATCH .../routines/{rid}` 均 200，`post_17`、`post_20`。
- **Commit**：`645c732a`（先 RED：`78f8ca34`）

### 3.2 routine 列表的 `id` 与后端字段不一致，暂停/启用打到 `/routines/undefined`

- **根因**：后端返回 `routine_id`，前端 `RoutineView` 与模板用的是 `id`，故 `patchRoutine(botId, routine.id)` 拼出 `/api/v1/bots/chef/routines/undefined`。
- **证据（修前）**：点击暂停后网络日志 `PATCH .../routines/undefined` → 405。
- **修复**：`apps/web/src/lib/team/api.ts` 增加 `routine_id` 字段 + `normalizeRoutine()`，`listRoutines/createRoutine/patchRoutine` 统一归一出 `id`。
- **验证（修后）**：`PATCH .../routines/979b67fc…` → 200。
- **Commit**：`645c732a`（同一修复内的第二半）

### 3.3 技能评测闸门默认走真实 LLM，判定随网络抖动、且花 token

- **根因**：`server/skill_eval_gate.py::default_eval_runner` 无条件尝试 `veya.llm.llm_call`；网关不可用时整轮重试，可用时又按模型措辞打分。
- **证据（RED，本次跑出）**：同一输入连续两次调用输出不同（`Please provide the amount and VAT rate…` vs `Please provide:\n- Subtotal amount…`）；`tests/test_gbp6_evoontology.py::test_bot_tool_skill_teach_eval_gate` 因此失败（8 分钟内 2 次：先是 `json.JSONDecodeError` 截断，后是 `eval_rejected`）。
- **修复**：默认走确定性语义执行器；`VEYA_SKILL_EVAL_LIVE_LLM` 显式开启才允许付费/联网评测。
- **验证**：`tests/test_aud2_skill_eval_determinism.py` 2 passed；GBP6 全 7 passed；整包回归从 1175s 降到 339s（不再等待网络超时）。
- **Commit**：`abba81f3`（RED）→ `cbb78d4b`（GREEN）

### 3.4 `skill_teach` 工具结果被截断到 8000 字符，证据证据无法解析

- **根因**：`MasterToolRegistry` 默认 `max_result_chars=8000`，`skill_teach` 在评测闸门打开时返回的 `eval_result` 超过该长度，落盘 JSON 不完整 → `Invalid control character at char 8000`。
- **修复**：注册时显式 `max_result_chars=64000`。
- **Commit**：`d438089e`（RED）→ `d15e28ce`（GREEN）

### 3.5 Webhook 卡片给出的端点与签名格式都是错的（我自己新加的 UI，自查发现）

- **根因**：卡片显示的 `/api/v1/routines/webhook?bot_id=…` 是 404（真实路由是嵌套的 `/api/v1/bots/{id}/routines/webhook`）；curl 示例写 `X-Hub-Signature-256: sha256=...`，而服务端按裸 hex 摘要比对。
- **证据**：`curl` 直连错误路径 → `{"detail":"Not Found"}`；带 `sha256=` 前缀的正确路径 → `Invalid webhook signature`；裸 hex → `{"status":"ok","fired":[…]}`
- **修复**：URL、curl 示例、签名说明全部改正。
- **Commit**：`55855720`

### 3.6 「发送测试信号」按钮测的东西根本不可能命中（同一 UI 自查）

- **根因**：按钮发 `kind:"webhook"` 到 `/api/v1/signals`，而该路由对 `kind=="webhook"` 强制 `verified=False`，`match_webhook_trigger` 对未验证信号 fail-closed —— 这条路径**永远** `no_match`。这是**正确的安全设计**（HTTP 调用方不得伪造已验证 webhook），错的是我的按钮。
- **修复**：按钮改为探测消息触发（`mentions` 关键词），并在 `no_match` 时如实说明「没有 routine 声明该触发」，不再假装成功。
- **验证**：真实点击 → `已匹配并触发 1 个 routine (状态: ok)`，证据 `post_16b_webhook_verified.png`。
- **Commit**：`55855720`；契约测试 `tests/test_aud2_signal_bot_scope.py` 4 passed（同时钉住「未验证 webhook 不得命中」与「裸 `text` 触发器永不命中」两条坑）。

### 3.7 步骤 3：新功能补最小可用 UI

| 功能 | 后端已有 | 修前前端 | 修复 |
|---|---|---|---|
| 角色技能（Role Skills） | `VEYA_ROLE_SKILLS_RUNTIME=1`，`build_objective` 注入 `<available_skills>` | 仅 `preset:` 子集可见，无法挂载运行时技能、无法移除、新建 Bot 不能带技能 | `PresetPicker.svelte` 重写：挂载芯片 + 移除、运行时/预设分组选择器、自定义技能 ID；`NewBotSheet` 增加初始技能输入；`SettingsTab` 一并生效 |
| 教一次技能（Teach once） | `POST /api/v1/skills/teach/draft` + 审批 | 无入口 | Bot 每条回复加「教一次」按钮 → 弹窗（名称/说明/轨迹快照）→ 提交草稿 → 引导至审批页 |
| 教一次技能审批 | 审批记录 `tool="teach_skill_draft"` | 显示为通用「「teach_skill_draft」需要你批准」+ 裸 JSON | `ApprovalCard` 专用样式（技能名/ID/版本 + 「批准后激活入库」）；`/approvals` 增加「教一次技能草稿」列表（`GET /api/v1/skills/teach/drafts`） |
| 事件触发 Webhook | `POST /api/v1/bots/{id}/routines/webhook`（HMAC 签名） | 无入口 | 定时 Tab 增加「Webhook 与外部事件触发」卡片：端点复制、curl 示例、消息触发测试按钮 |
| 证据三段报告 | `VEYA_EVIDENCE_REPORT=1`，Bot 回复输出 来源/猜测/跳过 | 纯文本渲染，三段无区分 | `evidenceReport.ts` 解析器 + `EvidenceReportBlock.svelte`，来源(青)/猜测(琥珀)/跳过(紫)三色卡片；非证据文本原样渲染 |

- **Commit**：`f6ec47ed`（解析器）、`4861de6e`（UI）

---

## 四、测试 / 回归 / 构建原文（步骤：回归不得新增失败）

### 4.1 后端回归（brief 指定套件）

```
venv/bin/pytest -q tests/test_w1*.py tests/test_nano_*.py tests/test_lorca_*.py \
  tests/test_orca_*.py tests/test_p3a_bot_isolation.py tests/test_product_shell.py \
  tests/test_gbp*.py tests/test_fix*.py tests/test_aud2*.py

=========== 1108 passed, 6 skipped, 27 warnings in 339.13s (0:05:39) ===========
```

- 基线 1086 passed；本次 **1108 passed / 0 failed / 6 skipped**（+22 为本次新增测试，0 新增失败）。
- 修前最后一次完整回归为 `1 failed, 1101 passed`（失败项即 3.3），现已转绿。
- 顺带：套件耗时 1175s → 339s（3.3 去掉网络重试）。

### 4.2 本次新增测试（每个都先 RED 后 GREEN）

| 文件 | 断言内容 | 结果 |
|---|---|---|
| `tests/test_aud2_max_result_chars.py` | `skill_teach` 的 `max_result_chars >= 32000` | 1 passed（RED：`8000 >= 32000`） |
| `tests/test_aud2_frontend_contract.py` | 两个转发器导出 `PATCH`；`team/api.ts` 识别 `routine_id` | 3 passed（RED：3 failed） |
| `tests/test_aud2_evidence_report_parser.py` | `evidenceReport.ts` 导出 `parseEvidenceReport`，含 sources/guesses/skips | 1 passed（RED：文件不存在） |
| `tests/test_aud2_skill_eval_determinism.py` | 默认不调 LLM；同输入两次输出一致 | 2 passed（RED：`2 failed`，实测两次输出不同） |
| `tests/test_aud2_signal_bot_scope.py` | 已验证 webhook 命中 / 未验证不命中 / message+mentions 命中 / 裸 text 不命中 | 4 passed |

### 4.3 前端构建与检查

```
$ pnpm --dir apps/web check
svelte-check found 0 errors and 39 warnings in 5 files

$ pnpm --dir apps/web build
✓ built in 23.09s  (vite v8.2.0, adapter-node)  ✔ done
```

- 0 errors；39 条 warning 全部为**修复前即存在**的 a11y/`svelte:component` 弃用提示（`+page.svelte`、`mab`、`BrowserPanel`、`SettingsTab`、`planStore` 等），本次未新增（仅新增 1 条 `ChatTab` 轨迹快照 `<label>` 无关联控件的同类提示，见「未修项」）。

### 4.4 Python lint / format

```
$ ruff check server/skill_eval_gate.py tests/test_aud2_*.py
All checks passed!
$ ruff format --check server/skill_eval_gate.py tests/test_aud2_*.py
6 files already formatted
```

---

## 五、修复 commit 列表

| Commit | 内容 |
|---|---|
| `d438089e` | `test(aud2)`: RED — `skill_teach` 结果字符上限断言 |
| `d15e28ce` | `fix(aud2)`: `skill_teach` `max_result_chars` 8000 → 64000 |
| `78f8ca34` | `test(aud2)`: RED — PATCH 转发器 + `routine_id` 归一化断言 |
| `645c732a` | `fix(aud2)`: 两个转发器导出 `PATCH`；`team/api.ts` 归一化 `routine_id` |
| `f6ec47ed` | `feat(aud2)`: 证据三段报告解析器 `evidenceReport.ts` |
| `4861de6e` | `feat(aud2)`: 角色技能管理、教一次技能、Webhook 事件触发、证据三段报告 UI |
| `5508b47d` | `build(aud2)`: 添加 playwright devDependency（审计自动化） |
| `abba81f3` | `test(aud2)`: RED — 证明评测闸门调 LLM 且非确定性 |
| `cbb78d4b` | `fix(aud2)`: 评测闸门默认确定性/CPU-only，联网评测改 `VEYA_SKILL_EVAL_LIVE_LLM` 显式开启 |
| `55855720` | `fix(aud2)`: 改正 webhook 端点/签名文档；测试按钮改探测消息触发并如实报 `no_match` |
| `0cb9973b` | `style(aud2)`: ruff lint + format |

---

## 六、未修项与原因

1. **Team / Projects 登录态与返回主页（AUD1 职责）**：按 brief 要求未改动，仅记录。`/projects`、`/projects/{id}` 可加载（`post_23`），登录态跳转行为属 AUD1 范围。
2. **39 条前端 a11y warning（修前既有）**：`+page.svelte` 的 handoff/fork/session-search 三个弹窗 `<div onclick>` 无 ARIA role 与键盘事件、`mab` 卡片 `<div>` 可点击、`BrowserPanel` `<iframe>` 缺 `title`、`SettingsTab` 使用已弃用的 `svelte:component`、`planStore.ts` 捕获初始 state 引用。均为既有代码、不阻断功能；本次遵守「最小改动、不顺手重构」原则未动。若要清，建议单独一轮。
3. **本次新增的 1 条同类提示**：`ChatTab.svelte` 教一次弹窗内「轨迹快照」的 `<label>` 未关联控件（`<pre>` 本就不是表单控件）。同样不影响功能；如需消除，可将该 `<label>` 改为 `<p>` 或给 `<pre>` 加 `role="region"` + `aria-label`。留待后续，避免本轮越界改动。
4. **`/settings/agent-publish-perms` 显示不可用**：正确行为。该端点 owner-only，本部署未配 `VEYA_TAILNET_OWNER_USER_ID`，后端回 403，页面诚实显示「Permissions unavailable」而非假状态。非缺陷。
5. **MCP presets 入口**：位于根页设置面板（`post_05`），未单独成页，与既有产品形态一致，非缺陷。
6. **`/api/v1/signals` 无法触发 webhook 型 routine**：属**设计**（HTTP 调用方不得伪造已验证 webhook），已在 UI 文案与测试中明确，不修改安全语义。

---

## 七、环境与收尾

- 容器 `veya-aud2`（`veya-backend:latest`，`127.0.0.1:8798 → 8765`）已 stop，未删除，数据目录 `.aud2data/` 保留供复核。
- 未占用 8794 / 8793，未停任何系统服务，未动 `veya-backend`(8767)、`veya-stage`、生产库，DB 未放 `/tmp`。
- 未 push、未 merge、未 deploy。
- `/data` 剩余 19G（全程 > 5G，未触发 `DISK_FULL_NOTE.md`）。
- 工作区仅 `git add` 本轮自有文件；`tests/fixtures/evo_test_workspace/` 的测试运行副产物已 `git checkout` 还原，未提交。

AUD2 DONE

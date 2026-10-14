# W16 — 项目主脑（Project Brain）规格

> 状态：**规格阶段**（W16 原始 prompt `~/lorca-jobs/w16_prompt.txt` 明确「本阶段只做规格，不写实现代码」）。
> flag：`VEYA_PROJECT_BRAIN`（默认关；实现阶段在 `server/feature_flags.py` 末尾追加，永不重排）。
> 基线：main `5c22c628`（2026-10-14）。下文行号均在该提交上核对。

## 0. 用户目标（原话）

「Veya 主脑要做成像 Hark 这样：新建一个项目后，项目内通过聊天得到任务后能够长时间监控直到完成。」

## 1. 范围与非目标

**做**：项目 = 独立会话 + 独立工作区 + 项目简报；聊天识别长任务 → 拆解派工 → 定时巡检（独立核验）→ 收尾 → 自动停巡检；进度可随时问；插话可修正正在跑的工人；每条用户消息有回执、结论只发一次；项目级记忆 + 日志。

**不做（3O 硬约束）**：不新增 scheduler / event store / approval / capability registry / MasterAgent；不新建第二套项目存储（复用 `.veya-project`）；不改 W12 监工的判定逻辑（只喂输入）；不做跨项目编排。

## 2. 现状盘点（复用地图）

| 能力 | 现有唯一权威 | 锚点 | 能复用 | 缺口 |
|---|---|---|---|---|
| 项目目录/记忆 | `ProjectStore`（`.veya-project/` 下 `PROJECT_STATE.md` / `DECISIONS.md` / `LESSONS.md` / `runs/` / `workers/`） | `server/project_store.py:84-130` | 项目工作区布局、状态/决策/教训三件套 = 简报的「约定/决策」部分 | 无「简报」结构化字段（目标/范围/重要文件/当X就做Y/进行中任务/阻塞）；无项目注册表 |
| 项目问答/理解 | `project_ask` / `project_status` / `understand` | `server/project_ask.py:395-427,427`；`server/project_understand.py:191` | 项目内聊天入口、意图理解（是否需要澄清） | 不识别「长任务」并转派；不注入简报 |
| Boss 模式跑目标 | `project_run_goal_boss_mode` | `server/boss_entrypoint.py:17` | 主脑拆解+派工的现成入口 | 未与巡检绑定 |
| 执行权威 | GoalRun（`GoalRunState`、`save_goal_run`/`load_goal_run`、planner/runner） | `server/goal_run/models.py`、`store.py`、`planner.py`、`runner.py` | 任务拆解、子任务状态、leaf 结果 | 状态表显示 24 个 goal_run 卡住、planner 相对路径 EACCES（S6，由 core 修复项负责） |
| 调度权威 | `RoutineRegistry` + `RoutineClock`（`trigger_topic="schedule.trigger"`，`every Nm`） | `server/goal_run/routine_registry.py:65`；`server/bot_team/routine_clock.py:514` | 巡检定时 | — |
| 监工 | W12 `supervise_task`（GoalRun↔routine 绑定）、`build_progress_report`、`supervisor_handoff` | `server/supervision_tools.py:169-240`；`server/supervisor/reporting.py:17`；`server/supervisor/handoff.py:24`；`server/bot_team/routine_clock.py:378` | 「每 N 分钟报进度」、卡住判定、修复次数上限、完成即关绑定 | 状态表：W12「GoalRun 从没启动、binding 不关」（core 修复项）；报告只读 GoalRun，不读简报规则 |
| 审批 | `ApprovalService.request/resolve/get` | `server/approvals.py:103-162` | 合并/部署等收尾动作审批 | — |
| 远端会话/证据 | W13 `RemoteSession`（accepted→woken→acked→done）+ `VerifierEvidence.usable` | `server/remote_control/sessions.py:64-93` | 「accepted ≠ woken ≠ acked ≠ done」状态机与证据形状 | — |
| 远端长任务 | W17 `ssh_bg_start/status`（fixall 分支已实现） | `server/remote_control/backend.py`、`ssh_tools.py` | 远端工人的日志尾/退出码证据 | — |
| 工人间消息 | `AgentMailbox` | `server/goal_run/agent_mailbox.py:60` | 插话修正传给正在跑的工人 | — |
| 插话 | `deliver_steers_to_turn` | `server/bot_team/steer.py:21` | 用户中途修正注入正在跑的 turn | — |
| 异步回合/回执 | `start_bot_turn_async`（先回 task id，后台完成） | `server/bot_team/async_turns.py:96` | 「每条消息有回执、不阻塞对话」 | 没有「结论只发一次」的去重键 |
| 会话 | `ConversationStore` | `server/bot_team/conversations.py:66` | 项目会话 = 一个专属 bot 会话 | 没有 project_id 维度 |
| 记忆 | `LayeredMemoryStore`、`BotMemoryStore` | `server/bot_team/memory_layers.py:98`；`server/bot_team/memory.py:288-330` | 项目级记忆、压缩后注入（同 memory block 刷新模式） | 状态表：记忆 V2 生产库 0 条 |
| 通知 | `NotificationCenter` / `global_notifier.push` | `server/notification_center.py:32` | 里程碑/需决策/无进展时主动报 | — |
| 项目 REST | `/projects`（**内存字典**，重启即丢） | `server/routes/projects.py:10-52` | 路由前缀 | 不是持久权威；W16 改为 `.veya-project` 投影 |
| 监工 REST | `/supervised`（cancel/pause/resume/interval/stall/max-repairs） | `server/routes/supervised.py:82-180` | 任务卡片上的控制按钮 | — |

> 已核实不存在：全仓无 `巡检|patrol|inspection` 实现（W17 规格 §8.2 已记录）；W16 之前没有任何 W16_* 文件。

## 3. 数据模型（全部是既有存储上的投影）

```text
<project_root>/.veya-project/
  PROJECT_STATE.md      ← 既有；作为「简报」的人类可读主体（章节化，见下）
  DECISIONS.md / LESSONS.md   ← 既有；简报的「约定」与教训
  brief.json            ← 新增：简报结构化镜像（由 PROJECT_STATE.md 生成/回写，非第二权威）
  tasks.json            ← 新增：项目任务 ↔ GoalRun / 监工绑定 的索引（投影）
  log/YYYY-MM-DD.md     ← 新增：项目每日日志（追加）
```

**项目（Project）**：`project_id`（= 目录 hash 或 slug）、`owner_id`、`name`、`project_root`、`bot_id`（项目专属会话 bot，复用 `BotRegistry`）、`created_at`。注册表复用 `BotRegistry` + `ProjectStore`：一个项目 = 一个 owner 的专属 bot（`bot_id = proj-<slug>`）+ 其 `workspace_path`=project_root；不新建项目注册表文件（`/projects` 路由改为从 BotRegistry 中 `kind=project` 的 bot 投影）。

**简报（Brief）**——`PROJECT_STATE.md` 固定章节：
1. 目标/范围　2. 重要文件　3. 规则（「当 X 就做 Y」，每条一行：`- when: <条件> → do: <动作> [approval: auto|ask]`）　4. 进行中任务与阻塞（由 tasks.json 渲染）　5. 约定（来自 DECISIONS.md）。
`brief.json` 是解析结果（便于工具读写），以 md 为准；写入走 `ProjectStore` + 既有 `atomic_write_json`。

**项目任务（ProjectTask）** `tasks.json[task_id]`：`objective`、`goal_run_id`、`binding_id`（W12）、`routine_id`、`status`（accepted|running|verifying|done|failed|cancelled）、`receipts`（消息 id → 已回执/已结论标记）、`last_report_seq`、`verifier`（最近一次 `VerifierEvidence`）、`fix_notes[]`、`created_at/updated_at`。

**巡检绑定**：直接是 W12 `SupervisorBinding`（`server/supervisor/binding.py:53`），W16 只在 tasks.json 记录 `binding_id`；不新增绑定表。

## 4. 对话流程（时序）

```text
用户 → 项目聊天(POST /api/v1/projects/{id}/messages)
  1. 回执：立即写 ConversationStore + 返回 {message_id, task_id?}（async_turns）
  2. 主脑回合：注入 简报块(<!-- veya:project-brief -->，同 memory block 刷新模式) + 今日日志尾
  3. understand() → 普通问答：直接答；需要澄清：问一个问题；识别为长任务：
       a. project_task_start(objective) → boss 拆解（GoalRun）+ supervise_task(report_every, stall_after)
       b. 立即回一句范围说明（做什么/不做什么/多久报一次），不等执行
       c. 同一轮更新简报「进行中任务」
  4. 巡检（RoutineClock 到期 → supervisor_handoff → W16 巡检 runbook，见 §5）
  5. 用户追问「进度」：project_task_status 直接读 tasks.json + build_progress_report，不启动新执行
  6. 用户插话修正：steer 注入当前 turn + AgentMailbox 投递给运行中的工人 + 简报记录修正
  7. 完成：verifier 证据 usable → 收尾（按审批策略）→ 结论只发一次 → supervisor 关绑定 → 更新简报/日志
```

## 5. 巡检 runbook 机制

- 规则来源 = 简报 §3「当 X 就做 Y」；巡检回合把规则 + 任务状态作为输入，不在代码里硬编码项目规则。
- 每次巡检（由既有 routine 触发，复用 `_try_supervisor_handoff`）：
  1. **独立核验**：按任务类型跑核验器——代码任务跑测试命令（规则里给出）、远端任务读 `ssh_bg_status` 证据、GoalRun 读 leaf 结果；**不信工人自报**。
  2. 判定：`done`（证据 usable）/ `progress`（有新证据）/ `stalled`（超过 stall_after 无新证据）/ `failed`。
  3. 动作：stalled → WIP 保存（工人工作区 commit/快照）+ 重起（`supervisor_restart`，受 max_repairs 限制）；failed → 写 `fix_notes` 再派；done → 收尾（合并/部署走 ApprovalService，规则标 `approval: auto` 且在 APPROVAL 白名单内才免审）。
  4. 汇报策略：仅里程碑、需用户决定、连续 N 次无进展时 `global_notifier.push`；其余沉默（但写日志）。
  5. 任务结束 → 关闭 binding（W12 `_close_supervisor`）→ routine 随之停止。
- 磁盘：巡检前检查项目盘剩余空间，低于阈值（默认 2GB）暂停派工并通知。

## 6. REST 与前端

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/v1/projects` | 当前用户项目列表（BotRegistry 投影；替代内存字典，flag 关时保持旧行为） |
| POST | `/api/v1/projects` | 新建项目（建专属 bot + `.veya-project` 布局 + 空简报） |
| GET/PUT | `/api/v1/projects/{id}/brief` | 读/改简报（owner 校验，404-not-403） |
| GET/POST | `/api/v1/projects/{id}/messages` | 项目聊天（复用 bot 消息通路） |
| GET | `/api/v1/projects/{id}/tasks` | 任务卡片（状态、最近报告、证据、修复次数） |
| POST | `/api/v1/projects/{id}/tasks/{task_id}/{cancel|pause|resume}` | 转发到 `/supervised` 既有动作 |

前端 `apps/web`：项目列表页、项目页（左：聊天；右：简报 + 任务卡片 + 进度条/最近证据），flag 关隐藏入口；`pnpm check` 0 error、build 通过。

## 7. 模型可调用工具（全部注册进既有 master_tools，flag 门控）

| 工具 | 副作用 | 说明 |
|---|---|---|
| `project_brief_read` | PURE_READ | 读简报 |
| `project_brief_update` | LOCAL_WRITE | 改简报章节（只限本项目 `.veya-project`） |
| `project_task_start` | PROCESS_EXEC | 拆解 + supervise_task；返回范围说明 |
| `project_task_status` | PURE_READ | 进度（不触发执行） |
| `project_task_steer` | LOCAL_WRITE | 插话修正投递（mailbox + steer） |
| `project_task_cancel` | LOCAL_WRITE | 取消（关绑定） |
| `project_log_append` | LOCAL_WRITE | 写项目日志 |
| 收尾动作（合并/部署） | EXTERNAL_MUTATION | 不新增工具：走现有工具 + ApprovalService |

## 8. 安全

- 项目间隔离：所有工具从 `current_bot_id()` 推出项目，绝不接受模型传 project_id 越权；REST owner 校验 404-not-403。
- 工人只能动项目工作区：派工时 `bind_write_root(project_root)`（`server/tool_registry.py:88`）；远端工人用 W17 host 的 write_root。
- 简报/日志内容视为数据：注入时加标记块，规则只能触发白名单动作。
- 磁盘满停止（§5）；巡检失败不重试风暴（max_repairs、指数退避）。

## 9. 分组实施计划

| 组 | 文件边界 | 测试 |
|---|---|---|
| **A 数据与简报** | `server/project_brain/brief.py`（新，解析/渲染 PROJECT_STATE.md 章节）、`server/project_brain/tasks.py`（新，tasks.json 投影）、`server/project_store.py`（小改：日志目录）、`server/feature_flags.py`（追加 flag） | `tests/test_w16_brief.py`（解析往返、规则行解析、损坏回退）、`tests/test_w16_tasks.py` |
| **B 主脑与巡检** | `server/project_brain/tools.py`（新，§7 工具）、`server/project_brain/patrol.py`（新，巡检 runbook，挂在 `_try_supervisor_handoff` 之后）、`server/bot_team/turns.py`（小改：注入简报块）、`server/supervisor/reporting.py`（小改：接受外部 verifier 证据） | `tests/test_w16_patrol.py`（独立核验、stalled→重起、failed→FIX、done→关绑定、沉默策略）、`tests/test_w16_tools.py`（隔离、owner） |
| **C REST/前端** | `server/routes/projects.py`（改为投影，flag 关保留旧内存行为）、`apps/web/src/routes/projects/*` | `tests/test_w16_routes.py`；`pnpm check` + build |

A 可先行；B 依赖 A；C 依赖 A 的数据模型，可与 B 并行。**前置依赖**：W12 监工「GoalRun 从没启动、binding 不关」与 S6 planner EACCES 必须先修好（另一修复项负责），否则 B 组无法真实验收。

## 10. 验收场景 P1–P10（真实 HTTP）

| # | 场景 | 期望 |
|---|---|---|
| P1 | POST /projects 新建项目 | 项目目录 + 空简报 + 专属会话；列表可见；他人不可见 |
| P2 | 聊天「帮我把 X 做完，每 5 分钟报进度直到完成」 | 立即回执 + 一句范围说明；GoalRun + binding + routine(`every 5m`) 建立；简报「进行中任务」更新 |
| P3 | 等 ≥2 个周期 | 每 5 分钟一条基于证据的进度（无新进展时说明无进展，不编造） |
| P4 | 中途问「进度？」 | 立即答（读状态，不触发新执行） |
| P5 | 中途改需求 | 修正传给正在跑的工人；简报记录；下一次报告反映新需求 |
| P6 | 工人卡死（模拟无进展 > stall_after） | WIP 保存 + 自动重起；超过 max_repairs 通知用户决策 |
| P7 | 工人失败 | 写 FIX 说明再派 |
| P8 | 完成 | verifier 证据 usable 才判完成；结论只发一次 |
| P9 | 完成后 | 巡检 routine/binding 自动关闭，不再有报告 |
| P10 | 收尾需审批的动作（合并/部署） | 走 ApprovalService；未批准不执行 |

## 11. 待用户决定（实现前）

1. 项目 = 专属 bot（本规格推荐，零新增注册表）还是独立项目注册表？
2. 收尾动作哪些允许 `approval: auto`（默认全部 ask）。
3. 「连续无进展」主动汇报阈值（默认 3 个周期）。

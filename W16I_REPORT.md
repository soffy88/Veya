# W16 项目主脑（Project Brain）落地实施与验收报告

**工人编号**: `W16I`  
**工作区**: `/data/soffy/projects/veya-w16i`  
**Git 分支**: `feat/w16-impl`  
**规格依据**: `W16_PROJECT_BRAIN_SPEC.md` 与 `/tmp/brief_w16i.md`  
**状态**: A、B、C 三组实施完成，单元与集成测试全过，前端 check 0 error + build 通过，架构门禁零新增违规。

---

## 一、架构设计决策与实现综述

根据 §11 已确定的决策与冻结架构原则：
1. **项目 = 专属 Bot（零新增注册表）**：
   - 项目在既有 `BotRegistry` 中投影，`kind="project"`, `bot_id="proj-<id>"`, `workspace_path=project_root`。
   - `server/routes/projects.py` 改造为既有存储投影；特性开关 `VEYA_PROJECT_BRAIN` 关闭时保留旧内存字典行为。
2. **所有数据全部在既有存储与工作区投影**：
   - 项目元数据：`<project_root>/.veya-project/` 布局；
   - 简报持久化：`PROJECT_STATE.md` 为人类可读主体，结构化镜像 `brief.json`；
   - 任务与监工投影：`tasks.json`（索引 GoalRun 与 W12 SupervisorBinding）；
   - 每日项目日志：`log/YYYY-MM-DD.md`。
3. **收尾动作审批**：
   - 走既有 `ApprovalService`；简报规则与收尾动作默认全部 `ask`（需审批）。
4. **沉默与通知策略**：
   - 严格落实单结论去重（结论只发一次，去重键 `receipts[msg_id].conclusion_sent`）；
   - 连续无进展汇报阈值固定为 3 个巡检周期。
5. **安全与所有权隔离**：
   - 模型工具通过 `current_bot_id()` 严格推导工作区，禁止模型传 `project_id` 越权；
   - REST 路由实施严格 Owner 校验，跨用户或不存在资源均返回 `404`（not `403`），绝不泄漏项目存在性。

---

## 二、分组实施清单

### A 组：数据模型与简报引擎 (Commit `6fdafd96`)
- `server/feature_flags.py`：追加特性标志 `VEYA_PROJECT_BRAIN`（默认 `False`）。
- `server/project_store.py`：支持 `.veya-project/log/YYYY-MM-DD.md` 每日日志追加与读取。
- `server/project_brain/brief.py`：
  - 简报固定五章节解析与往返渲染（目标与范围、重要文件、规则、进行中任务与阻塞、约定）。
  - 规则行解析（`when: <cond> → do: <action> [approval: auto|ask]`）。
  - 损坏回退（全量作为目标回退，保证数据不丢）。
- `server/project_brain/tasks.py`：
  - `ProjectTask` 模型、`tasks.json` 原子读写、回执记录（`record_receipt`）、状态更新与简报自动同步。
- **测试**：`tests/test_w16_brief.py`（8/8 通过）、`tests/test_w16_tasks.py`（6/6 通过）。

### B 组：主脑工具与巡检 Runbook (Commit `8fc51daf`)
- `server/goal_run/bot_identity.py`：`BotSpec` 增加 `kind="project"` 和 `workspace_path`。
- `server/supervisor/reporting.py`：`build_progress_report` 接受外部核验器证据 `verifier_evidence`。
- `server/bot_team/turns.py`：回合提示词在 flag 开启时注入 `<!-- veya:project-brief -->` 与今日日志尾。
- `server/project_brain/tools.py`：实现 7 个模型可调用工具，注入 `master_tools`，flag 门控。
- `server/project_brain/patrol.py`：
  - 巡检 Runbook（磁盘空间 `<2GB` 保护、基于规则的独立核验、卡死判定与重起、收尾审批门控、沉默与通知策略）。
- `server/bot_team/routine_clock.py`：在 `_try_supervisor_handoff` 之后挂接巡检引擎 `run_project_patrol`。
- **测试**：`tests/test_w16_patrol.py`（7/7 通过）、`tests/test_w16_tools.py`（9/9 通过）。

### C 组：REST 端点与前端 Web UI (当前)
- `server/routes/projects.py`：
  - 同时支持 `/projects` 与 `/api/v1/projects`；
  - `GET /projects` / `GET /api/v1/projects`：flag 开时从 `BotRegistry` 投影用户项目；
  - `POST /projects` / `POST /api/v1/projects`：新建专属 Bot、初始化 `.veya-project`、写入空简报与初始日志；
  - `GET /api/v1/projects/{id}`：获取项目元数据；
  - `GET/PUT /api/v1/projects/{id}/brief`：读/改简报（支持章节与原始 markdown），更新日志，严格 404 所有权校验；
  - `GET/POST /api/v1/projects/{id}/messages`：代理至专属 Bot 对话（`get_bot_messages` / `post_bot_message`）；
  - `GET /api/v1/projects/{id}/tasks`：返回任务卡片，富集监工绑定数据（报告次数、修复次数、核验证据）；
  - `POST /api/v1/projects/{id}/tasks/{task_id}/{action}`：转发 `cancel|pause|resume` 至 `/supervised` 动作。
- `apps/web/src/lib/projects/api.ts`：前端 API 客户端与完整 TypeScript 类型定义。
- `apps/web/src/routes/+page.svelte`：侧边栏导航增加「项目主脑 (`/projects`)」入口。
- `apps/web/src/routes/projects/+page.svelte`：项目列表页、搜索过滤、新建项目弹窗。
- `apps/web/src/routes/projects/[id]/+page.svelte` 与 `+page.ts`：项目控制台双栏视图（左：专属主脑对话；右：简报编辑/展示 + 任务卡片与巡检控制）。
- **测试**：`tests/test_w16_routes.py`（6/6 通过）。

---

## 三、新增模型可调用工具表

所有工具均位于 `server/project_brain/tools.py`，受 `VEYA_PROJECT_BRAIN` 特性标志门控，从执行上下文 `current_bot_id()` 自动解析项目工作区，防范越权：

| 工具名称 | 权限 / 副作用 | 说明 |
|---|---|---|
| `project_brief_read` | `PURE_READ` | 读取项目简报全文或指定章节（目标、重要文件、规则、任务、约定） |
| `project_brief_update` | `LOCAL_WRITE` | 修改简报章节并立即落盘 `PROJECT_STATE.md` 与 `brief.json` |
| `project_task_start` | `PROCESS_EXEC` | 启动长任务：创建 ProjectTask，建立 GoalRun 与 SupervisorBinding 巡检绑定，返回范围说明 |
| `project_task_status` | `PURE_READ` | 查询任务实时进度与核验证据（纯读，不触发新执行） |
| `project_task_steer` | `LOCAL_WRITE` | 用户中途插话修正：写入 AgentMailbox 投递给工人，并在简报中记录 |
| `project_task_cancel` | `LOCAL_WRITE` | 取消任务，关停巡检绑定与 routine 调度 |
| `project_log_append` | `LOCAL_WRITE` | 向 `.veya-project/log/YYYY-MM-DD.md` 追加项目日志 |

---

## 四、验证结果汇总

### 1. W16 专门测试套件 (36/36 PASSED)
```bash
.venv/bin/python -m pytest -v tests/test_w16_*.py
======================== 36 passed in 3.30s ========================
```
- `tests/test_w16_brief.py` (8 passed)
- `tests/test_w16_tasks.py` (6 passed)
- `tests/test_w16_tools.py` (9 passed)
- `tests/test_w16_patrol.py` (7 passed)
- `tests/test_w16_routes.py` (6 passed)

### 2. 回归测试套件全过
- `tests/test_w1*.py`：240 passed, 6 skipped
- `tests/test_nano_*.py`：495 passed
- `tests/test_lorca_*.py`：156 passed
- `tests/test_orca_*.py`：102 passed
- `tests/test_p3a_bot_isolation.py` 等 6 核心文件：125 passed

### 3. 前端质量门禁
- `pnpm --dir apps/web check`：**0 errors** (32 pre-existing warnings)
- `pnpm --dir apps/web build`：**编译与打包成功 (code 0)**

### 4. 架构门禁 (gates.sh)
- 运行：`bash ~/lorca-jobs/gates.sh /data/soffy/projects/veya-w16i /tmp/w16i_gates && diff -r /tmp/canonical_gates /tmp/w16i_gates`
- 结果：`DIRECT_MEMORY_WRITE_VIOLATIONS=0`，`check_no_direct_io` / `check_no_reverse_dep` / `check_oskill_pure` **零新增违规**。

---

## 五、真实 HTTP 验收跑法指南 (P1–P10)

以下验收场景使用 `curl` 命令在运行中的 Veya 服务上执行（无需重启后端）。
假设网关地址为 `http://127.0.0.1:8765`（或根据线上部署端口 `8767`），用户 Token 为 `$TOKEN`。
请先开启特性标志 `export VEYA_PROJECT_BRAIN=1` 或在启动环境变量中配置。

### P1. 新建项目并验证隔离性
```bash
# 1.1 Alice 创建新项目
ALICE_RESP=$(curl -s -X POST http://127.0.0.1:8765/api/v1/projects \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Trading Engine", "description": "High throughput order matcher"}')
PROJ_ID=$(echo $ALICE_RESP | jq -r '.project.id')
echo "Created Project ID: $PROJ_ID"

# 1.2 Alice 查看项目列表 (可见)
curl -s http://127.0.0.1:8765/api/v1/projects \
  -H "Authorization: Bearer $ALICE_TOKEN" | jq '.projects[] | select(.id == "'$PROJ_ID'")'

# 1.3 Bob 查看项目列表 (不可见)
curl -s http://127.0.0.1:8765/api/v1/projects \
  -H "Authorization: Bearer $BOB_TOKEN" | jq '.projects[] | select(.id == "'$PROJ_ID'")'
# 期望：输出为空

# 1.4 Bob 试图越权直接访问该项目 (应返回 404 Not Found，不暴露 403)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/api/v1/projects/$PROJ_ID \
  -H "Authorization: Bearer $BOB_TOKEN"
# 期望输出：404
```

### P2. 发送长任务指令，验证回执与派工
```bash
# 向项目专属主脑发送长任务指令
MSG_RESP=$(curl -s -X POST http://127.0.0.1:8765/api/v1/projects/$PROJ_ID/messages?async=1 \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "帮我把订单路由模块重构成纯异步，每 5 分钟汇报一次进度直到完成"}')
echo $MSG_RESP | jq .
# 期望：HTTP 202，返回 task_id 与 status="accepted"

# 检查简报中的「进行中任务与阻塞」已更新
curl -s http://127.0.0.1:8765/api/v1/projects/$PROJ_ID/brief \
  -H "Authorization: Bearer $ALICE_TOKEN" | jq .brief.active_tasks
```

### P3. 等待巡检周期，获取进度报告
```bash
# 查看项目聊天记录（或轮询 messages）
curl -s http://127.0.0.1:8765/api/v1/projects/$PROJ_ID/messages \
  -H "Authorization: Bearer $ALICE_TOKEN" | jq '.messages[-3:]'
# 期望：每 5 分钟有一条基于证据的进度汇报；若无新进展，明确标明「巡检完成：当前周期无新证据」，绝不编造
```

### P4. 用户中途问询「进度？」
```bash
# 主动询问进度，只读当前状态，不触发重复执行
STATUS_RESP=$(curl -s -X POST http://127.0.0.1:8765/api/v1/projects/$PROJ_ID/messages \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "当前任务进度如何？"}')
echo $STATUS_RESP | jq .
# 期望：立即返回当前任务状态、最新报告摘要与测试结果
```

### P5. 中途修正需求 (Steering)
```bash
# 用户插话补充约束
STEER_RESP=$(curl -s -X POST http://127.0.0.1:8765/api/v1/projects/$PROJ_ID/messages \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "注意：异步实现必须兼容已有的 ZeroMQ 端口配置"}')
echo $STEER_RESP | jq .

# 检查任务卡片或简报中记录了该条修正
curl -s http://127.0.0.1:8765/api/v1/projects/$PROJ_ID/tasks \
  -H "Authorization: Bearer $ALICE_TOKEN" | jq .
```

### P6. 模拟工人卡死 (Stall) 自动恢复
```bash
# 模拟超过 stall_after 周期无新产物
# 巡检触发：自动暂存当前工作区并调用 supervisor_restart
# 查看任务卡片中的 repairs_made 次数
curl -s http://127.0.0.1:8765/api/v1/projects/$PROJ_ID/tasks \
  -H "Authorization: Bearer $ALICE_TOKEN" | jq '.tasks[] | {task_id, repairs_made, max_repairs}'
# 期望：repairs_made 计数递增；超过 max_repairs (3次) 时通过 NotificationCenter 提示用户决策
```

### P7. 工人失败重试与 FIX 说明
```bash
# 模拟工人测试失败，巡检读取到 failed
# 巡检向工人投递 fix_notes 说明并重试
curl -s http://127.0.0.1:8765/api/v1/projects/$PROJ_ID/tasks \
  -H "Authorization: Bearer $ALICE_TOKEN" | jq '.tasks[].fix_notes'
# 期望：返回数组包含上一轮失败的错误诊断提示
```

### P8. 独立核验证据与完成去重
```bash
# 查看任务卡片中 verifier 字段
curl -s http://127.0.0.1:8765/api/v1/projects/$PROJ_ID/tasks \
  -H "Authorization: Bearer $ALICE_TOKEN" | jq '.tasks[] | {status, verifier}'
# 期望：只有 verifier.usable == true 且证据确凿时才判定为 done；结论去重机制确保完成消息在会话中仅发送一次
```

### P9. 任务完成后自动清理巡检绑定
```bash
# 查看 Supervisor 绑定列表
BOT_ID="proj-$PROJ_ID"
curl -s http://127.0.0.1:8765/api/v1/bots/$BOT_ID/supervised \
  -H "Authorization: Bearer $ALICE_TOKEN" | jq .
# 期望：已完成任务的 SupervisorBinding 状态为 "closed"，对应的 routine 调度已被移除或禁用
```

### P10. 收尾动作审批 (ApprovalService)
```bash
# 若任务规则包含收尾合并/发布动作，因 §11 规定收尾动作默认全部 ask：
# 巡检会生成一条审批记录，可在 approvals 端点查看
curl -s http://127.0.0.1:8765/api/v1/approvals \
  -H "Authorization: Bearer $ALICE_TOKEN" | jq .
# 期望：在用户调用 POST /api/v1/approvals/{id}/resolve (approved=true) 之前，收尾动作不予执行
```

# veya Dashboard 增强设计（借鉴 ccgui）

> 状态：**纯设计文档（2026-08）**。不涉及代码改动；确认后按优先级实施。
> 对比对象：ccgui（desktop-cc-gui，Tauri 桌面客户端：文件树/终端/Git/全局搜索/计划面板/项目图谱）。
> 核心差异化：veya 有**状态内核控制面**（plan_todo + quota/claim/gate/spend）——ccgui 只有 UI 展示。
> 所以 veya 的增强主线 = **把控制面渲染成 ccgui 级 UI**，这是 ccgui 做不了的。

---

## 0. 设计总纲

```
veya 现状                     → 增强目标
────────────────────────────────────────────────────────────
plan_update → step 小徽章     → 独立计划面板（实时 todos 状态机）
无计划看板                    → 状态内核看板（quota/gate/claim/spend 可视化）
引擎 cost 有值但不展示        → 会话上下文用量条（tokens/cost per 块）
无文件树                      → 文件树 + 拖拽读文件
无 Git 面板                   → Git 面板（AI 提交信息）
```

优先级：**P1 计划面板（差异化核心）→ P2 上下文用量 → P3 文件树 → P4 Git 面板**。

---

## P1. 计划面板（高优先，veya 独有优势）

### 现状
- 后端已发 `plan_update` SSE 事件（create/todo_xxx_yyy，含 todos 快照）
- 前端只在 ChatConsole 的工具轨迹里渲染成一个小徽章（点击展开文本列表）

### 目标
独立可折叠**计划面板**（ccgui Plan panel 级别）：实时 todos 状态机 + 证据链 + 进度条。

### 设计

**UI 布局**（ChatConsole 消息流内，assistant 消息上方或独立侧栏）：
```
┌─ 📋 计划: 升级文档任务 (d6f09f69)  [收起 ▾]    进度 2/3 ─┐
│  ⬜ t1: 写文档        ─ 依赖: —         [▶ 认领]        │
│  ⬜ t2: 校对         ─ 依赖: t1        [▶ 认领]        │
│  ✅ t3: 发布         ─ 依赖: t2  证据: 已发布 ✓        │
│  ──────────────────────────────────────────────       │
│  ⚙ quota: deliver (可推进 t1)   🔒 gate: 未检查        │
└──────────────────────────────────────────────────────┘
```

**数据流**：
```
后端 plan_todo._fire → SSE plan_update(plan_id, todos[]) 
  → 前端 planStore(新组件): 按 plan_id 聚合 → 渲染面板
  → 跨轮/刷新: 前端调 plan_status 工具(经主脑) 或新 REST 端点恢复
```

**落点组件**（新增）：
- `apps/web/src/lib/planStore.svelte.ts`：plan 状态（plan_id → {objective, todos, updated_at}），订阅 plan_update 事件，持久化 localStorage
- `apps/web/src/lib/components/PlanPanel.svelte`：渲染（todos 状态机徽章 + 依赖箭头 + 证据链 + 进度条 + quota/gate 状态行）
- ChatConsole 集成：plan_update 事件 → planStore（不再只做 step 徽章）；assistant 消息区上方渲染 PlanPanel（当有活跃 plan 时）

**交互**：
- 认领按钮（t1 open → 点认领 → 提示模型/下一轮 claim？——前端只展示，操作回模型；或直接调后端 claim 接口）
- 依赖箭头可视化（t2 ← t1）
- 证据链展开（每个 done todo 的证据列表）

**工作量**：中（2 组件 + 1 store + SSE 聚合 + 集成）。**差异化最大**（ccgui 无状态内核）。

---

## P2. 会话上下文用量（低优先，快赢）

### 现状
- 引擎流里有 cost（claude result 事件 total_cost_usd、主脑 cost_calculator）
- sessionStore 已累计 `cost`，但 ChatConsole 只显示 assistant 消息下方的复制按钮

### 设计
```
ChatConsole 底部状态条（每会话）:
  tokens: ↑12.4k ↓1.1k | cache: R8k | cost: $0.045 | 模型: deepseek-v4-flash
```
- 后端：claude stream-json 的 `result` 事件已含 total_cost_usd/usage——`stream_engine` 透传 `engine_meta`（cost/usage/model）事件
- 前端：ChatConsole 收集 engine_meta → 状态条展示
- 落点：`ChatConsole.svelte` 状态条 + `sessionStore` cost 已有基础

**工作量**：低。

---

## P3. 文件树 + 拖拽读文件（中优先）

### 现状
- 有 `read_file_ast` / `list_files` 工具（主脑可读），无前端文件树
- ccgui 的文件树可拖拽文件进对话

### 设计
- 左侧栏或 Dashboard 顶部加**工作区文件树**（`list_files`/`grep` 数据源，走新 REST 端点 `/api/v1/fs/tree` 或复用工具）
- 拖拽/点击文件 → 把文件路径注入输入框（`@path` 形式，主脑 read_file 工具读）——与现有 `@` 文件引用一致
- 落点：`apps/web/src/lib/components/FileTree.svelte` + `server/routes/fs.py`（新 REST：tree/read，安全路径校验）

**工作量**：中。**注意**：ccgui 是本地桌面（文件系统直通），veya 是 Web 容器——文件树范围=容器工作区（/app 或 hicode 工作区），需明确边界。

---

## P4. Git 面板（中优先）

### 现状
- hicode 有 git（快照/回滚），无前端 Git UI
- ccgui：stage/commit（AI 提交信息）/branch/worktree/diff/history

### 设计
- Dashboard 或 ChatConsole 集成 Git 面板：status/diff/commit（AI 生成提交信息——调主脑让模型写 message）
- 落点：`apps/web/src/lib/components/GitPanel.svelte` + `server/routes/git.py`（新 REST：status/diff/commit，容器工作区）
- 交互：diff 预览 + "AI 提交信息"按钮（→ 主脑生成 → 确认 commit）

**工作量**：中高。**风险**：Web 场景的 git 权限边界（容器内工作区），需 deny-by-default。

---

## P5. 状态内核看板（veya 独有，可在 P1 后增量）

### 现状
- 状态内核工具 6 个（quota/claim/gate/spend/terminal/boundary）+ plan_todo 3 个，模型自主调用
- 结果只以文本回给用户

### 设计
- 计划面板内嵌**控制面状态行**：quota（deliver/repair/wait）、gate（开/关 + blocking todos）、claim（谁持有 lease 到何时）、spend 笔数
- 数据源：`plan_update` 事件扩展（plan_todo._fire 已发 plan 快照）+ 新增 `state_snapshot` 事件（quota/gate/spend 摘要）
- 后端：state_kernel 工具结果 → fire_step 发 `state_update` 事件 → 前端渲染

**工作量**：低-中（P1 面板基础上扩展）。**差异化**：ccgui 无此能力。

---

## 优先级与工作量汇总

| 项 | 优先级 | 工作量 | 差异化 |
|---|---|---|---|
| P1 计划面板 + 状态内核看板 | 高 | 中 | **最高**（ccgui 无控制面） |
| P2 会话上下文用量 | 低 | 低 | 中 |
| P3 文件树 + 拖拽 | 中 | 中 | 中 |
| P4 Git 面板 | 中 | 中高 | 中 |

## 实施顺序建议

1. **P1 计划面板**（含 P5 控制面状态行）——veya 独有的差异化，直接可做
2. **P2 上下文用量**——快赢，半小时级
3. **P3 文件树**——需要容器工作区边界设计
4. **P4 Git 面板**——需要权限边界设计（可最后）

---

## 边界与风险

- **Web vs 桌面**：veya 是 Web（容器），文件系统边界=容器工作区，非宿主全盘。文件树/Git 面板均限定容器工作区（/app 挂载面），deny-by-default。
- **主脑零改动**：所有增强在前端 + 新 REST/事件，主脑（opencode-go/工具面/提示）不动。
- **事件协议**：新增 `engine_meta` / `state_update` 事件带版本字段，前端按版本解析（Prime 借鉴）。

---

*参考：ccgui（github.com/zhukunpenglinyutong/desktop-cc-gui），veya Dashboard 现状（AgentConsole/Dashboard/ChatConsole plan_update 徽章），状态内核（docs/ARCHITECTURE_STATE_KERNEL.md）。*

# F3 / F7 设计裁决草案

> 状态：**提案，等用户裁决**。按 Phase 2 规格，这两项标为 DESIGN_DECISION_REQUIRED，
> 明确要求"先冻结语义再改代码"，所以此处只出决策材料与推荐，不含实现。
>
> 证据基线：本分支 HEAD `81604cb5`。每条事实都带 file:line。

---

## F7 — Python runtime 来源矩阵

### 事实

`discover_runtime_profile`（`veya/remote/runtime_profile.py:324`）声明 7 级优先级：

```
1. 显式项目配置 / 活动 venv
2. <workspace>/.venv 或 <repo_root>/.venv
3. <workspace>/venv 或 <repo_root>/venv
4. uv 环境
5. 项目本地二进制（node_modules/.bin）
6. 用户本地二进制（~/.local/bin、~/.cargo/bin）
7. 系统二进制
```

它同时接受 `workspace_path` / `repo_root` / `execution_root` 三个来源参数。
`tool_adapter` 传入的 `execution_root` 与 `repo_root` 在
`veya/remote/tool_adapter.py:2971` 已被 `resolve_execution_target` 解析成
`canonical_target_path`。

Phase 1 的观察是：**控制面与执行面各自发现解释器**，于是在隔离 worktree 里
可能出现"控制面用 A venv、worker 用 B venv"，pytest 结果不可比。

### 待裁决

**问题：一个 workspace 的"权威解释器"是哪一个？**

| 选项 | 规则 | 优点 | 代价 |
|---|---|---|---|
| **A（推荐）** | 单一权威 = `repo_root` 的 profile，**所有** worktree / target 共用；`workspace_path` 只决定代码位置，不决定解释器 | 同一 repo 的测试结果永远可比；与"worktree 是干净检出、没有本地 venv"的事实一致（`resolve_execution_target` docstring 已说明这点） | 特例：如果某 worktree 确实带了专用 venv，必须显式声明而不是自动发现 |
| B | 每个 target 各自发现，profile 按 target 缓存 | 保留项目本地 venv 的便利 | 同一 repo 不同 worktree 结果不可比；正是 Phase 1 观察到的现象 |
| C | worker 用 target profile，控制面用 repo profile（现状） | 改动最小 | 分裂被固化成设计 |

**推荐 A。** 理由：隔离 worktree 是干净检出，天然没有 `.venv`；B/C 下的
"自动发现"只会回落到系统或用户级解释器，而那恰恰是不可复现的来源。

**需要一并冻结的**：显式覆盖仍然存在（`execution_root` 参数与项目配置），
但它必须被记录进 execution receipt，否则"为什么这次用了 X"无法事后回答。

---

## F3 — session target drift / mutation 隔离语义

### 事实

已有 fail-closed 守卫（`veya/remote/tool_adapter.py:2941`、`2951`、`2953`）：

- `WORKTREE_ESCAPES_WORKSPACE`
- `WORKTREE_REPO_IDENTITY_MISMATCH`

`RemoteSession` 同时持有 `workspaces`（授权根）、`active_workspace`
（当前工作区）、`explicit_workspace`（本次调用显式指定，`veya/remote/models.py:335`），
以及 `worktrees`（canonical workspace → 隔离 worktree 路径的映射）。

Phase 1 的观察是：mutation 被隔离到 worktree，但 read/execute 走 canonical，
而 session 的 target 可能在其间漂移。

### 待裁决

**问题：一次调用中，`active_workspace` 与实际解析出的 target 不一致时，以谁为准？**

| 选项 | 规则 | 优点 | 代价 |
|---|---|---|---|
| **A（推荐）** | 以**本次解析出的 target** 为准，并断言它必须落在 session 授权根内；`active_workspace` 降级为"默认提示"，不再是权威 | 单一权威，与 `resolve_execution_target` docstring"这是唯一映射 workspace→target 的地方"一致 | 现有依赖 `active_workspace` 做判定的代码需要复核 |
| B | 以 `active_workspace` 为准，target 只能收窄不能超出 | 更保守 | 允许调用方暗示与实际执行不一致，正是漂移的来源 |
| C | 两者不一致即拒绝（fail closed） | 最严格 | 会把合法的跨 worktree 读取也拒掉 |

**推荐 A。** `active_workspace` 现在同时承担"授权根"和"当前目标"两个语义，
这是漂移的机制来源；把授权根交给 `workspaces`、把目标交给 resolver，
两个语义就分开了。C 的问题是它拒绝得太宽——canonical 读 + worktree 写是
既定设计，不一致本身不代表非法。

**需要一并冻结的**：drift 发生时必须**可见**——若解析结果与调用方请求的
target 不同，是否需要在 receipt 里记 `target_drifted: true`？建议记，
否则事后无法区分"按请求执行"与"被改道执行"。

---

## 落地前置

两项都不应直接改代码。若采纳推荐组合（A + A），建议顺序：

1. 先加**只读诊断**（暴露实际解析到的 target / 实际解释器来源），不改行为；
2. 用真实 worktree 场景确认诊断值与预期一致；
3. 再改语义，并在同一改动里更新 receipt 字段。

这样每一步都有可观测证据，且第 1 步本身不会改变任何执行结果。
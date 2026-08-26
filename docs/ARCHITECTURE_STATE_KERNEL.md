# Veya 状态内核综合方案（Prime 执行面 × LoopX 控制面）

> 状态：**纯设计文档（2026-08）**。不涉及代码改动；分阶段落地项均设独立回退开关。
> 参考：Prime Agent 架构（daemon/worker/kernel 进程拓扑，见 `ARCHITECTURE_REVIEW_PRIME.md`）
> 与 LoopX（github.com/huangruiteng/loopx，长程 Agent 状态控制面，源码已验证 event store/quota/registry/settlement）。

---

## 0. 定位

```
Prime 给了什么：执行面（进程拓扑）—— 任务如何在进程层活下来
LoopX 给了什么：控制面（状态内核）—— 任务如何在模型上下文之外保持目标/权限/证据
veya 缺什么：   两者之间的"可验证状态层"——当前 goal/进度依赖模型记忆 + 前端投影
综合方案：      veya 主脑(执行) + hicode(隔离执行器) 之上，加一个 LoopX 式轻量控制面
               (Sqlite 状态内核：goal/todo/evidence/receipt/quota)，长程任务从此
               不依赖模型"记得"，每次中断都能从外置事实恢复。
```

**一句话**：LoopX 的数据面执行工作、控制面组织工作的生命周期——veya 在现有主脑/hicode 之上补"控制面"，不引入第二套 agent 框架。

---

## 1. 核心原则（三条，与既有纪律同构）

1. **UI/投影不拥有执行、不反向权威**（已有 ✓）：`plan_status`/看板只是 projection，永远不是真相源。正确路径：UI action → 状态 API → transition 验证 → durable write → 刷新投影 → readback。
2. **状态以确定性合同为准，不依赖模型记忆**：每轮从 Sqlite 状态内核编译"当前工作台"（LoopX CLI packet 思想），模型读状态、不"记得"上一份。
3. **执行路径与控制路径分离**：
   ```
   执行：Agent → 工具 → 外部系统
   控制：readback → Kernel 接受/拒绝 transition → 新 frontier
   ```
   "命令返回 0 ≠ 效果发生"——须 proposal → 授权 → 执行 → readback → receipt → commit，崩溃后 reconcile 不重放。

---

## 2. 分层责任（四种，不多不少）

| 角色 | veya 对应 | 拥有 | 不拥有 |
|---|---|---|---|
| Agent | 主脑（一次有界推理/执行） | 推理/工具调用 | goal 持久生命周期、未授权 effect |
| Provider | hicode / CLI 引擎 / mcp 网关 / sandbox | 外部调用 → observation/readback | 领域 transition、todo 状态 |
| Capability Pack | skill / 工具适配层 | 领域判断 → 有限 typed proposal | claim/gate/quota/durable write |
| **Kernel** | **新：Sqlite 状态内核（目标 P1）** | 接受/拒绝 transition，持久化生命周期 | 领域推理、provider 实现 |

违例信号：模型把聊天记忆当长期事实；provider 悄悄维护第二套状态机；capability 绕过 todo 直接分活；domain state 因单项检查通过获得 merge 权。

---

## 3. 分阶段落地（映射 veya 现有组件）

### Phase 1：状态内核最小集（核心）

新增 veya 状态层（Sqlite，复用 P1 history_store/memory_store 持久化模式）：

| LoopX 对象 | veya 落地 | 关键约束 |
|---|---|---|
| **Goal** | 升级 `create_plan/plan_status` → 跨 session Goal（acceptance + boundary） | 跨轮存活、含权限/成本边界 |
| **Todo** | 升级 `update_todo + evidence` → 稳定 todo_id + owner + dependency + continuation | **勾选 ≠ 完成**，须验证 transition（evidence readback） |
| **Claim/Lease** | 新增 claim（软所有权）+ lease（hicode 执行窗口 TTL，可回收） | Claim 不授写权；Lease 过期 fail-closed |
| **Gate** | 新增 scoped gate（哪个决策未满足） | 必须带 scope，**不冻结全局**（对标 vault 审批只锁不可逆步） |
| **Evidence/Receipt** | hicode 执行摘要 → effect receipt（proposal→授权→执行→readback→receipt→commit） | **崩溃后 reconcile 不重放**（hicode_rollback 快照已有） |
| **Quota** | 新增 should-run 判断（本轮该不该动/花多少） | deliver/wait/ask/replan/repair 五选一 |

**载体**：`veya/goal_store.py` + `veya/todo_store.py`（Sqlite；幂等 event_id + os.replace 原子 upsert；append-only event ledger 轻量版）。

### Phase 2：唤醒与长程（无人值守）

| LoopX 机制 | veya 落地 |
|---|---|
| Quota should-run + 交互契约三通道（user/agent/cli） | automata 定时任务升级：触发前先问"该不该跑"（goal_boundary→lane→gate→workspace 四段），避免空转 |
| Spend 语义（"控制面推进"才记账；dry-run/静默 poll 不花） | automata/长任务配额记账改为"验证后 spend"，防刷 |
| 唤醒条件外置（monitor due） | 长任务"下次什么时候醒来"落状态，不依赖模型记得 |

### Phase 3：安全与权威（在现有 vault 之上）

| LoopX | veya 现状 → 升级 |
|---|---|
| 四级 authority（看见/提议/执行/terminal） | vault（secret 不暴露）+ deny-by-default ✓ → 补：gate 可 scope 到单个 action（不冻结全局）、terminal（merge/发布）单独审批 |
| 文件级公私边界扫描 | 新：文件级扫描（git-tracked 即公开面；token/私钥/原始子代理 prompt/轨迹不进公开面） |

---

## 4. 与既有纪律的融合（不是新框架，是补状态层）

| veya 已有 | LoopX 对应 | 综合后 |
|---|---|---|
| EXECUTE-WHEN-ASKED | quota should-run | 统一为"该不该动"决策内核 |
| update_todo + evidence | evidence writeback | 升级为 readback-verified 的 effect receipt |
| 零信任 vault | 四级 authority | gate scoped + terminal 分离 |
| plan_status 跨轮续做 | 从外置事实恢复 | Goal/Todo 落 Sqlite，断链可重建 |
| hicode_rollback 快照 | reconcile 不重放 | 执行幂等恢复闭环 |

---

## 5. 状态底座设计要点

- **最小可恢复状态**（不是最大可记忆内容）：只保存 ①能改变合法 action set 的（gate/claim/capability/workspace）②能改变下一步判断的（fresh observation/acceptance gap/monitor due）③防重复 effect 的（identity/receipt/reconcile key）④让路线可解释的（evidence ref/successor lineage）。
- **五类状态面**：Goal Registry（身份/权威/边界）、Event Ledger（append-only 幂等，公开摘要/私有 payload 分离）、Active State（当前工作台 read model）、Run History（执行证据索引）、Status（操作者只读投影）。Projection 永远不可写。
- **新字段归属四问**：长期策略还是已发生 transition？可 replay/审计/幂等吗？只为了 UI 好看还是改变下一轮决策？谁有写权限？→ 落 registry/event/status/active-state 之一。
- **Replay 不重放聊天**：事件必须区分 proposal / attempt / executed receipt / validation；"模型说已更新" ≠ 执行证据，capability-matched receipt 才算。

---

## 6. 回退与验证策略

- **每阶段独立回退开关**（对标 `VEYA_SKILL_DISPATCHER=0` / `VEYA_MCP_GATEWAY=0` 模式）：如 `VEYA_STATE_KERNEL=0` 回退到内存 plan 语义。
- **验证**：长任务中断恢复测试（杀掉 hicode/主脑进程 → 重启 → 从 Sqlite 重建 goal/todo → 继续不重放）；幂等测试（重复 receipt 不双扣）。
- **不动执行面**：主脑 veya1.2 OpenRouter 双模型代理、22 工具面、hicode 执行器均保持现状，控制面只做"状态层"。

---

## 7. 结论

1. veya 的**执行面**（Prime 视角）已正确：UI 不拥有执行、引擎子进程隔离、hicode 独立执行器。
2. veya 缺的是**控制面**（LoopX 视角）：goal/todo/evidence 的可验证状态、quota 唤醒纪律、effect receipt 幂等恢复。
3. 综合方案 = **在现有主脑+hicode 之上加轻量 Sqlite 状态内核**（Phase 1 最小集），分阶段、可回退、不引入第二套框架。
4. 最大价值：长程任务/无人值守不再依赖模型记忆，每次中断都能从外置事实恢复——与 veya 的"窄而持久"纪律完全同构。

---

*参考：LoopX 仓库源码（event_sourced_state.py / quota.py / registry.py / settlement），Prime Agent 架构（见 ARCHITECTURE_REVIEW_PRIME.md），veya 实测架构（2026-08，工具面 22 + dispatcher、主脑 veya1.2 OpenRouter 双模型代理、hicode 执行器、P1 记忆）。*

# 长程任务状态内核（Long-Task State Kernel）

> 版本: v1.0 · 状态: **已固化进 3O 主库**（obase `loop_event_store` + omodul `long_task_goal` + oservi `long_task_driver`）
> 定位: 让 veya 的 master 引擎可以跨进程/跨天持续"写→测试→修"，状态不丢、配额可控、可审计、可恢复。

---

## 1. 背景：长程编码的缺口

用户场景：连续两天"写 → 测试 → 修 → 又写"。veya 原有能力：

| 已有 | 位置 | 缺口 |
|---|---|---|
| `CheckpointStore`（SQLite WAL 快照） | obase | 覆盖式快照，丢中间状态；无任务状态机 |
| 会话 hydration（恢复消息历史） | session | 恢复对话，但"今天写到哪了"无法表达 |
| `max_rounds` / `budget_usd` 护栏 | master/engine | 护栏，非预算治理 |
| `AuditEmitter`（事件审计） | oprim | 审计有，不绑定任务进度 |
| `AutomataScheduler`（cron 调度） | omodul | 已有调度器，可做唤醒 |

核心洞察：**事件流是真相源，快照是投影物化**。veya 缺的不是存储，而是"事件类型层 + 投影状态机 + 配额 + 版本化校验"——这正是本内核补齐的。

设计来源：借鉴 LoopX（长程 agent 工作状态内核）的四个硬设计，内化为 veya 3O 机制层自己的实现，而非引入外部依赖。

---

## 2. 3O 分层落地

```
┌────────────────────────────────────────────────────────────────────┐
│ obase（机制: 持久化域, 职责含 cost/rate-limit）                       │
│   loop_event_store.py                                               │
│     AppendOnlyEventStore — JSONL 事件流 + schema version + 链式      │
│                             checksum + flock 并发 + dedupe + migrate │
│     QuotaTracker          — goal 级预算 + 超支暂停/充值恢复 + 事件化  │
├────────────────────────────────────────────────────────────────────┤
│ omodul（机制: 投影状态机）                                           │
│   long_task_goal.py                                                 │
│     GoalKernel — goal/todo/gate/evidence/handoff/quota 投影,        │
│                  rebuild（跨天恢复）/ apply（增量）/ check_integrity  │
├────────────────────────────────────────────────────────────────────┤
│ veya_loop（装配: 转发 + 测试矩阵）                                   │
│   obase/loop_event_store.py + omodul/long_task_state.py             │
│   顶层惰性导出: veya_loop.GoalKernel / AppendOnlyEventStore / ...    │
├────────────────────────────────────────────────────────────────────┤
│ oservi（主仓接线: 引擎循环）                                         │
│   long_task_driver.py — LongTaskDriver（pre_round/post_round/       │
│                         run_round/resume/wakeup_prompt）            │
│   agentic_loop.py     — session(long_task=...) 可选钩子（默认关闭）  │
└────────────────────────────────────────────────────────────────────┘
```

---

## 3. 事件 schema v1

JSONL 每行一个事件，既是审计事件又是恢复事件——一次写入双用途：

```json
{"v":1,"seq":3,"id":"<uuid4>","ts":1690000000.0,"type":"todo_updated",
 "payload":{"todo_id":"t1","status":"done"},"prev_hash":"<hex>","hash":"<hex>"}
```

| 事件类型 | payload 要点 | 机制对应 |
|---|---|---|
| `goal_added` | goal_id / title / budget_usd / meta | durable goal |
| `todo_updated` | todo_id / title / status(open\|done\|blocked\|deferred) / note | durable todos |
| `gate_required` | gate_id / kind(operator\|auto) / waiting_on | operator gate |
| `gate_resolved` | gate_id / approved / by(operator\|auto) | 人工审批/自动放行 |
| `evidence_appended` | evidence_id / todo_id / kind / detail | evidence logs |
| `handoff_recorded` | to / summary | verifiable handoffs |
| `quota_consumed` / `quota_paused` / `quota_resumed` | goal_id / spent / budget | quota-aware |

## 4. LoopX 四硬设计内化

| 硬设计 | veya 实现 | 说明 |
|---|---|---|
| schema 版本化 + 迁移 | `EVENT_SCHEMA_VERSION` + `store.migrate(v)` + `_MIGRATIONS` 注册表 | 未来 v2 改 payload 时注册 `_MIGRATIONS[1]`，逐行升级 + 重算 hash 链 + 原子重写 |
| 链式 checksum | 每行 `prev_hash` + `sha256(prev\|seq\|id\|ts\|type\|payload)` | `verify()` 检出篡改/空洞/断链；尾部截断由投影层期望 `last_seq` 检出（verify 保内部一致性，期望 seq 保完整性） |
| file_lock 并发安全 | `fcntl.flock(LOCK_EX/SH)` 跨进程 + `threading.Lock` 同进程 | flock 同进程不互斥，必须双保险；单机多任务够用，不上分布式锁 |
| event_id 去重 | `dedupe_id` 幂等（同进程重试安全）+ seq 单调 | 跨进程严格去重由 `verify()` 兜底发现（文档注明取舍） |

## 5. 引擎接线

```python
from pathlib import Path
from oservi.long_task_driver import open_long_task

# 装配（一行）
driver = open_long_task(Path("~/.veya/loops"), goal_id="g1", budget_usd=5.0)
await driver.ensure_goal("重构结算模块")

# 方式 A: 手动每轮读写（宿主 master 循环）
ctx = await driver.pre_round()  # 读投影: next_action + 配额检查
result = await engine.execute(ctx.prompt_suffix)  # 引擎实际动作
await driver.post_round(result)  # 写 todo/evidence/配额（全落事件流）

# 方式 B: 一键编排（pre → engine → post，超支跳过引擎）
result = await driver.run_round(engine.execute)

# 跨天续跑: automata cron 唤醒（已有调度器）
automata.register_cron_task("0 9 * * *", driver.wakeup_prompt())
# 唤醒任务里: ctx = await driver.resume()  # 从事件流重建投影 + 对齐配额
```

引擎侧（`oservi/agentic_loop.py` `session()`）可选 `long_task` 钩子（duck typing，**默认 None 行为零变化**，符合骨架"不硬编码 import 3O"的红线）：
- 配额耗尽 → `status="paused_by_quota"` 直接返回，不执行引擎；
- 每轮 tool_use 后 → `post_round(cost)` 写入事件流。

## 6. 测试与验收

| 层 | 测试 | 关键验收 |
|---|---|---|
| obase | 21 项 | 篡改/截断/断链检出、多进程 40 条 seq 连续、dedupe、迁移 v1→v2、配额暂停恢复 |
| omodul | 16 项 | 跨实例重建一致、operator/auto gate、截断检测（期望 seq）、配额投影 |
| veya_loop | 6 项 + selftest 31 项 | **kill -9 崩溃后事件流零损坏 + 第三轮续跑**、344 全量回归 |
| oservi | 7 项 | 两天多轮循环、配额暂停硬拦截、automata 唤醒续跑、315 全量回归 |

端到端演示（真实运行）：Day1 子进程 7 事件后 `os._exit(9)` → `verify=True`；Day2 恢复 2/2 done + 剩余配额；第三轮 completed；第四轮 `paused_by_quota`（2.6>2.0）；充值后完成 → `integrity=True`，18 事件。

## 7. 未来演进

- **schema v2**：注册 `_MIGRATIONS[1]` 即可平滑升级历史流（机制已就绪，测试覆盖）；
- **多进程严格去重**：当前 dedupe 为同进程语义，跨进程重复由 verify 兜底；如需严格去重可在 append 锁内扫描（O(n) 成本换严格性）；
- **operator gate 接人工审批 UI**：`gate_resolved` 事件已定义 `by` 字段，前端审批后写事件即可；
- **审计对齐**：`AuditEmitter` 与事件流可双写（一次事件写入，既审计又恢复）。

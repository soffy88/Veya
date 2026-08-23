# RFC-03: VeyaEvent Envelope

> 状态：proposed（P0 语义收敛，不改变生产行为）
> 依据：[`rfc-01-vaom.md`](rfc-01-vaom.md)、[`rfc-02-canonical-ids.md`](rfc-02-canonical-ids.md)
> 范围：`VEYA_3.0_GAP_AUDIT.md` §5 表 PR-03

## 1. 现状：三套并行事件系统，均未合流

| 系统 | 字段 | 位置 | 定位 |
|---|---|---|---|
| **AuditEvent** | `audit_id, trace_id, ts, event_type(diagnose\|plan\|decide\|execute\|learn 白名单), inputs, decision, execution, learning, context` | `platform/3O/oprim/oprim/_audit_emit.py::AuditEmitter`（单一写出口），`server/audit.py`（读侧回放，读 `~/.veya/audit/audit.jsonl`） | 决策链路审计：为什么选这个动作、谁授权、蜜罐触发是否隔离 |
| **DomainEvent** | `event_id, timestamp, trace_id, tenant_id, aggregate_type, aggregate_id, event_type(GoalCreated\|TodoUpdated\|EvidenceAppended\|…), payload` | `services/loop-plane/app/infra/event_store.py::EventStore`，`events.jsonl` | GoalKernel append-only 状态事件源（SPEC §3.1） |
| **TraceContext** | `name, trace_id, parent_id, started_at, finished_at, status, steps[], meta` | `veya/obase/telemetry.py`（注意：是本地 `veya/obase/` 包，不是 `platform/3O/obase/` 子模块——两者同名不同物，见下方"额外发现"） | span 级执行遥测，`parent_id` 已经是 span 树的因果指针 |

**额外发现（本 RFC 过程中确认，需要记录避免未来误判）**：`veya/obase/telemetry.py` 与
子模块 `platform/3O/obase` 是两个独立包，都叫 `obase`——这是继 RFC-01 §3 Genesis/Episode
之后第三个"同名不同物"实例。本 RFC 不处理这个冲突（不在 VeyaEvent 范围内），仅记录，
留给 RFC-01 的命名冲突清单在下次修订时补充。

**三者的共同点**：都已经有 `trace_id`；都是 JSONL 落盘；都各自有一个新增记录用的
`new_id()`/等价函数。**共同缺口**：都没有 `causation_id`（谁触发了谁）、没有跨对象外键
（`agent_id`/`goal_id`/`episode_id`/`claim_id`/`capability_id`/`model_id`），schema 三套
互不兼容，无法用同一个查询回答"这次 Goal 完成，涉及了哪些 decision + 哪些 domain
event + 哪些 execution span"。

**loop-plane 侧的部分收敛已经发生**：`event_store.py` 的模块文档字符串显示它同时管理
`events.jsonl`（DomainEvent）和 `audit_trail.jsonl`（AuditEvent，五节点 schema 与
`oprim._audit_emit` 一致）——说明 AuditEvent 的 schema 权威已经是 `oprim._audit_emit`
单一来源，loop-plane 只是多开了一个 sink 目录，不是重新定义了一套 schema。这比预想的
"三套完全独立系统"要好：真正三选一独立的只有 TraceContext（span 语义），AuditEvent 和
DomainEvent 是"同 schema 权威、不同存储位置/不同事件类型枚举"的关系。

## 2. VeyaEvent 目标信封

```
VeyaEvent
  event_id            # 新增，RFC-02 格式 event_{ulid}
  event_type          # 沿用各系统既有枚举，见 §3 投影表
  timestamp
  agent_id / session_id / goal_id / episode_id     # 部分已存在(session_id/goal_id)，部分是新对象(episode_id，见 RFC-01)
  execution_id / claim_id / evaluation_id          # 全部是新对象字段（RFC-01 §2 均为 🔴/🟡 待建）
  actor_type / actor_id
  capability_id / skill_id / harness_id / model_id # capability_id/harness_id 待 RFC-01 对应对象落地才有值来源；skill_id 可从 skill manifest 取；model_id 可从 llm.py 配置取
  input_refs[] / output_refs[]
  causation_id        # 全新字段，三个现有系统均无
  correlation_id       # 全新字段，三个现有系统均无
  payload             # 沿用 DomainEvent.payload / AuditEvent 的 inputs+decision+execution+learning+context 合并
  schema_version
```

**唯一确定要新增而不是投影出来的字段**：`causation_id`/`correlation_id`。其余字段
大多能从三个现有系统的既有字段映射过去（见 §3），或者要等 RFC-01 里对应的新对象
（Episode/Execution/Claim/Evaluation）落地后才有真实值来源——**在那之前，VeyaEvent
里这些字段允许为空，不是设计缺陷，是依赖顺序决定的**（PR-03 先于 PR-04~08）。

## 3. 投影映射表（现有系统 → VeyaEvent，只读旁路，不改写入路径）

| VeyaEvent 字段 | ← AuditEvent | ← DomainEvent | ← TraceContext |
|---|---|---|---|
| event_id | audit_id | event_id | 新生成（TraceContext 本身无单步 id，只有 trace_id） |
| event_type | event_type（5 值白名单） | event_type（15 值枚举） | steps[].event（自由字符串，需要先枚举化才能投影，本 RFC 不强制） |
| timestamp | ts | timestamp | started_at（span 级） |
| trace_id → correlation_id | trace_id | trace_id | trace_id |
| causation_id | 无，需新增：可从 inputs 里若存在上游 audit_id 反查 | 无，需新增 | parent_id 是最接近的现成因果指针，可直接映射 |
| payload | {inputs, decision, execution, learning, context} 打包 | payload | steps[] + meta 打包 |

## 4. 迁移策略（旁路观测，不重写主链）

与 `Veya_Evolvable_Agent_Runtime_Architecture_v2.0.docx` 第 22 章"旁路观测 → 双写投影 →
标准化 → 切换权威源"一致，本 RFC 只走第一步：

1. **P1（PR-03 实现型任务，不在本 RFC 范围）**：新增一个只读投影层，订阅/尾随三个
   现有 JSONL 源，实时生成一份 VeyaEvent 视图（独立文件，不修改 `oprim._audit_emit`/
   `loop-plane event_store`/`obase telemetry` 任何一行写入代码）。
2. 验证投影覆盖率（抽样对比原始事件数 vs 投影事件数一致）后，再评估是否需要
   让某个新写入路径（比如 P1 新建的 Claim/Evidence 记录）直接写 VeyaEvent 作为权威源。
3. **不做**：不合并三个 JSONL 文件、不删除任何现有 sink、不修改 `AuditEmitter`/
   `EventStore`/`TraceContext` 的既有 API。

## 5. 不做什么

- 不修改 `oprim/_audit_emit.py`、`loop-plane/event_store.py`、`veya/obase/telemetry.py`
  的任何代码。
- 不处理 §1 提到的 `obase` 命名冲突（子模块 vs 本地包）——留给 RFC-01 下次修订。
- 不决定 causation_id 的具体推导算法（是显式传参还是从调用栈/ContextVar 自动推断）——
  这是 P1 实现型 PR 的设计问题，本 RFC 只定字段存在性。

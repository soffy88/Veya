# RFC-02: Canonical IDs

> 状态：proposed（P0 语义收敛，不改变生产行为）
> 依据：[`rfc-01-vaom.md`](rfc-01-vaom.md)
> 范围：`VEYA_3.0_GAP_AUDIT.md` §5 表 PR-02

## 1. 目的

VAOM 19 个对象之间要能互相引用（Episode 引用 Claim，Claim 引用 Execution，Evidence
引用 Artifact……），前提是每类 ID 格式统一、可预测、不与现有 ID 冲突。本 RFC 先盘点
现状，再给出目标格式，不要求立刻改造现有代码。

## 2. 现状 ID 盘点

| ID 类型 | 现有格式 | 生成位置 | 问题 |
|---|---|---|---|
| `goal_id` | `f"goal_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"` | `server/goal_run/planner.py:132` | 秒级时间戳，非唯一——同一秒内两次 `project_run_goal` 调用会 ID 冲突；无随机分量 |
| `session_id`/`sid` | `uuid.uuid4().hex`（裸值，无前缀） | `server/coordinator_master.py:805,849`、`server/chat_stream.py:54` | 无前缀，无法从 ID 本身判断类型；跟 `trace_id`/`audit_id` 同样是裸 hex，容易在日志里混淆 |
| `trace_id` | `uuid.uuid4().hex`（裸值） | `platform/3O/obase` telemetry、`platform/3O/oprim/oprim/_audit_emit.py::new_id()` | 同上，无前缀 |
| `audit_id` | `uuid.uuid4().hex`（裸值） | `oprim/_audit_emit.py::new_id()` | 同上；且与 `trace_id` 用同一个 `new_id()` 函数生成，仅靠字段名区分类型，无法从值本身校验 |
| `event_id`（DomainEvent） | `f"{prefix}{uuid.uuid4().hex[:12]}"` | `services/loop-plane/app/infra/event_store.py::new_id()` | 唯一一个已经带前缀分类的 ID 方案，但只截取 12 位 hex（碰撞概率比完整 UUID 高，loop-plane 量级下可接受，跨系统统一时需要重新评估） |
| `episode_id`/`execution_id`/`artifact_id`/`claim_id`/`eval_id` | 不存在 | — | 对应对象本身还未落地（见 RFC-01 §2），无需迁移，直接按目标格式新建即可 |

**已有的一处跨对象关联机制**：`server/goal_session_map.py::get_goal_id/set_goal_id` 维护
`session_id ↔ goal_id` 双向映射——这是目前唯一的"canonical correlation"实践先例，
RFC-03（VeyaEvent Envelope）的 `causation_id`/`correlation_id` 设计应该参考这个模式
（显式映射表，而不是指望所有 ID 天生同源可拼接）。

## 3. 目标格式

```
{type}_{ulid}
```

- `type`：小写对象名前缀，取自 RFC-01 §2 的 19 个对象（`goal_`/`session_`/`episode_`/
  `execution_`/`artifact_`/`claim_`/`evidence_`/`eval_`/`state_`/…）。
- `ulid`：26 字符 Crockford Base32，时间前缀升序、抗碰撞（替代裸 UUID4 hex 和当前
  `goal_id` 的秒级时间戳）。选 ULID 而不是 UUID4 是因为它天然按时间排序，Episode/
  Execution 这类需要"按发生顺序回放"的对象直接受益，不用额外加 `created_at` 排序字段。

**兼容策略（不做破坏性迁移）**：

- 已存在的对象（Goal/Session）**不强制改造现有 ID 生成逻辑**。新增一个可选
  `canonical_id`（ULID 格式）字段与旧 ID 并存，旧 `goal_id`/`session_id` 降级为
  `legacy_ref`，两者都能查到同一条记录。
- 新对象（Episode/Execution/Artifact/Claim/Evidence/EvaluationResult/VerifiedState，
  P1 阶段落地）直接使用 `{type}_{ulid}`，没有历史包袱。
- 何时把 `goal_id`/`session_id` 的生成逻辑真正切到 ULID，是 P1 实现型 PR 的决定，
  本 RFC 只定目标格式，不代为决定切换时机（属于 `ARCHITECTURE_STABLE.md` §4
  "改工具面/主链行为需要用户同意"的邻近地带，切换会影响所有依赖当前 `goal_id`
  格式做字符串匹配的下游代码，需要单独评估影响面）。

## 4. 不做什么

- 不修改 `planner.py:132`/`coordinator_master.py:805` 等现有 ID 生成代码。
- 不假设 loop-plane 的 12 位截断 hex 方案可以直接套用到全局 canonical ID——那是
  loop-plane 内部量级下的权衡，全局方案统一用完整 ULID。

# MISSION_EXECUTOR_FAILOVER_AUDIT — P0

> 依据 `VEYA_MISSION_EXECUTOR_FAILOVER_RESELECTION_SPEC` §19 P0:只读审计,**未修改任何源码**。
> 基线:`f963702c`

## 1. 最重要的结论:failover 机制已经存在

Spec 的问题陈述把 failover 描述成待建能力。审计发现**核心机制已实现**,位于
`veya/supervision/runner.py`。缺的不是机制,而是**规范化、receipt 与可观测性**。

已存在的能力,逐条对应 Spec:

| Spec 要求 | 现状 | 位置 |
|---|---|---|
| 重新选择 executor | ✅ `select_mission_executor()` | `runner.py:228` |
| candidate 由 Registry 投影 | ✅ `executor_candidates()` | `runner.py:178` |
| capability 检查 | ✅ `check_capability_compatible()` | `runner.py:209` |
| health 检查 | ✅ `ExecutorHealthRegistry.get_health()` | `runner.py:215` |
| 完整 L1 链(INV-03) | ✅ identity→capability→health→admission | `runner.py:194-221` |
| 不走 retask(INV-04) | ✅ 直接 `_run_canonical_goal(assignee=selected)` | `runner.py:539` |
| preferred 只是偏好(§8) | ✅ 文档与实现一致,不 pin | `runner.py:239-243` |
| 排除已尝试 executor | ✅ `attempted` 集合 | `runner.py:516-521` |
| 副作用后拒绝重放 | ✅ `FAILOVER_REPLAY_UNSAFE` | `runner.py:500-508` |
| 候选耗尽 → blocked | ✅ `_no_execution_target()` | `runner.py:546` |
| 失败归因到责任层 | ✅ `_record_failure_against_the_responsible_layer()` | `runner.py:463` |
| 同一 GoalRun 新 attempt(INV-02) | ✅ `execution_attempts` 续跑账本 | `server/goal_run/models.py:449` |

`_failover_if_provider_failure`(`runner.py:466`)的 docstring 已经写明:
"Retask a provider-blocked iteration onto another eligible executor… must not
kill a mission that still has a lawful alternative."

## 2. Spec 中的错误码在代码库中不存在

```text
NO_HEALTHY_AVAILABLE_EXECUTOR     0 命中
```

真实代码是:

```text
NO_HEALTHY_EXECUTION_TARGET       veya/supervision/runner.py:586
FAILOVER_REPLAY_UNSAFE            veya/supervision/runner.py:506
RETASK_BLOCKED_WORKER_UNRESOLVED  veya/supervision/retask.py:272
```

Spec §5 引用 `NO_HEALTHY_AVAILABLE_EXECUTOR` 时应以 `NO_HEALTHY_EXECUTION_TARGET` 为准。

## 3. RETASK_BLOCKED_INVALID_NEXT_TASK 的真实语义

`veya/supervision/retask.py:270`,在 `_task_from_review` 内:

```python
if not objective:
    lineage["retask_block_reason"] = "RETASK_BLOCKED_INVALID_NEXT_TASK"
    return None, lineage
```

即 **next_task 为空**时阻塞。这从代码上证实了 Spec §1 的核心论点:
retask 是**任务变更**操作,`next_task` 是它的必填输入,因此 executor 更换
在结构上就无法用它表达。Spec INV-04「不得删除该 gate」是正确的,
而且现状已经符合 —— failover 走的是完全不同的路径。

## 4. 真正缺失的部分

| Spec 要求 | 现状 | 缺口 |
|---|---|---|
| `mission.executor_reselect` 规范化操作 | ❌ 只有内部 `_failover_if_provider_failure` | 需要 canonical 入口 |
| `ExecutorReselectionReceipt` 完整决策链 | ⚠️ 只有 3 键 dict | 见 §5 |
| 结构化事件(§16, 6 个) | ❌ 无 | 需要新增 |
| 幂等键(§11) | ⚠️ 只有内存 `attempted` set | 无跨进程幂等 |
| 并发 CAS(§12) | ❌ 未见 | 需确认 GoalRun 侧是否已有 |
| 公开可观测面 | ⚠️ `executor_inventory()` 已存在但未暴露为工具 | 需要暴露 |

## 5. 现有 substitution 记录远达不到 receipt 契约

`runner.py:541`:

```python
retried.executor_substitution = {
    "failed_executor": failed_executor,
    "failure_class": failure_class,
    "selected_executor": selected.executor_id,
}
```

Spec §10 要求回答:为什么原 executor 被淘汰 / 为什么某候选被选中或拒绝 /
有哪些 candidate / 为什么其他候选没被选 / 最终谁拿到 admission。

现有 3 键**无法回答其中任何一个问题**。被拒候选的原因
(`capability_satisfied` / `reachable` / `authenticated` / `health`)
已经计算在 `ExecutorCandidate` 上(`runner.py:202-219`),
只是没有随决策一起落盘 —— 这是最低成本的补齐路径,不需要新增计算。

另有一处已有证据落盘:`veya/remote/executor_health.py:458` 以
`kind="executor_substitution"` 记录,但那是 health 侧证据,不是 receipt。

## 6. Authority 归属确认

| Authority | 位置 | 状态 |
|---|---|---|
| Mission 状态机 | `MissionStatus`,`veya/supervision/models.py:27` | 唯一,注释明写 "THE canonical state machine" |
| Executor identity/capability | `veya/remote/executor_registry.py` | 唯一 |
| Health | `veya/remote/executor_health.py` | 唯一,failover 写入与 selection 读取**同一实例** |
| Admission | ExecutorRegistry(`_submit_worker_child` 路径) | 唯一 |
| GoalRun attempt 账本 | `server/goal_run/models.py:449` | 已存在,注释明写 "A new attempt never forks a new GoalRun" |

无第二套 authority。Spec §2 与 §18 的禁止项在现状下均已满足。

## 7. 与前一份 Spec 的边界

本 Spec 声明 Non-goal 是 Permission Effect Model。审计确认二者不重叠:

* SF-001 是 **effect 未送达引擎**(permission 语义)
* 本 Spec 是 **executor 身份选择**(L1 lifecycle)

`executor_candidates` 只读 identity 的 `executor_kind` / `reachable` /
`authenticated`,不涉及 effect 判定。

## 8. 实施规模的重估

Spec 设想的 P1–P5 隐含"从零建 failover"。审计后实际工作量为:

| 阶段 | Spec 设想 | 实际 |
|---|---|---|
| P1 规范化 API | 新建操作 | 把既有内部路径提升为 canonical 入口 + 请求/响应模型 |
| P2 Registry 接入 | 接入 registry | **已完成**,无需接入 |
| P3 lineage + receipt | 新建 attempt 模型 | attempt 账本已存在;**只需 receipt** |
| P4 真实资格 | 新链路 | 复用既有 failover 测试 + 补一条真实链路 |
| P5 Hicode | 同上 | 不变,仍需独立 reactivation |

即 P2 基本为零工作量,P3 大幅缩小。这是把"新建能力"纠正为"补齐可观测性"
的关键差别。

## 9. P0 未验证项(留给 P1)

1. 并发 CAS:GoalRun 侧是否已有 durable locking 可复用,未确认
2. `attempted` 的生命周期:仅单次迭代内存态,跨迭代重试是否重复尝试同一 executor
3. `_replay_safe` 的判定边界:哪些副作用被判为不可重放
4. `executor_inventory()` 是否已有对外暴露面

## 10. P0 补充验证:幂等(§11)与并发(§12)的地基不存在

P1 之前先验了 P0 未验证项的第 1、2 条。两条都指向同一个结论:
**failover 目前唯一的记忆是进程内的,因此 §11 与 §12 无法在现状上实现。**

### 10.1 无 durable CAS

| 检查 | 结果 |
|---|---|
| `server/goal_run/store.py:48` `save_goal_run` | tmp + `replace`,**崩溃安全写,但无版本检查** |
| `server/goal_run/store.py:114` `append_event` | 追加写,无 compare-and-swap |
| 全系统锁 | 均为 `threading.RLock`(`agent_mailbox.py:64`、`execution.py:889`、`execution.py:1213`)—— **进程内** |
| GoalRun 侧 CAS | **不存在** |

结论:两个进程各自读同一份 `execution_attempts` 并各自追加,是当前可发生的行为。

### 10.2 `attempted` 只覆盖本次迭代

`veya/supervision/runner.py:351` 每次迭代**新建**单元素集合:

```python
attempted={selected.executor_id},
```

它不是历史记忆,只排除本迭代刚失败的那一个。

### 10.3 health 是纯内存,且未知即健康

| 事实 | 位置 |
|---|---|
| `ExecutorHealthRegistry.__init__` 只有 `self._records = {}`,**无任何持久化路径** | `executor_health.py:324-326` |
| `HealthRecord` 默认 `state=UNKNOWN`, `provider_reachable=True`, `consecutive_failures=0` | `executor_health.py:304-314` |
| `get_health(allow_unknown=True)` 把 `UNKNOWN` **读作 HEALTHY** | `executor_health.py:440` |

三条合起来的后果:

1. **单进程内**:3 次连续失败 → `UNAVAILABLE`(`executor_health.py:390`),failover 有界,不会无限 ping-pong。
2. **跨进程 / 重启后**:失败记忆全部丢失。一个已知坏掉的 executor 在新进程里
   `state=UNKNOWN` → 被判为 `HEALTHY` → 重新入选。
3. **并发**:两个 supervisor 各持独立 health 视图,可能对同一 task 分别准入
   不同 executor。§12 要求的「only one active executor admission」在现状下
   **不是理论风险**。

### 10.4 对阶段计划的影响

`ExecutorReselectionReceipt` 不只是可观测性。按 §11 与 §12,它同时是
**failover 唯一缺失的 durable 记忆**:

- 幂等键(§11)需要落盘:`mission_id + iteration_id + task_id + failure_event_id`
  必须在 receipt 里,重复请求才能命中已有 receipt 而不是新建 attempt
- 并发准入(§12)需要一个 CAS,而 receipt 的写入正是那个 CAS 的载体 ——
  同一 `goal_run_id` 下「已存在本 task 的 active admission」就等于拒绝第二次

因此 P3 的定位应从「补可观测性」上调为「补 durable failover 记录」,
并且 P1/P3 需要触及 `server/goal_run/store.py` 的写入路径。
这是 durable execution authority,属于 §2 列出的 GoalRun 权限范围,
应在实施前单独取得同意。

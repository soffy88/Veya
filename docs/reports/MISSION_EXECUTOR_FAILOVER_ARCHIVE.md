# Mission Executor Failover v1.0 — 归档与基线

```text
Mission Executor Failover Reselection v1.0 = IMPLEMENTATION COMPLETE

P1a/P1b  PASS  1a5888e9
P2        PASS  既有 Registry 路径（审计确认，零工作量）
P3        PASS  25 个测试（durable ledger + receipt + 幂等 + 并发）
P4        PASS  31357429  真实链路
```

## 1. 基线

| 项 | 值 |
|---|---|
| 验收 commit | `31357429` |
| 实现 commit | `1a5888e9` |
| 证据 commit | 本提交 |
| P4 测试入口 | `venv/bin/python -m pytest -q tests/supervision/test_p4_real_failover_qualification.py` |
| P1/P3 测试入口 | `venv/bin/python -m pytest -q tests/supervision/test_executor_failover_reselection.py` |

## 2. 已固化的真实证据

原始凭据不再只存在于报告正文，落盘在
`docs/qualification/p4-real-failover/`:

| 文件 | 内容 |
|---|---|
| `EVIDENCE.json` | 关键 ID 与结论的机器可读摘要 |
| `failover_ledger.json` | 真实 `ExecutorReselectionReceipt` 全文（candidate / rejected / health / admission） |
| `goalrun_taskgraph.json` | 真实 GoalRun 终态 |

最近一次运行的 ID:

```text
failover_receipt_id   resel_4a6a4b48fd074cda
execution_attempt_id  attempt_6335ff15c774
idempotency_key       m-p4.it-0.t-p4.fe-p4
previous_executor     claude_code
selected_executor     opencode
goal_run              status=completed  acceptance_verdict=ACCEPT  unfinished_work=[]
```

## 3. 归档过程中发现并修复的缺陷

固化证据时读到持久化 ledger,发现 `execution_attempt_id: null` ——
attempt id 是在 `admit()` **之后**才赋值的,而 §10 要求 receipt 含该字段。
durable 副本才是审计会读的那一份,所以这是真缺陷,不是记录问题。

修复:在持久化**之前**铸造 attempt id,并补测试
`test_d5_persisted_receipt_carries_the_execution_attempt_id`
—— 断言持久化副本的 id 非空且等于内存副本。

这正是"固化证据"这一步的价值:报告正文当时是绿的。

## 4. WRITE 不可用 —— 环境/provider 能力限制,不是 failover 缺陷

明确归类,避免日后被误当作 failover 的问题:

```text
opencode    WORKER_CAPABILITIES.supports_write_task = False   ← 真实能力声明
claude_code  真实调用 AUTH_FAILURE
codex       真实调用 PROVIDER_UNAVAILABLE
antigravity unauthenticated
```

因此 P4 的真实任务为 READ。这是**执行环境与 provider 能力边界**,
与 failover 机制无关:

* failover 本身已被证明可用 —— 它在 canonical executor 真实 AUTH_FAILURE 后
  把任务交给了另一个真实可用且**真实执行成功**的 executor;
* 若把 canonical 换成可写且健康的 executor,failover 行为不变;
* 放宽 `supports_write_task` 去迁就任务,才是引入缺陷。

## 5. 与 failover 无关的独立观察(不在本 Spec 范围)

`ExecutorRegistry._credential_present` 只检查凭据文件**是否存在**,
不验证有效性。后果是 `claude_code` / `codex` 报 `authenticated: True`
却在真实调用时 AUTH_FAILURE。

这是 **Executor Health/Credential Truthfulness** 问题,已另立 Spec,
**不并入** P5 Hicode reactivation,以免两个变更互相污染:

`docs/specs/VEYA_EXECUTOR_CREDENTIAL_TRUTHFULNESS_SPEC.md`

## 6. 已证明的架构结论

```text
canonical executor
    ↓ unavailable
ExecutorRegistry                    ← 完整 L1: capability/health/policy/permission/selection/admission
    ↓
alternate executor
    ↓
real admission
    ↓
real worker                          ← 真实 provider 调用
    ↓
real verifier                        ← acceptance_verdict = ACCEPT
    ↓
same GoalRun COMPLETE                ← goal-runs 目录数 = 1,lineage 未分叉
```

而不是通过 retask 绕过去。因此:

```text
RETASK_BLOCKED_INVALID_NEXT_TASK   必须继续保留
executor change ≠ retask
```

该 gate 的保留已有测试守护
(`test_c4_retask_block_gate_still_exists`)。

## 7. 未完成 / 后续

| 项 | 状态 |
|---|---|
| P5 Hicode 进入候选池 | NOT STARTED,见 `VEYA_HICODE_EXECUTOR_REACTIVATION_SPEC.md` |
| credential 真实性 | 另立 Spec,未开始 |
| 本地底座 vs provider executor 排序策略 | 未覆盖;P4 按 §8 用偏好指定 |
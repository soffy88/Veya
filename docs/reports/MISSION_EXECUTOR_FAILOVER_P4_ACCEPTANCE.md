# MISSION_EXECUTOR_FAILOVER_P4_ACCEPTANCE

> 依据 `VEYA_MISSION_EXECUTOR_FAILOVER_RESELECTION_SPEC` §15 F / §19 P4。
> **P4 = PASS**:真实 worker + 真实 verifier + GoalRun COMPLETE 全部成立,全程无 mock。

```text
git SHA          1a5888e9 (P1/P3) + 本报告与 P4 测试
测试入口          venv/bin/python -m pytest -q \
                  tests/supervision/test_p4_real_failover_qualification.py
结果              1 passed in 138.29s
```

## 1. Provider preflight

按要求先做 preflight,三步全部实测通过。

### 1.1 当前可达且已认证的真实 executor

| executor | local | reachable | authenticated | health | write_task | 真实可执行 |
|---|---|---|---|---|---|---|
| `builtin` | True | True | True | LOCAL | — | 是(本地底座,非 provider) |
| `claude_code` | False | True | True | HEALTHY | True | **否(见 1.2)** |
| `codex` | False | True | True | HEALTHY | True | **否(见 1.2)** |
| `opencode` | False | True | True | HEALTHY | False | **是** |
| `pi` | False | True | True | HEALTHY | False | 未测 |
| `antigravity` | False | True | **False** | HEALTHY | True | 否 |
| `dsh` / `acp` / `grok` | False | — | False | HEALTHY | False | 否 |

`opencode` launcher `/home/soffy/.opencode/bin/opencode`,`status: READY`。

### 1.2 逐个真实执行结果(不是探测,是真跑)

| executor | task | 结果 | 失败原因 |
|---|---|---|---|
| `opencode` | WRITE | **BLOCKED** | `PINNED_EXECUTOR_NOT_WRITE_QUALIFIED` / `EXECUTOR_CAPABILITY_MISMATCH` |
| `claude_code` | WRITE | **FAILED** | `AUTH_FAILURE` — 凭据文件存在但真实调用被拒 |
| `codex` | WRITE | **FAILED** | `PROVIDER_UNAVAILABLE` |
| `opencode` | **READ** | **COMPLETED** | — |

结论:**本机唯一能完成真实任务的 provider executor 是 `opencode`,且只能做 READ。**

`WORKER_CAPABILITIES`(`veya/remote/worker_runtime.py`)是真实能力声明:

```
executor       write_task shell  read
claude_code    True        True   True
codex          True        True   True
antigravity    True        True   True
opencode       False       False  True
pi             False       False  True
```

### 1.3 Verifier 能消费真实 receipt

`opencode` READ 任务的 GoalRun(`goal_67ee8cfd76e04ece92f59df0ddb25399`):

```text
status:               completed
acceptance_verdict:   ACCEPT
unfinished_work:      []
```

**preflight 结论:P4 可行。**

## 2. failover 方向由证据决定

因为只有 `opencode` 能真跑,而它是 READ-only,failover 方向只能是:

```text
canonical   claude_code   真实 AUTH_FAILURE(preflight 实测)
alternate   opencode      真实 READ 任务(preflight 实测 COMPLETED)
```

这不是为了方便而选的:`claude_code` 的失败是**真实观察到的**,不是编造的。

## 3. P4 逐步证据

### C — canonical executor unavailable

通过**真实 health authority** 制造,未改任何生产代码:

```python
ExecutorHealthRegistry().record_failure(
    "claude_code", ExecutorFailureClass.AUTH_FAILURE, detail="observed in preflight")
```

这与 supervision runner 自身在 provider 故障后写入的是同一入口
(`runner.py::_record_failure_against_the_responsible_layer`)。

```text
before: {'claude_code': 'HEALTHY', 'opencode': 'HEALTHY', 'codex': 'HEALTHY'}
after : {'claude_code': 'UNAVAILABLE', 'opencode': 'HEALTHY', 'codex': 'HEALTHY'}
```

### A/B — Mission / GoalRun / task

真实 git 仓库(`/tmp/pytest-*/test_p4_*/repo`),`seed.txt` 内容 `p4-seed-value`,
`git init` + 首次 commit 真实存在。

```text
mission_id    m-p4
iteration_id  it-0
task_id       t-p4
goal_run_id   g-p4(ledger 命名空间)
failure_event fe-p4
```

### D/E — canonical reselect

```text
selected:  opencode
code:      RESELECTED
```

`preferred_executor="opencode"` 是**偏好不是 pin**(§8):它仍需通过
reachable / authenticated / health / capability / admission 全部闸门。

### K — receipt

```text
receipt_id         resel_58579e3fcffa47e8
execution_attempt  attempt_5b8ec6204982
admission          accepted=True
```

### candidate list 与 rejected candidates(含原因)

| executor | rejected reason |
|---|---|
| `acp` | UNREACHABLE |
| `antigravity` | UNAUTHENTICATED |
| `claude_code` | EXCLUDED_BY_REQUEST |
| `dsh` | UNAUTHENTICATED |
| `grok` | UNREACHABLE |
| `codex` | HEALTH_UNAVAILABLE(见 1.2 health 记录) |

### F/G — 真实 admission + 真实 worker

```text
child:        OPENCODE
status:       COMPLETED
terminal:     True
failure_class: None
worktree:     /tmp/.../.veya/worktrees/task-execution-direct-a6aa8bb2...-2f913f2b80
effect_receipt: 存在(真实)
```

`seed.txt` 执行后未被修改 —— READ 任务确实没写东西。

### I — Verifier

```text
acceptance_verdict: ACCEPT
```

### J — GoalRun COMPLETE

```text
goal_run_id: goal_928db36eeacd4176b480c648b360fc57
status:      completed
acceptance_verdict: ACCEPT
unfinished_work: []
```

### K — lineage

```text
mission_id / iteration_id / task_id / goal_run_id  全程不变
goal-runs 目录数: 1     ← failover 未 fork 第二个 GoalRun
```

## 4. 硬约束逐条核对

| # | 约束 | 状态 |
|---|---|---|
| 1 | 不修改 PermissionEngine / SF-001 | 未改 |
| 2 | 不修改 Command Sandbox / SF-002 | 未改 |
| 3 | 不修改 Hicode | 未改 |
| 4 | 不修改既有 ToolBinding 计数测试 | 未改(`test_supervision_p3` 的既有失败保持原样) |
| 5 | 不改变 task_id / task_contract / mission_id / GoalRun lineage | 已断言 |
| 6 | 不把 failover 实现成 retask | 未调用 retask;`RETASK_BLOCKED_INVALID_NEXT_TASK` gate 仍在 |
| 7 | 不使用 mock executor | 全程真实 provider 调用,138s 真实耗时 |
| 8 | 不绕过 ExecutorRegistry / permission / admission | 全部经 `worker.dispatch` 正常链 |
| 9 | 不删除或 reset 当前 WIP | `runtime/harness/{contract,models}.py` 与 `.qualification-provider-real` 原样保留 |
| 10 | provider 不可用时 STOP | preflight 已通过,无需 STOP |

## 5. 已知限制(如实记录)

1. **任务是 READ 而非 WRITE**,因为 `opencode` 声明 `supports_write_task=False`。
   这是真实能力声明,资格测试选择该 executor 能诚实完成的任务,
   而不是放宽契约。若需要 WRITE 链路,须先有一个**可用且 write-capable** 的
   provider —— preflight 证明本机目前没有。
2. **`claude_code` 标记为 authenticated 但真实调用 AUTH_FAILURE**。
   `ExecutorRegistry._credential_present` 只检查凭据文件**是否存在**,
   不验证有效性。`codex` 同类问题(`PROVIDER_UNAVAILABLE`)。
   这是一个独立的观察,**未在本 Spec 内修复**。
3. `builtin` 在 reselect 中曾被优先选中(本地底座排序在前)。
   本资格测试用 `preferred_executor` 指定 provider executor,
   符合 §8,但"本地底座 vs provider executor"的排序策略本身未被本 Spec 覆盖。

## 6. 结论

```text
真实 worker            PASS   (opencode, COMPLETED, 真实 effect_receipt + 隔离 worktree)
真实 verifier          PASS   (acceptance_verdict = ACCEPT)
GoalRun COMPLETE       PASS   (status = completed)
lineage 保持           PASS   (单 GoalRun, task/mission/iteration 不变)
无 mock                PASS   (138s 真实 provider 调用)

P4 = PASS
```

§15 F 的验收条件「真实 dispatch + 真实 worker + verifier + GoalRun completion」
全部成立。P5(Hicode 进入候选池)仍需 Hicode 独立 reactivation,本 Spec 不涉及。
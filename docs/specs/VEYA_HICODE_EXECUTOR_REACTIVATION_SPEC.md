# VEYA_HICODE_EXECUTOR_REACTIVATION_SPEC v1.0

Status: SPEC ONLY — NOT STARTED
Scope: Hicode as an L1 executor, end to end
Precondition: `VEYA_EXECUTOR_CREDENTIAL_TRUTHFULNESS_SPEC` 应先落地(见 §2)

---

## 0. Goal

Hicode 重新成为 L1 执行面的一部分,必须走完**完整生命周期**:

```text
registration → capability → credential/health → policy → permission
             → selection → admission → worker → receipt → real qualification
```

**任何一环缺失,`registered` 都不等于 `available`,更不等于 `usable`。**

明确禁止:

```text
preferred_executor=hicode  绕过任何门
直接调用 hicode CLI
复活 legacy/executors/hicode/* 作为实现
改 Mission failover 让它通融
```

## 1. 当前状态(2026-10-04,实测)

```text
ExecutorRegistry._KNOWN              不含 hicode
veya.executor_retirement              退役策略生效,is_retired("hicode") = True
server/hicode_cooldown.py             已迁至 veya/provider_cooldown.py
server/hicode_agent.py                已删除,仅存 legacy/
server/hicode_runtime.py              已删除
legacy/executors/hicode/              归档,禁止复活为实现
deploy/hicode-entrypoint.sh           引用已删模块,脚本本身已死
HICODE_MANAGED_PYTHON (宿主)         /home/soffy/.veya/hicode-runtime 不存在
which hicode                          无
provider-registry.json                "hicode": {..., "retired": true, "Do not re-admit."}
```

P4 preflight 进一步确认:本机**没有**任何 Hicode 二进制或托管 runtime。
`RemoteErrorCode.EMPTY_MODEL_RESPONSE` 现为无任何代码 raise 的枚举成员。

## 2. 为什么 credential truthfulness 是前置

`authenticated` 目前只检查凭据文件是否存在
(`ExecutorRegistry._credential_present`),已实测产生假阳性:
`claude_code` / `codex` 报 `authenticated: True`,真实调用却 AUTH_FAILURE。

若在假阳性未修的情况下重新启用 Hicode,会得到一个
「Registry 看起来健康、真实调用才失败」的 executor —— 而这正是
Mission failover spec 用真实链路(P4)才发现的问题。

因此:

```text
SF-CRED (credential truthfulness)  →  先落地
SF-HICODE (本 Spec)                →  再落地
```

两者不得在同一变更内,否则无法归因。

## 3. 实施前置(全部必须先满足)

| # | 前置 | 说明 |
|---|---|---|
| 1 | L2 runtime 实际存在 | 需要一个可执行的 hicode 二进制 + 其 python runtime;当前宿主与容器路径均不存在 |
| 2 | `veya.executor_retirement` 的退役被显式解除 | 不能靠 `is_retired` 打补丁;需要一个受控的、记录在案的退役解除 |
| 3 | `provider-registry.json` 的 `retired: true` 被更新 | 当前写明 "Do not re-admit" |
| 4 | SF-CRED 已落地 | `credential_valid` 存在且未探测时不呈现为已认证 |
| 5 | `deploy/hicode-entrypoint.sh` 不再引用已删模块 | 当前它调 `server.hicode_runtime`,该模块不存在 |
| 6 | 命名空间裁决 | `ExecutorRegistry` 用 `hicode`;`HARNESS_ENGINES` 曾用 `hicode`;需确认唯一规范名 |

任何一条不满足 → **STOP 并报告阻塞点**,不得推进 registration。

## 4. 分阶段实施

### P0 — 只读考古与清单

从 `b7b1a7d4`(退役 commit)之前的最后可用状态提取:

```text
旧实现依赖的模块清单
旧 provider / model 映射
旧 credential 要求
旧 capability 声明
旧 policy / permission 分类
旧 receipt 形状
```

输出 `docs/reports/HICODE_REACTIVATION_AUDIT.md`。**不得改源码。**

### P1 — Capability 声明

**新写** capability 声明,不从 legacy 复制实现。至少明确:

```text
supports_read_task / supports_write_task
supports_shell_effect / supports_git
model / provider
是否网络依赖
```

### P2 — Registration 与 retirement 解除

```text
ExecutorRegistry.register(identity)   唯一注册入口
veya.executor_retirement              受控解除,带记录
provider-registry.json                同步更新
```

`register()` 内已有退役守卫(`executor_registry.py::register`),
解除必须走显式路径,不得删守卫。

### P3 — Credential / Health

```text
credential_valid 由真实探测支撑(依赖 SF-CRED)
health 独立记录
两者归因可区分
```

### P4 — Policy / Permission / Admission

Hicode 触达 action gateway 与 admission 时,必须走既有链。
不允许因为 executor 名字特殊而获得额外权限,也不允许被额外限制
到无法完成已声明 capability 的程度。

### P5 — Worker 与 Receipt

复用现有 worker adapter 与 `EffectReceipt`。
**不新建第二套 worker 或 receipt 机制。**

### P6 — 真实资格

至少一次真实链路:

```text
Mission task
  ↓
ExecutorRegistry 选择到 hicode(非 pin,需过全部门)
  ↓
真实 admission
  ↓
真实 hicode 执行
  ↓
真实 verifier
  ↓
GoalRun COMPLETE
```

禁止 mock 作为唯一资格证据。

### P7 — 进入 failover 候选池

只有 P6 通过后,Hicode 才成为
`VEYA_MISSION_EXECUTOR_FAILOVER_RESELECTION_SPEC` §5 的合法 candidate。

## 5. 与 Mission Failover 的关系

Failover 已 IMPLEMENTATION COMPLETE(见
`docs/reports/MISSION_EXECUTOR_FAILOVER_ARCHIVE.md`)。

Hicode 进入候选池后,必须满足:

```text
registered + capable + healthy + policy allowed
+ permission allowed + admitted
```

缺任一项,它只是一个名字。`preferred_executor=hicode` 仍然只是偏好,
仍需逐门通过 —— 这是 failover spec §8 已验证并由测试守护的语义。

## 6. Acceptance Gates

```text
G1  registration      在 ExecutorRegistry 中,且退役解除有记录
G2  capability        声明与真实行为一致(读/写/shell/git)
G3  credential/health  credential_valid 由真实探测支撑
G4  policy/permission 与其他 executor 同等对待,无特殊豁免
G5  selection/admission 经完整 L1,preferred 不构成 pin
G6  worker/receipt     复用既有机制,无第二套
G7  real qualification 真实链路 GoalRun COMPLETE
G8  failover 候选     完整 P4 链路在含 hicode 时仍成立
```

## 7. Rollback

```text
退役解除是一条可逆的显式记录
回滚 = 恢复退役状态 + 从 registry 注销
provider-registry.json 与 executor_retirement 同步回滚
```

不得通过删除代码来回滚。

## 8. 风险

| 风险 | 缓解 |
|---|---|
| L2 runtime 不存在 | §3 前置 1;不满足即 STOP |
| 旧实现在 legacy 中被误当实现复用 | P0 只做考古;实现必须新写 |
| 命名空间与既有 executor 冲突 | §3 前置 6 |
| credential 假阳性 | 依赖 SF-CRED 先落地 |
| 权限意外放宽 | G4 要求与既有 executor 同等对待 |

## 9. Deliverables

```text
docs/specs/VEYA_HICODE_EXECUTOR_REACTIVATION_SPEC.md   本文件
docs/reports/HICODE_REACTIVATION_AUDIT.md              P0
docs/reports/HICODE_REACTIVATION_ACCEPTANCE.md         验收
implementation + tests + real qualification evidence
```

## 10. 当前判定

```text
P5 Hicode Reactivation = NOT STARTED

阻塞点(截至 2026-10-04):
  1. 本机与容器均无 Hicode L2 runtime
  2. provider-registry.json 标记 "Do not re-admit"
  3. SF-CRED 未落地,credential 仍为假阳性
  4. deploy/hicode-entrypoint.sh 引用已删模块
```

在 §3 六项前置全部满足前,**任何 registration 都是伪造**。
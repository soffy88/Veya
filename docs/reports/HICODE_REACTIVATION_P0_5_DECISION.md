# HICODE_REACTIVATION_P0_5_DECISION — 架构裁决 + runtime 前置门

> **P0.5 = ARCHITECTURE_READY_BUT_RUNTIME_BLOCKED**
> 零源码改动。停止于 §28 的 P0.5-G/H。
> 前序:`8c426cfb`(P0 考古)、`91f77a69`(SF-CRED 独立真实验收)

## 0. 总体结果

```text
G1 Historical Baseline     PASS
G2 Namespace              BLOCKED_BY_RUNTIME
G3 Credential Model       PASS
G4 Fault Domain           PASS
G5 Legacy Failure         PASS
G6 Effect Classification  PASS
G7 Runtime                BLOCKED   (R0 = NOT_FOUND)

P0.5 = ARCHITECTURE_READY_BUT_RUNTIME_BLOCKED
P1   = BLOCKED
```

§29 明令禁止「P0.5 PASS 但 runtime 实际不存在」。
本报告据此**不**声明 P0.5 PASS。

## 1. 执行记录（§28 顺序）

| 阶段 | 内容 | 结果 |
|---|---|---|
| A | 历史基线修正 | 完成 — 基线改为 `937184cd^`，`b7b1a7d4^` 判 INVALID |
| B | namespace 裁决 | **BLOCKED_BY_RUNTIME** |
| C | credential model 裁决 | 完成 — Model B(分层) |
| D | Antigravity fault domain | 完成 — `shared_fault_domain = true` |
| E | legacy failure semantics | 完成 — `EMPTY_MODEL_RESPONSE` 不从 legacy 恢复 |
| F | effect classification 边界 | 完成 — 4 工具边界记录 |
| G | L2 runtime discovery | **R0 = NOT_FOUND** |
| H | 真实 runtime/provider probe | **未执行**(无可启动 runtime) |
| I | 最终裁决报告 | 本文件 |

## 2. G1 — Historical Baseline: PASS

```text
HICODE_LAST_REAL_AVAILABLE_BASELINE = 937184cd^

_KNOWN(937184cd^) = ("pi","hicode","codex","antigravity","opencode","dsh")
_KNOWN(b7b1a7d4^) = ("pi","codex","antigravity","opencode","claude_code","dsh","grok","acp")
                                                  ↑ 无 hicode → 原 spec 前提 INVALID
```

hicode 的移除是两个相隔 5 天的独立事件：

```text
2026-09-28  937184cd   从 _KNOWN 移除(注册消失,实现仍在 server/)
2026-10-02  b7b1a7d4   实现归档进 legacy/(15 files, 0 增 0 删)
```

已同步修正 `VEYA_HICODE_EXECUTOR_REACTIVATION_SPEC.md` §4,
并落 `docs/qualification/hicode-p0-5/BASELINE.json`。

## 3. G2 — Namespace: BLOCKED_BY_RUNTIME

### 已确定的事实

```text
历史 executor_kind      = "internal_hicode"
历史 capabilities       = frozenset()   空
现行 executor_kind 取值 = "l1_worker" | "in_process_substrate"
当前 8 个 executor      全部 l1_worker
```

`fault_domain` 与 `internal_hicode` 在现行代码中**均无对应物**。

### 为何不能选 N1（standard L1 worker）

`veya/supervision/runner.py:201` 显示现行唯一结构区分是 `local`：

```python
local = identity.executor_kind == _LOCAL_SUBSTRATE_KIND   # "in_process_substrate"
...
capability_satisfied = local_capable if local else check_capability_compatible(...)
reachable            = True if local else identity.reachable
authenticated        = True if local else identity.authenticated
health               = "LOCAL" if local else health.get_health(name)
```

把 hicode 归为 `in_process_substrate` 会让它**绕过 capability 检查、
绕过 authenticated、绕过 health** —— 正是 §2 约束 11/12/14 禁止的路径。

### 为何不能选 N2/N3/N4 而不断言

| 候选 | 需要的证据 | 现状 |
|---|---|---|
| N2 internal executor | worker / health / receipt 接口 | **NOT_TESTABLE**(无 runtime) |
| N3 provider-backed | provider binding + native probe | 部分可观察(`config.toml`)，但无 runtime 无法确认 probe 语义 |
| N4 composite | 子执行边界与故障隔离语义 | **NOT_TESTABLE** |

### 裁决

```text
D1 = BLOCKED_BY_RUNTIME
```

解锁条件(§25 `HICODE_EXECUTOR_KIND` 必须有值):

```text
1. managed Reasonix runtime 存在且可启动(R0 → R4)
2. 在该 runtime 上实测 worker 边界:它接收什么任务、返回什么 receipt
3. 实测 health probe 面:能否在不执行任务的情况下判断可用性
4. 裁决 in_process_substrate 的绕过语义是否需要为 hicode 变体保留
```

**不得**先注册为 `l1_worker` 再补证 —— 那会污染 admission 面,
且 §2 约束 14 明令禁止。

## 4. G3 — Credential Model: PASS

```text
CREDENTIAL_TRUTH_CONTRACT = Model B (Runtime-owned),Model A 仅限可观测子集
```

依据与六问答详见 `docs/reports/HICODE_CREDENTIAL_MODEL.md`。要点：

```text
credential_owner     = runtime(按 provider 分裂)
credential_source    = api_key_env 环境变量名 / 本地 proxy
credential_present   = NOT_APPLICABLE(veya 侧无凭据源)
credential_valid     = runtime 认证证据(非文件系统证据)
credential_fresh     = UNVERIFIED → BLOCKED_BY_RUNTIME
```

历史做法 `authenticated = bool(provider and model)` 是**第三种假阳性**,
比 SF-CRED 已修的两种都宽松,且与凭据完全无关。本裁决取代它。

§7 Forbidden 三条路径全部写入 `DECISIONS.json.forbidden_paths`。

**但**：Layer 2 探测需要 runtime，故 eligibility 的三项条件当前**无一满足**,
构成 §27 STOP CONDITION 4。

## 5. G4 — Fault Domain: PASS

```text
fault_domain_id          = FD-GEMINI-UPSTREAM
shared_fault_domain      = true
selection_diversity_rule = RULE FD-1..FD-4(见 FAULT_DOMAIN 报告 §5)
```

决定性证据是 `config/hicode_model_mapping.json` 的显式字段
`internal_provider: antigravity` —— 不是从命名推断。

`fault_domain` 概念此前**不存在于本仓库**，属首次定义。

## 6. G5 — Legacy Failure Semantics: PASS

`EMPTY_MODEL_RESPONSE` 四问逐项核实：

| 问题 | 答案 | 证据 |
|---|---|---|
| 1. 仍有生产 producer? | **否** | producer 在 `legacy/executors/hicode/hicode_agent.py` |
| 2. 仍有 canonical failure class? | **否** | 不在 `ExecutorFailureClass` / `ExecutionFailureClass` / `ProviderFailureClass` 任一 enum |
| 3. 仍有 verifier consumer? | **否** | 无任何 verifier 引用该 code |
| 4. 仍有 receipt consumer? | **语义上否** | `runtime.execution.observability` 接受任意 `code` 字符串(通用透传,非语义消费者) |

唯一残留引用：

```text
veya/remote/models.py:25                    RemoteErrorCode.EMPTY_MODEL_RESPONSE   ← 死枚举成员
tests/test_p0_observability.py:281          code="EMPTY_MODEL_RESPONSE"            ← 自由字符串字面量
```

### 裁决

```text
不得从 legacy 恢复(§10)。
若将来需要,必须映射到 canonical namespace:
    ProviderFailureClass.PROVIDER_MODEL_UNAVAILABLE
理由: 空响应 = provider 对所请求模型未返回可用内容。
      ExecutorFailureClass.MODEL_FAILURE 备选,但它把责任放在 executor 层,
      而空响应发生在 provider 层 —— ProviderFailureClass 的 docstring 正是
      按"哪一层出的问题"划分的。
```

**不得复制旧实现。**

## 7. G6 — Effect Classification: PASS

```text
HICODE 工具总数        = 7
  PURE_READ(3)         hicode_sessions, hicode_status, hicode_tasks
  无 effect 声明 (4)   hicode_run, hicode_rollback, hicode_review, hicode_stop
非工具(易误计)        hicode_bound_workspace / hicode_bound_execution_id (ContextVar)
                      hicode_progress (事件类型)
```

### 边界裁决

```text
UNKNOWN ≠ READ

在 SF-001 未完成前:
    Hicode 4 个未知 effect 工具
        ↓
    not eligible for privileged admission
```

记录阻塞标识：

```text
HICODE_EFFECT_CLASSIFICATION_BLOCKER
```

本阶段**不修复 SF-001**(§11、§2 约束 7),只记录边界。

风险说明:若复活时沿用 `side_effect=None`,这 4 个工具会默认放行 ——
这正是 SF-001 在主面上记录的问题("38 个非只读声明全部 ALLOW")在
hicode 上的重演。

## 8. G7 — Runtime: BLOCKED

```text
RUNTIME_GATE_STATE = R0 (NOT_FOUND)
```

证据见 `docs/reports/HICODE_RUNTIME_GATE.md`。要点：

```text
/home/soffy/.veya/hicode-runtime            存在,但只有 config.toml + 空 state/
/home/soffy/.veya/hicode-runtime-managed    存在但完全为空
reasonix / hicode on PATH                  不存在
managed manifest                           不存在
可执行 runtime 二进制                       不存在
```

### 对继承断言的实测更正

P0 spec §1 与 P0 审计报告称 `/home/soffy/.veya/hicode-runtime` **不存在**。
**实测存在。** §23 禁止"应该存在"式断言,故更正:

```text
准确表述:runtime 目录存在,但不含任何 runtime 产物。
         R0 指 runtime 本身 NOT_FOUND,而非父目录不存在。
```

`config.toml` 是本机唯一存活的 runtime 侧配置,也是 G3 的主要证据 ——
一个此前被判定为"完全不存在"的目录里,藏着 credential model 的关键事实。

## 9. P1 Entry Contract 状态（§25）

| 输入项 | 状态 |
|---|---|
| `HICODE_EXECUTOR_KIND` | **BLOCKED_BY_RUNTIME** |
| `HICODE_CAPABILITIES` | **UNKNOWN** — 未确认前不得声明 |
| `HICODE_CREDENTIAL_MODEL` | Model B(已裁决) |
| `HICODE_HEALTH_MODEL` | **UNKNOWN** |
| `HICODE_FAULT_DOMAIN` | `FD-GEMINI-UPSTREAM`(已裁决) |
| `HICODE_RUNTIME_ID` | **NOT_FOUND** |
| `HICODE_RUNTIME_VERSION` | **NOT_FOUND** |
| `HICODE_PROVIDER_BINDING` | 部分 OBSERVED(`cliproxy-google` @ 127.0.0.1:10100) |
| `HICODE_EFFECT_CLASSIFICATION` | 边界已记录,4 工具仍 UNKNOWN |

**4 项 UNKNOWN / NOT_FOUND,无一标记 not applicable。**

```text
P1 = BLOCKED
```

## 10. STOP

命中 §27 条件：

```text
1. L2 runtime 不存在                    成立(§8)
2. runtime manifest 不存在              成立(§8)
4. credential validity 无法获得真实证据   成立(Layer 2 需 runtime)
5. Hicode namespace 无法确定            成立(§3)
6. Antigravity fault domain 无法确定     不成立 —— 已裁决(§5)
```

```text
P0.5 BLOCKED
Reason:     L2 managed Reasonix runtime 与 canonical manifest 均不存在(R0)。
            D1 namespace 需要 runtime 才能确认 worker/health/receipt 边界。
Evidence:   docs/reports/HICODE_RUNTIME_GATE.md
            docs/qualification/hicode-p0-5/RUNTIME_EVIDENCE.json
Required external prerequisite:
            一个可执行的 managed Reasonix runtime + canonical manifest,
            满足 P0.5 §15 九项字段(version / binary / commit 等),
            置于 HICODE_MANAGED_PYTHON 指向的位置。
            到位后需重跑 P0.5-G 与 P0.5-H。
```

STOP 不是 FAIL。G1/G3/G4/G5/G6 的裁决在 runtime 到位后**不需重做**。

## 11. Deliverables

```text
docs/reports/HICODE_REACTIVATION_P0_5_DECISION.md   本文件
docs/reports/HICODE_RUNTIME_GATE.md
docs/reports/HICODE_CREDENTIAL_MODEL.md
docs/reports/HICODE_FAULT_DOMAIN.md
docs/qualification/hicode-p0-5/DECISIONS.json
docs/qualification/hicode-p0-5/RUNTIME_EVIDENCE.json
docs/qualification/hicode-p0-5/BASELINE.json
docs/specs/VEYA_HICODE_EXECUTOR_REACTIVATION_SPEC.md   §4 基线已修正
```

`SOURCE CHANGES = 0`。§24 要求的源码修改**未发生**,因此无需另立
implementation phase。
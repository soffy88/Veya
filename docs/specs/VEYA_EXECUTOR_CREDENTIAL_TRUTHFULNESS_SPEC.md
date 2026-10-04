# VEYA_EXECUTOR_CREDENTIAL_TRUTHFULNESS_SPEC v1.0

Status: SPEC ONLY — NOT STARTED
Scope: ExecutorRegistry identity projection / ExecutorHealthRegistry
Non-goal: Hicode reactivation / Mission failover / PermissionEngine / Command sandbox

---

## 0. Problem

```
ExecutorRegistry._credential_present
    ↓  只判断 credential 文件存在
    ↓
authenticated = True
    ↓  registry snapshot 报健康、eligible
    ↓
ExecutorCandidate.qualified = True
    ↓
真实 provider 调用
    ↓
AUTH_FAILURE / PROVIDER_UNAVAILABLE
```

实测(2026-10-04,P4 preflight):

| executor | registry `authenticated` | 凭据文件 | 真实调用结果 |
|---|---|---|---|
| `claude_code` | True | `~/.claude/.credentials.json` 存在 | **AUTH_FAILURE** |
| `codex` | True | `~/.codex/auth.json` 存在 | **PROVIDER_UNAVAILABLE** |
| `opencode` | True | `~/.local/share/opencode/auth.json` 存在 | 成功 |

根因在 `veya/remote/executor_registry.py::_credential_present`:

```python
def _credential_present(names: list[str], files: list[Path]) -> bool:
    return any(bool(os.environ.get(name)) for name in names) or any(
        path.is_file() for path in files
    )
```

它回答的是「凭据是否**存在**」,而 `ExecutorRuntimeIdentity.authenticated`
这个词回答的是「凭据是否**有效**」。两者被当成同一件事。

## 1. 为什么单独立项

`authenticated` 参与三处判定:

1. `ExecutorCandidate.eligible`(supervision/runner.py)—— 不可达/未认证即不合格
2. `ExecutorRuntimeIdentity.selectable`
3. Mission failover 的 candidate 过滤

因此一个失效凭据会让 executor 出现在健康快照里、被选中、被准入,
然后在真实调用时失败。P4 之所以要用 READ 任务而不是 WRITE,
部分原因就是 `claude_code` / `codex` 在 capability 层之外还要在**调用层**
再失败一次 —— 换句话说,registry 的判断没有拦住它。

这与 SF-001(permission effect)、SF-002(command sandbox)、Hicode reactivation
都是不同层次的问题。混在一起会让每个变更都难以归因。

## 2. Design Principle

必须区分三件事,当前只有第一件:

```text
credential_present   文件或环境变量在不在        已有,可靠
credential_valid     凭据能不能完成一次真实调用   缺失
credential_fresh     凭据会不会很快失效           缺失
```

`authenticated` 只能由第二件支撑。第一件不足以支撑这个词。

## 3. 目标语义

### 3.1 新增独立字段

```text
credential_present : bool     文件/环境变量存在(现状,保留)
credential_valid   : bool | None   真实探测结果;None = 未探测
authenticated      : bool     仅在 credential_valid 为 True 时为 True
```

`credential_valid is None`(未探测)时,`authenticated` **不得为 True**。
未探测必须 fail-closed 到「不可用」,或让 `authenticated` 变成三态
(`UNKNOWN`),由消费方决定是否 fail-closed。

### 3.2 探测方式

必须是**真实**的最小调用,不能是格式校验:

```text
每个 provider 一条最小探测
  记录耗时与结果
  结果缓存,带 TTL
  TTL 内不重复探测
  失败不立即重试到「不可用」,需退避
```

探测必须走该 provider 自己的 client/adapter,不得绕过
`ProviderRegistry` 或 worker adapter 直连 CLI。

### 3.3 与 health 的关系

`ExecutorHealthRegistry` 目前把「进程活着」与「provider 健康」分开,
这是对的。本 Spec 补的是第三个维度:**凭据是否有效**。

```text
credential_valid = False   → 独立于 health 记录,不与 provider 故障混淆
health = UNAVAILABLE        → provider 侧问题
```

两者都导致 `eligible = False`,但归因不同,必须能区分。

## 4. Non-Goals

* 不重新启用 Hicode(见 `VEYA_HICODE_EXECUTOR_REACTIVATION_SPEC.md`)
* 不改 Mission failover 机制(已 IMPLEMENTATION COMPLETE)
* 不改 PermissionEngine / permission effect(SF-001)
* 不改 command parser / sandbox(SF-002)
* 不要求所有 executor 同时探测(可分批)

## 5. Implementation Phases

### P0 — 只读盘点

输出每个 executor 的三元组现状:

```text
executor | credential_present | credential_valid | health | 真实调用结果
```

**不得改源码。**

### P1 — 字段与投影

引入 `credential_present` / `credential_valid`,`authenticated` 改为由
`credential_valid` 支撑。**默认保持现状值**(即未探测 = 未认证),
使行为变化可测量。

### P2 — 真实探测

按 provider 逐个接入最小真实探测,带缓存与退避。

### P3 — 回归与真实资格

```text
至少一个 executor 从 authenticated=True 翻转为 False,且有真实证据
至少一个 executor 保持 True,且真实调用成功
```

## 6. Acceptance Gates

```text
G1  字段分离          present / valid / authenticated 三者不再混用
G2  未探测 fail-closed  未探测不得呈现为已认证
G3  真实探测          探测走 provider client,非直连 CLI
G4  归因可区分        credential 失效与 provider 故障可分别归因
G5  真实资格          至少一次"registry 改判 + 真实调用证实"
G6  无回归            Mission failover P4 链路仍可复现
```

## 7. 风险

探测本身要花钱与时间。缓解:

* TTL 缓存,默认不高于 P4 一次资格的成本量级
* 只对**被请求选择**的 executor 探测,不横扫全部
* 探测失败不得把 executor 永久钉死为不可用(凭据可能当天修复)

## 8. 与 P4 的关系

P4 已用**真实调用**绕过了这个假阳性(claude_code 被 health 记录为
UNAVAILABLE 后才被 failover 排除)。本 Spec 的目标是让 registry 自己
就能说对,而不是靠一次真实失败来纠正。

因此本 Spec 完成后,P4 那种「先真跑一遍才发现 AUTH_FAILURE」的探测
应当变成 registry 可见的状态。

## 9. Deliverables

```text
docs/specs/VEYA_EXECUTOR_CREDENTIAL_TRUTHFULNESS_SPEC.md   本文件
docs/reports/CREDENTIAL_TRUTHFULNESS_INVENTORY.md            P0 产出
docs/reports/CREDENTIAL_TRUTHFULNESS_ACCEPTANCE.md           验收
```
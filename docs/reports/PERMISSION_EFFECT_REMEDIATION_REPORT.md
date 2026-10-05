# PERMISSION_EFFECT_REMEDIATION_REPORT — SF-001

> 授权：用户选定「Layer 1 + Layer 2 一起做」+「UNKNOWN 延伸为权限层原则」
> 结果：**哨兵转绿，unit-fast 首次全绿**；核心写文件工具**未**回归
> 剩余缺口见§6，未伪造完成。

## 0. 结果

```text
tests/test_tool_governance_3o.py       17 passed  (was 16 passed 1 failed)
tests/remote/test_permission_p0_cd.py  auto_open 恢复
unit-fast                              365 passed, 0 failed
权限/策略/准入全域(14 文件)              279 passed
mypy / ruff                             clean
```

`test_layer4_action_policy_does_not_allow_unknown_non_read_effect` 是长期已知
失败项,现转绿。它是 unit-fast 里唯一的失败,因此 required suite 首次全绿。

## 1. 根因(一句话)

`PermissionEngine._evaluate` 的判定只读 `command_effect`(命令危险性),
**从不读 `context.remote_effect`**;而 `PolicyRequest` 根本不携带 effect,
`_build_context` 用三工具硬编码白名单把其余一切当作 `read`。
两者叠加:一个未分类的 remote 动作以 `command_effect=NONE`、
`filesystem_effect=read` 到达引擎,直接 ALLOW。

证据(改动前实测):

```python
# veya/remote/policy_resolver.py:454
effect = "write" if request.tool in {"file.write","file.patch","artifact.write"} else "read"
```

这正是 `SF-001-permission-effect-dropped.md` 标题所述"governed actions 以 read
到达引擎"。

## 2. 更正三处继承事实

### 2.1 "77/146 工具已声明 effect" —— 不成立

```text
声明源实测(SideEffect.*):
  PURE_READ 25 / LOCAL_WRITE 16 / PROCESS_EXEC 1  = 42
  legacy bool True                                  = 10
  合计                                              52
veya/remote/effect_registry.py declared_ids()       = 0   ← 空的
```

`effect_registry` 自 `f59f732f` 引入后**从未被 declare 过**,也未接线
(`grep effect_registry` 在 veya/server/runtime 中零命中)。

### 2.2 `classify_action_effect` 对几乎一切返回 `remote`

```text
oskill.classify_action_effect("file.read")  -> 'remote'
oskill.classify_action_effect("shell.exec") -> 'remote'
oskill.classify_action_effect("zzz_fake")   -> 'remote'
146 工具生产路径实测:remote 138 / read 5 / local_write 3
```

因为 `remote` 是**作用域**而非效果。`effect_registry` 的文档字符串早已写明这条
规则("非匹配报为 UNKNOWN,不报 remote"),但该registry 是空的,规则没被执行。

**这是本次最大的爆炸半径风险**:若不先修 `_effect_for`,Layer 1 会把 138 个工具
全部变成需审批。已在 `_effect_for` 中修正。

### 2.3 权威原则早已存在于 3O 平台

```python
>>> oskill.classify_tool_effect("file.write")
ValueError: tool effect must be declared by a ToolSpec
```

平台**拒绝分类未声明工具**,并接受 `declared_effect`。所以"必须声明"不是新主张,
veya 只是没有执行它。

## 3. 改动(3 处生产代码)

### 3.1 Layer 1 送达 —— `policy_resolver.py`

`PolicyRequest` 新增 `effect: str = ""`,并由 adapter 从服务端推导后传入。
文档字符串明确它**不是客户端输入**:请求路径上无任何东西填充它,
空值意为"未声明"。

`_build_context` 由三工具白名单改为按声明映射:

```text
(未声明)   → 见 §6 临时桥      remote        → remote_effect="mutation" + REMOTE scope
read       → read              destructive   → filesystem write + privilege_level=host
local_write→ write             (其余)        → none
```

### 3.2 Layer 2 判定 —— `permission_engine.py`

新增一条分支,置于既有 `filesystem_effect` 规则之后、破坏性分支之下:

```python
if context.remote_effect not in {"none", "read"}:
    return PermissionDecision(
        Decision.APPROVAL_REQUIRED,
        ReasonCode.APPROVAL_IRREVERSIBLE_REMOTE, ...
    )
```

**复用既有冻结规则**(与 `filesystem_effect` 分支同形),而非新造一套。
位置在下层是为了保证更严重的裁决永不被软化。

### 3.3 `_effect_for` 作用域修正 —— `action_gateway_adapter.py`

分类器返回 `"remote"` 时降级为 `"unknown"`;`"remote"` 仅保留给已声明的
`external_mutation`。

## 4. 顺带修好的第二个洞

实测枚举全部声明 effect 的裁决,发现 `destructive` 落到 `ALLOW`:

```text
改动前                          改动后
(未声明) PROJECT ALLOW          (未声明) PROJECT ALLOW
read       PROJECT ALLOW        read       PROJECT ALLOW
local_write USER    ALLOW       local_write USER    ALLOW
remote     REMOTE   APPROVAL_REQUIRED   remote  REMOTE   APPROVAL_REQUIRED
destructive USER    ALLOW       destructive HOST    APPROVAL_REQUIRED  ← 修好
```

`destructive` 的 ALLOW **不是本次引入的**(改动前经 `ALLOW_READ_ONLY` 也是 ALLOW),
但把最危险的一类放行是错的。修法同样是**复用**既有规则:让 `_build_context` 为
destructive 提供 `privilege_level="host"`,既有
`scope==HOST and filesystem_effect not in {read,none}` 规则即可触发。

## 5. 爆炸半径(146 工具面实测)

```text
未声明 / unknown → ALLOW              138    与改动前一致
read              → ALLOW               5    与改动前一致
local_write       → ALLOW               3    与改动前一致
remote(已声明)    → APPROVAL_REQUIRED    0    ← 当前无工具声明 external_mutation
destructive       → APPROVAL_REQUIRED    0    ← 当前无工具声明 privileged
```

即**新分支当前在生产中尚不触发**,因为没有任何工具声明
`external_mutation`/`privileged`。它在有人正确声明外部变更或特权操作时生效,
且不会静默放过。

## 6. 剩余缺口(未完成,不得声称已修)

### 6.1 `file.write` / `file.patch` / `artifact.write` 没有任何声明

这是本次最重要的发现。三者在 veya registry、oskill ToolSpec 中**均无
`side_effect` 声明**,它们被当作写操作**只靠那份硬编码白名单**。

一度尝试让"未声明"等于"无效果",结果是三个核心写文件工具被阻断
(`test_project_mutation_is_auto_open` 失败)。因为引擎会报
`ALLOW_READ_ONLY`(capability `workspace.read`),与工具层授予的 write capability
不交集,capability-intersection guard 随即拒绝。

**临时桥**:`_UNDECLARED_WRITE_TOOLS = {file.write, file.patch, artifact.write}`
保留原行为,并在代码中**明确标注为待替换的临时措施**及其原因。

**正确收口**:在各自的 ToolSpec 上声明 `LOCAL_WRITE`。这是
`classify_tool_effect` 已经在要求的事。完成它即可删除临时桥与那份硬编码列表。

### 6.2 其余约 94 个未声明工具

按用户选定的策略,UNKNOWN **不强制审批**(那会使 94 个工具不可用),而是
阻断特权准入。实测确认该阻断已**结构性成立**:未声明工具经 `_build_context`
得不到 `privilege_level="host"` 或 `service_effect="system"`,因此 `_scope()`
只会给出 `PROJECT`/`USER`,**无法进入 HOST 准入**。

但这属于"没给证据所以拿不到特权",而非"显式判定为不可准入"。若要显式表达,
需要在 admission 层新增规则 —— 那是新增公开面,本阶段不做。

### 6.3 `effect_registry` 仍为空且未接线

它是为此问题建立的规范 authority,至今 `declared_ids() == 0`。
§6.1 与 §6.2 的收口都应经由它,而不是再增加散落的映射。

## 7. 修正的测试字面量

```python
- assert decision.verdict == "REQUIRE_APPROVAL"
+ assert decision.verdict == "APPROVAL_REQUIRED"
```

`PermissionEngine.Decision` 的 canonical 取值是 `APPROVAL_REQUIRED`;
`obase` 中两种拼写均不存在。`REQUIRE_APPROVAL` 是**生产系统从不产生的字符串**,
该断言因此从未可能通过 —— 它并没有在测试它声称的规则。

同文件另外 4 处 `REQUIRE_APPROVAL` 是**构造 fixture**(非断言),未改动。

`test_policy_resolver.py` 补上 `effect="local_write"`:该测试测的是**层优先级**
而非 effect 推断,补的是它所表达的声明。

## 8. 遗留失败(既有,已实测确认非本次引入)

```text
tests/remote/test_execution_contract_p1.py::test_cli_execution_manifest_json
    ValueError: Executor retired: 'hicode'          ← Hicode 退役导致
tests/remote/test_execution_contract_p1.py::test_principal_identity_authorization_isolation
    DID NOT RAISE ExecutionError
```

在 `git archive HEAD` 的干净检出上**同样失败**,已实测确认。

## 9. 传感器

```text
ruff check / format     clean(改动文件)
mypy                    Success: no issues found in 2 source files
unit-fast               365 passed, 0 failed
权限/策略/准入 14 文件   279 passed
test_tool_governance_3o 17 passed
```

## 10. 下一步

```text
已完成
  1. ToolSpec effect population          22c7daeb  临时桥已删,declared_ids() 0 -> 3
  2. SF-002 两处边界缺口                  858becd8  走私形式 + 包装器绕过
  3. SF-CRED consumer cutover             9dc0557f  candidate.authenticated 已消除
  4. A1 审批在 policy hook 内消解          1815c9da  remote effect 强制已恢复

仍欠
  5. ~94 个未声明工具的分类（需人工读实现，静态分析不可信）
  6. A2 —— 见下节
  7. Hicode P1（阻塞于 Reasonix runtime，见 HICODE_REACTIVATION_P0_5_DECISION.md）
```

## 11. A2 待办 —— 3O 引擎的审批通路

**状态**：OPEN，未实施。归属 `platform/3O/oservi/`，**不在 veya 本仓 git 跟踪内**。

### 问题

```text
platform/3O/oservi/oservi/engines/action_gateway.py
    if decision.verdict != "ALLOW":
        return {"status": "failed", ...}
```

`approval_resolver` 已声明为 `Injection(kind="layer4", cardinality="0..1")`，
但这条非 ALLOW 路径**完全不查它** —— "需审批"与"已拒绝"被同等对待。

实测后果：`publish` 这类合法携带 `remote_effect="mutation"` 的 canonical action，
即使调用方传入 `lambda _request: True`，仍被判 `failed`。

### 当前靠 A1 绕过

`server/action_gateway_adapter.py::_settle_approval_sync` 在 policy hook 内先消解审批，
让引擎只见到 `ALLOW` / `DENY`。代价是消解逻辑落在 veya 侧，
而它本属引擎的职责 —— 引擎已经声明了那个注入点却不使用。

### A2 要做什么

让 `ActionGatewayEngine.invoke` 在非 ALLOW 时区分 `REQUIRE_APPROVAL` 与 `DENY`：
前者走 `approval_resolver`，解析通过则继续执行，拒绝或无法解析才返回 failed。

### 为什么不在本仓做

跨项目改动，且该子树未被跟踪 —— 改了不会进入 veya 历史，也无法在此 review。
需要 3O 平台侧的独立变更。

### 迁移时的兼容注意

A1 若在 A2 落地后保留，会**双重解析审批**（veya 侧已消解成 ALLOW/DENY，
引擎不会再看到 REQUIRE_APPROVAL，故实际不会重复，但语义会分叉）。届时应：
1. 删除 `_settle_approval_sync`，恢复"无 resolver 则裁决不动"的原行为；
2. 保留其三条失败模式测试（async / raising / no-resolver）作为回归防护；
3. 更新本文档本节状态。

### 相关证据

```text
1815c9da  A1 落地,remote 强制恢复
3b403aeb  上一次回退(当时误判为分支错误,实为接线错误)
报告 §7   修正的测试字面量
```

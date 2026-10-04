# Security Finding SF-001 — governed actions reach the permission engine as "read"

> 状态：**OPEN / 阻塞 Local2 收敛**
> 发现阶段：Local2 最终收敛 Spec,S2(G1 归因)
> 分支：`3946a997`
> 归因：**pre-existing**,早于本轮 15 个 commit

## 1. 结论

`server/action_gateway_adapter.py` 算出了正确的 effect-aware `OperationContext`,
但在策略解析成功时**丢弃它**,改用一个**丢失了 effect 信息**的请求去问权限引擎。
结果是 146 个已注册工具中的 138 个 —— 包括明确具破坏性与外部可见的 ——
在没有任何一层执法的情况下获得 `ALLOW`。

这不是测试陈旧,是活的 fail-open。

## 2. 证据链

### 2.1 断链位置

```
server/action_gateway_adapter.py:167
    remote_effect="mutation" if effect == "remote" else "none"      ← 正确算出

server/action_gateway_adapter.py:172-187
    try:
        resolver.resolve(PolicyRequest(actor, tool, args, workspace, cwd, goal_id))
        decision = policy_decision.decision.value                   ← 成功即采用
    except Exception:
        permission = self._permission_engine.evaluate(context)      ← 上面那个 context 只在异常时用

veya/remote/policy_resolver.py:92-105
    class PolicyRequest:  无 effect 字段

veya/remote/policy_resolver.py:454
    effect = "write" if request.tool in {"file.write","file.patch","artifact.write"} else "read"
```

⇒ 除非工具名恰好是那 3 个写工具,引擎一律被问"这是 read"。

### 2.2 实测:引擎收到的 context

对 `effect="remote"` 的请求插入探针:

```
remote_effect = 'none'          ← 不是 mutation
filesystem    = 'read'
process       = 'none'
-> decision: ALLOW ALLOW_READ_ONLY
```

### 2.3 实测:哪些层在执法

`policy_resolver.resolve` 的 provenance(7 层全弃权,仅引擎裁决):

```
layer=managed            outcome=ABSTAIN
layer=security           outcome=ABSTAIN
layer=workspace          outcome=ABSTAIN
layer=goal               outcome=ABSTAIN
layer=agent              outcome=ABSTAIN
layer=skill              outcome=ABSTAIN
layer=tool               outcome=ABSTAIN   rule=tool.abstain
layer=permission-engine  outcome=ALLOW     rule=engine.allow_read_only
```

`policy_resolver.py:386-397` 的 tool 层只登记 7 个工具的 grant,
其余全部 `tool.abstain`。

### 2.4 实测:受影响工具

146 个注册工具中 **138 个**被 `oskill.classify_action_effect` 分类为 `effect="remote"`。
逐个走 `_evaluate_policy`:

```
github_pr_create_draft    -> ALLOW
github_pr_post_review     -> ALLOW
veya_review_apply         -> ALLOW
memory_forget             -> ALLOW
skill_delete              -> ALLOW
team_shutdown_request     -> ALLOW
unclassified_remote       -> ALLOW
```

分布: `remote: 138, local_write: 3, read: 5`。

## 3. 预-existing 证明

失败测试的 import 闭包 = 196 个本地模块,与本轮 15 个 commit 改动集的交集为 3 个文件:

```
server/goal_run/leaf.py
server/graft_autocontext.py
server/tool_registry.py
```

把这 3 个文件还原到 baseline 版本后重跑,失败方式**完全相同**:

```
AssertionError: assert 'ALLOW' == 'REQUIRE_APPROVAL'
```

`veya/remote/policy_resolver.py` 与 `veya/remote/permission_engine.py`
在本轮从未被修改(它们不在 `b665c698..HEAD` 的改动集内)。

因此归因成立:**本阶段未引入,本阶段也未依赖该错误行为**。

## 4. 为何窄修复无效(已实测)

在 `permission_engine.py` 的 read-only 分支加上
`and context.remote_effect in {"none", ""}` 后:

- `tests/test_tool_governance_3o.py` 仍失败
- 探针显示引擎收到的 context 仍是 `remote_effect='none'`、`filesystem='read'`

因为 effect 根本没有传到引擎,改引擎的判定条件碰不到断链位置。

## 5. 真正的修复范围(超出本 Spec)

1. `PolicyRequest` 增加 effect 字段
2. `_build_context` 使用该字段,不再硬编码三工具白名单
3. `_evaluate_policy` 不再丢弃已算好的 effect-aware context
4. 为 138 个工具补 tool 层 grant,或给出明确的引擎规则

第 4 步的效果面:这些工具从 `ALLOW` 变为 `APPROVAL_REQUIRED`。
这是权限 authority 的变更,必须独立 Spec、独立验收。

## 6. 对 Local2 收敛的影响

`VEYA_LOCAL2_FINAL_COMPLETION_CONVERGENCE_SPEC` §3.2 要求确认
"failure 不代表 L0/L1/L2 安全边界退化"。本 finding 正是该边界退化,
因此 §3.3 的 `G1-A PASS_WITH_BASELINE_FAILURE` 不可用("非 release-critical"
无法诚实满足),归入 §3.3 的 `G1-B`:

```
G1 = FAIL
LOCAL2_COMPLETE = NO
```

本分支未修改任何权限代码;`veya/remote/permission_engine.py` 与 HEAD 逐字节一致。
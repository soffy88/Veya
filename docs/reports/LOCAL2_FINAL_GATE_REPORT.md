# Local2 最终收敛 Gate 报告

> Spec: `VEYA_LOCAL2_FINAL_COMPLETION_CONVERGENCE_SPEC v1.0`
> 执行顺序: S1→S9,按 §17
> 最终判定: **`LOCAL2_COMPLETE = NO`**

## Heads

```
BASE_HEAD   3946a99794ed376410be58530b82383a7de57340   (S1 冻结)
FINAL_HEAD  a32db3b0f018dca821fee57bd4e5745144563667
```

## Commit 数量与分类

区间 `b665c698..FINAL_HEAD` 共 18 个 commit,其中 `47610dad` 为他人并发工作。
**本阶段 commit = 17**。

Spec 撰写时预期 15 个。差额来自 Spec 自身要求的两个产出:

* `37a68b89` docs(security) — G1 归因结果是 FAIL,§3.2/§3.3 要求记录证据
* `a32db3b0` fix(backends) — G2 本就在 Spec 范围内

### Commit 矩阵（§8 分类,全部 ACCEPTED）

| commit | 语义 | impl/tests/docs | 分类 |
|---|---|---|---|
| `7ce6e9bb` | 关闭未鉴权 backend 执行面 | 3/2/0 | KEEP — security boundary |
| `0e76d43b` | autonomous fail-closed | 3/1/0 | KEEP |
| `66247a07` | leaf run identity 碰撞 | 1/1/0 | KEEP |
| `34d44fda` | git path 过滤 | 2/1/0 | KEEP |
| `272f95ce` | secret ignore + 描述泄漏 | 5/2/0 | KEEP(边界略宽,两项均已论证) |
| `37472f80` | capability 从权威派生 | 2/1/0 | KEEP |
| `d9c4ad21` | journal 损坏可见 | 3/1/0 | KEEP |
| `137bac7f` | wait 非终态不算成功 | 2/1/0 | KEEP |
| `3220a7d2` | required suite 死条目 + 护栏 | 1/1/0 | KEEP |
| `8ba49677` | backend 统一响应形状 | 2/1/0 | KEEP |
| `4ed04172` | 修自己测试的跨文件泄漏 | 0/1/0 | KEEP — 非机械,抹掉会隐藏"原测试是错的"这一事实 |
| `72337f20` | Hicode 退役收尾 | 2/5/0 | KEEP |
| `81604cb5` | facade 委托到统一链 | 1/2/0 | KEEP — security boundary |
| `454a3471` | F3/F7 决策提案 | 0/0/1 | KEEP — §12 要求保持 proposal |
| `3946a997` | 重命名文件的换行修复 | 0/1/0 | SQUASH-CANDIDATE,未执行 — 为一个换行重写已发布历史不值得 |
| `37a68b89` | SF-001 记录 | 0/0/1 | KEEP — G1 产出 |
| `a32db3b0` | facade shell 双门 | 2/2/0 | KEEP — security boundary |

§7.2 十项核验:线性无 merge(0)、无生成物、未吞并他人 WIP、冻结文件 0 次改动
(`tests/test_inferera_free_pool.py` / `veya/obase/_llm_config.py` / `veya/obase/llm.py`)、
`runtime/harness/*` 0 次提交。

## Required Suite 结果

```
262 passed
1 failed
```

失败:`test_layer4_action_policy_does_not_allow_unknown_non_read_effect`

## Baseline Failure 归类

**不是 PASS_WITH_BASELINE_FAILURE,是 FAIL。**

归因(pre-existing 已确证):

```
失败测试 import 闭包 = 196 本地模块
闭包 ∩ 本阶段改动 = {leaf.py, graft_autocontext.py, tool_registry.py}
三者还原到 baseline 后,失败方式完全相同:assert 'ALLOW' == 'REQUIRE_APPROVAL'
policy_resolver.py / permission_engine.py 本阶段 0 次改动
```

但根因是活的 fail-open,不是陈旧测试:

```
adapter 算出 remote_effect="mutation"  → try 分支成功后被丢弃
PolicyRequest 无 effect 字段
policy_resolver.py:454  effect = "write" if tool in {3个写工具} else "read"
⇒ 138/146 个工具(含 github_pr_create_draft / github_pr_post_review /
  veya_review_apply / memory_forget / skill_delete / team_shutdown_request)
  全部 ALLOW,7 层全部 ABSTAIN,无任何一层执法
```

详见 `docs/security/SF-001-permission-effect-dropped.md`。
按 §3.2 第 5 条"不代表安全边界退化"无法诚实满足 ⇒ §3.3 G1-B。

## Facade 双门（G2）

```
Gate A  VEYA_BACKEND_FACADE_ROOT
Gate B  VEYA_BACKEND_FACADE_ALLOW_SHELL   仅接受 "1" / "true"
```

§6 八 case 全部覆盖,21 个测试通过。Case 4 为**真实 dispatch**(无 mock shell):
到达 ExecutorRegistry、pre-admission、GoalRun pre-create、durable parent + child。

行为红证据:仅设 Gate A 时,旧代码 `ok=True` 并创建真实 execution
`parent_14de137c82234505b3b5114506f2b8ae`;新代码 `SHELL_NOT_AUTHORIZED`、无 execution。

`FACADE_PERMISSIONS` 保持 canonical 上界,git/destructive/network 在任何门组合下均关闭。

## WIP 保全

```
S1  tracked : .qualification-provider-real / runtime/harness/models.py / runtime/harness/contract.py
    untracked: 6 gold 产物 + tests/integration/test_harness_3o_runtime.py

FINAL 完全一致
CONCURRENT_WIP_PRESERVED = YES
contract.py 校验和 ed828c64d6573512… 前后一致(临时还原验证后逐字节复原)
```

## L0/L1/GoalRun 回归

| 层 | 结果 |
|---|---|
| unit-fast (required) | 262 passed, 1 failed (SF-001) |
| goalrun | 165 passed, 1 skipped |
| personal | 29 passed |
| runtime(用已提交 contract.py) | 2 failed, 465 passed, 5 skipped, 1 xfailed |
| runtime(含他人并发 WIP) | 额外 2 failed(`oskill.capability_intersection` 缺失) |

`runtime` 的 2 个 failed 中,只有 1 个是真失败:

* **真失败**:`test_coding_sandbox_profiles.py::test_command_parser_rejects_shell_escape_and_runner_captures_redacted_artifact`
  `parse_command("echo ok && touch outside")` 被接受。`bwrap` 存在 ⇒ 测试真跑,非 skip。
  `runtime/coding/command_runner.py` 本阶段 **0 次改动** ⇒ pre-existing。
* **回声**:`test_p9c_automated` / `test_coding_harness_contract` 是外层 meta-test,
  它们把上面那个失败再报一遍。

另 2 个(`test_coding_crash_resume.py`)由**他人未提交的** `runtime/harness/contract.py`
调用 `oskill.capability_intersection` 引起,该调用在未提交 diff 里,HEAD 版本没有。

## Gate 判定

| Gate | 要求 | 状态 |
|---|---|---|
| G1 | required suite 归因 | **FAIL** — SF-001,release-critical security path |
| G2 | facade shell 双门 | **PASS** |
| G3 | commit 完整性 | **PASS** — 17/17 ACCEPTED |
| G4 | WIP 保全 | **PASS** |
| G5 | L0/L1/L2 回归 | **FAIL** — pre-existing `parse_command` shell-escape 缺口 + 并发 WIP 导致的失败 |

## 未在本阶段处理

按 §1 Non-Goals 与用户裁决,以下均未动:

* SF-001 的修复(`PolicyRequest` 加 effect / `_build_context` 使用 / adapter 不丢 context /
  138 个工具的 grant)—— 权限 authority 变更,需独立 Spec
* `parse_command` 的 shell 转义检测缺口 —— 同类,建议另立 SF-002
* `runtime/harness/*` 的并发 WIP
* F3/F7 的语义落地(§12:NO SEMANTIC MIGRATION)
* `legacy/` Hicode(§11:保持 archive)

## 最终判定

```
G1 = FAIL
G2 = PASS
G3 = PASS
G4 = PASS
G5 = FAIL

LOCAL2_COMPLETE = NO
```

理由:§16 要求五个 Gate 全部 PASS 才可 COMPLETE。G1 与 G5 FAIL,且两者都指向
**本阶段之外的既有权限/沙箱边界缺陷**,不是收敛工作可以关闭的。按 §3.3 与
§1,不得把它们偷换成本阶段 failure,也不得通过降低 gate 达成 PASS。

已完成的部分是真实闭环的:backend 执行面已收归唯一权威、required suite 从
"无法启动"恢复为可运行并有护栏、facade 的 shell entitlement 从隐式变为双门、
Hicode 退役收尾、commit provenance 可审计、WIP 零损失。
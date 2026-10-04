# PERMISSION_EFFECT_MIGRATION_REPORT — Phase 2 STOP

> 依据 `VEYA_PERMISSION_AUTHORITY_EFFECT_MODEL_SPEC` §16:
> 「如果 ALLOW 数量发生大规模变化:STOP,必须产生 decision report。」
> 状态:**STOP**,未修改任何权限判定代码。

## 1. 结论

SF-001 的修复**不是一个窄修复**。断链有两层,修好第一层不改变任何判定:

```text
Layer 1  effect 在 PolicyRequest 边界被丢弃
         → 可窄修,但单独修完 0 个工具改变判定(见 §3)

Layer 2  PermissionEngine 的判定分支不按 effect 收紧
         → 需要改权限语义,影响全部 146 个工具(见 §4)
```

因此「让 77 个已声明工具生效」无法在不触碰冻结权限 authority 的前提下完成。

## 2. Phase 2 前的一次自我更正

Phase 0 与 SF-001 初版均称「无任何工具声明 effect」(DECLARED 0 / UNKNOWN 138)。
**该结论错误**:探测了不存在的 `master_tools._side_effects`,真实通道是
`master_tools._tool_specs`(146 条 `ToolSpec`)。

真实分布(`95577575` 已更正文档):

```
PURE_READ          39
LOCAL_WRITE        29
PROCESS_EXEC        7
EXTERNAL_MUTATION   2
UNDECLARED         69
```

**77/146 已声明**,其中 38 个非只读声明当前全部 ALLOW。
Stage D 爆炸半径是 69,而非 146。

## 3. Layer 1 —— effect 送达边界即可,但不改变判定

已验证 effect 正确到达 `ActionRequest.effect`:

```
github_pr_create_draft  EXTERNAL_MUTATION -> "remote"      -> ALLOW
github_pr_post_review   EXTERNAL_MUTATION -> "remote"      -> ALLOW
veya_mission_run        PROCESS_EXEC      -> "process"     -> ALLOW
coding_run_command      PROCESS_EXEC      -> "process"     -> ALLOW
coding_run_tests        PROCESS_EXEC      -> "process"     -> ALLOW
coding_run_lint         PROCESS_EXEC      -> "process"     -> ALLOW
coding_run_typecheck    PROCESS_EXEC      -> "process"     -> ALLOW
coding_build            PROCESS_EXEC      -> "process"     -> ALLOW
harness_sensor_run      PROCESS_EXEC      -> "process"     -> ALLOW
skill_delete            LOCAL_WRITE       -> "local_write" -> ALLOW
```

`_evaluate_policy` 读到了它,构造了正确的 effect-aware `OperationContext`,
但 `try` 分支成功后改用不含 effect 字段的 `PolicyRequest`
(`policy_resolver.py:454` 硬编码),该 context 只在 `except` 分支使用。

**但把 effect 完整送进引擎后实测:**

```
判定分布: {'ALLOW': 146}
```

即 Layer 1 单独修复的收益为 **0**。

## 4. Layer 2 —— 引擎的 effect→context 映射把所有类别导向 ALLOW

`server/action_gateway_adapter.py` 的 `effect_to_field`:

```python
"read":        ("read",   "none",    "none")
"local_write": ("write",  "none",    "none")
"process":     ("none",   "inspect", "none")   ← 命中只读分支
"network":     ("none",   "none",    "network")
"remote":      ("none",   "none",    "none")   ← 命中只读分支
"destructive": ("write",  "none",    "none")
"privileged":  ("write",  "none",    "none")
```

对照 `veya/remote/permission_engine.py:1306` 的只读分支:

```python
if context.filesystem_effect in {"read", "none"} and context.process_effect in {"none", "inspect"}:
    return ALLOW_READ_ONLY
```

* `process` → `("none","inspect")` ⇒ **命中只读分支**
* `remote`  → `("none","none","none")` ⇒ **命中只读分支**
* `local_write` / `destructive` / `privileged` → `filesystem=write` ⇒ 落到
  `scope == PROJECT or USER` 分支 ⇒ `ALLOW_PROJECT_MUTATION`

**六类 effect 全部落在 ALLOW 分支上。** 所以「正确送达 effect」本身不构成修复。

## 5. Phase 2 分类尝试的失败(按实现分类,已尝试并放弃)

用户选择「按实现逐个分类」。两种静态传递分析都不可信:

| 做法 | 结果 | 失败模式 |
|---|---|---|
| 仅同模块调用解析 | 133/146 无命中 | **假阴性**:工具跨模块委托,`skill_delete` → `get_personal_runtime().delete_skill` |
| 全仓函数名索引(11,918 名 / 4,444 文件) | **97 个被标成全集** `(DESTRUCTIVE,NETWORK,PROCESS,WRITE)` | **假阳性**:裸名撞名。`grep` 被解析到 `collaboration.py:join`、`team_registry.py:broadcast`;`get`/`add`/`append`/`_path` 匹配到数十个无关模块 |

假阳性比 UNKNOWN 更危险:`grep`、`list_files`、`memory_search` 被标成 DESTRUCTIVE
会造成大规模误拒,直接违反 §2 的

```text
permission_effect(tool) == actual_side_effect_class(tool)
```

**结论:静态分析无法在此代码库给出可信分类。** 69 个未声明工具需要人工逐个阅读实现,
这本身是 Spec §8 的工作内容,但不应与「修 Layer 2」混在同一改动里。

## 6. 为什么不自行修 Layer 2

Layer 2 需要改 `PermissionEngine` 的判定分支顺序与条件,即冻结的权限 authority。
后果面已量化:**当前 146/146 ALLOW**;一旦按 effect 正确收紧,`PROCESS_EXEC`(7)、
`EXTERNAL_MUTATION`(2)、`LOCAL_WRITE`(29)、以及 69 个未声明工具的判定都会变化。

§16 要求这种情况下 STOP 并出本报告。§1 Non-Goals 亦禁止在未确认下改权限模型。

## 7. 已完成且不涉及权限语义的部分

| 产出 | commit | 性质 |
|---|---|---|
| Phase 0 inventory | `bcd9e686` | 只读盘点(已在 `95577575` 更正) |
| Phase 1 `veya/remote/effect_registry.py` | `f59f732f` | **未接线**,纯只读 resolver,27 测试 |
| 更正 DECLARED 0 → 77 | `95577575` | 事实更正 |

Phase 1 的 registry 已具备 Spec §2/§3/§5 要求的不变量,并有两处关键拒绝行为:

* `oskill` 对未知 action 返回 `"remote"`,而 remote 是作用域词 → 映射为 `UNKNOWN`,
  不进入 Effect 枚举
* 无 effect 的声明被拒绝,而非默认成 READ

## 8. 建议的下一步(需授权)

按爆炸半径从小到大:

1. **只接 Layer 1**(让 effect 送达引擎)。独立可回滚,**但必须同时接受"判定 0 变化"**;
   它的价值是让 Layer 2 的后续改动可被测量,而不是立即产生拒绝。
2. **Layer 2 分级收紧**:先让 `PROCESS_EXEC` / `EXTERNAL_MUTATION` /
   `DESTRUCTIVE` 三类不再走 ALLOW 分支(9+ 个已声明工具,爆炸半径最小),
   `LOCAL_WRITE` 与未声明工具留待后续。
3. **人工分类 69 个未声明工具**(Spec §8 的 Class R/W/S/N/D/U)。
4. 最后 Stage D:UNKNOWN fail-closed。

每步都需要 decision report 与回归,不合并。
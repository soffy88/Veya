# SF002_SANDBOX_BOUNDARY_QUALIFICATION — 边界资格

> 性质：**adversarial boundary qualification**，不是 happy-path 测试。
> 交付：`tests/security/test_sf002_sandbox_boundary.py`（36 passed，已注册 unit-fast）
> 源码改动：**0**

## 0. 结论

```text
四个边界的当前成立状态（实测,非推断）

  命令语义边界   FAIL   5 个走私形式 + 1 个包装绕过
  路径边界       PASS   工作区外写入被拒，未执行后报告
  权限边界       FAIL   sh/bash/env/ssh 包装可降级 remote mutation
  证据边界       PASS   拒绝不产生 execution_id / goal_run_id
```

SF-002 **不能判定为 PASS**。发现两处真实可利用缺口。

## 1. 隔离的既有失败（按要求，不并入 SF-002）

```text
tests/supervision/test_supervision_p3.py::test_high_level_mcp_bindings_share_the_same_server
    硬编码 ToolBinding 计数 43 vs 实际 42
tests/remote/test_execution_contract_p1.py  2 处
    一处 ValueError: Executor retired: 'hicode'
    一处 DID NOT RAISE ExecutionError
```

三处均早于本轮，且已在 `git archive HEAD` 干净检出上复现。**baseline-known，不参与 SF-002 判定。**

## 2. 命令语义边界 —— FAIL

`parse_command_context` 按**首个可执行名**分级，因此凡是把真实命令推迟给另一个解释器的写法，都按包装器分级。

### 2.1 被完全无视的分隔符与替换（5 个走私形式）

```text
'echo ok; touch pwn'        READ_ONLY  ALLOW    ← ; 未解析
'echo ok\ntouch pwn'        READ_ONLY  ALLOW    ← 换行未解析
'echo $(touch pwn)'         READ_ONLY  ALLOW    ← $() 未展开
'echo `touch pwn`'          READ_ONLY  ALLOW    ← 反引号未展开
'env touch pwn'             READ_ONLY  ALLOW    ← env 前缀未剥离
```

第二命令对引擎完全不可见，变更操作伪装成读取。

### 2.2 已正确识别的组合形式（这些必须保持）

```text
'&&'  '||'  '|'  '&'  eval''  sh -c''     → 均判为 REVERSIBLE_MUTATION 或更严
```

注意：被判为 `REVERSIBLE_MUTATION` 后，项目内变更会被 ALLOW
（`ALLOW_PROJECT_MUTATION`）。这属于 SF-001 的裁决范围，不是 SF-002 的缺口。

### 2.3 拒绝是同步的、无 execution_id

```text
'echo ok > /tmp/sf002'    DENY               DENY_SCOPE_ESCAPE
'touch ../../outside'     DENY               DENY_PATH_TRAVERSAL
'echo ok > /etc/hosts'    APPROVAL_REQUIRED  APPROVAL_HOST_PRIVILEGE
```

三者都拒绝执行，但**拒绝理由不同且正确**：工作区外路径分两类 ——
`/tmp` 是 scope escape 直接 DENY；`/etc/hosts` 解析为 host scope 走审批。

## 3. 路径边界 —— PASS

```text
工作区外写入    DENY（scope escape / path traversal），执行前拒绝
工作区内写入    REVERSIBLE_MUTATION → ALLOW_PROJECT_MUTATION（边界是工作区，不是全禁写）
target drift   parse_command_context("touch report.txt") → 
               target_paths=(PosixPath('.../veya/report.txt'),)  相对路径按声明 cwd 解析
```

`target_paths` 已实际解析并记录，未发现 shell / file / git 三者解析不一致。

## 4. 权限边界 —— FAIL（真实绕过）

```text
'git push origin main'                REMOTE_MUTATION         APPROVAL_REQUIRED
"sh -c 'git push origin main'"        REVERSIBLE_MUTATION     ALLOW    ← 绕过
"bash -c 'git push origin main'"      REVERSIBLE_MUTATION     ALLOW    ← 绕过
"env sh -c 'git push'"                READ_ONLY               ALLOW    ← 降级更深
"ssh host 'git push'"                 REVERSIBLE_MUTATION     ALLOW    ← 绕过
```

**特权与破坏类未被绕过**（记录在测试中）：

```text
'sudo rm -rf /'                PRIVILEGED_HOST_MUTATION  APPROVAL_REQUIRED
"sh -c 'sudo rm -rf /'"        REVERSIBLE_MUTATION       APPROVAL_REQUIRED
```

原因是 host scope 独立于命令文本解析。**但 remote 类完全依赖命令文本**，
所以被包装器绕过。

与 SF-001 的交叉：工具声明的 `remote` effect 现在会被拦（`CREDENTIAL_REFUTED` /
`APPROVAL_IRREVERSIBLE_REMOTE`），但一个 `shell.exec` 工具把 `git push` 包进
`sh -c` 就从引擎判定下溜了。声明门与命令门之间存在这道缺口。

## 5. 证据边界 —— PASS

```text
DENY / APPROVAL_REQUIRED / ALLOW   三种结果三种不同 reason，不互相可读
拒绝不留 execution_id            execution_id is None
拒绝不留 goal_run_id             goal_run_id is None
意图效果被记录                    filesystem_effect == "write"（receipt 必须能说明拒绝了什么）
decision 自带 scope + effects     receipt 可证明 target / effect / admission / sandbox decision
引擎不产生 submitted / running    判定只有三态，进度态属于执行层，混淆即是缺口
```

一处曾被我写错并纠正：拒绝时 `filesystem_effect` **应当**是 `write`。
记下意图是证据要求；**不得**累积的是执行证据。

## 6. 为何缺陷以「钉住」而非「断言通过」的形式存在

```text
test_tested_command_smuggling_forms_are_pinned
test_shell_wrappers_are_pinned_as_a_permission_bypass
```

这些断言的是**当前存在缺陷**这一事实，并写明"解析器一旦识别该形式，本测试即失败，
届时删除钉子并改为断言修正后的分类"。

理由：断言"边界成立"会让 suite 变绿而缺口仍在 —— 那正是本次要抵制的
"看起来应该安全就算成立"。钉住让缺口无法被遗忘，且修复时会自动提醒。

**修复合并触及冻结权限 authority**：`parse_command_context` 位于
`veya/remote/permission_engine.py`，改动会影响所有使用这些形式的命令的裁决，
需用户授权。

## 7. 传感器

```text
tests/security/                36 passed
unit-fast                      401 passed（新增 36）
ruff / format                  clean
```

## 8. 建议的下一步（需授权）

```text
1. 授权 parse_command_context 识别 ;  换行  $()  反引号  env 前缀
2. 授权解析器解包 sh -c / bash -c / ssh 的内层命令后再分级
   —— 或改为"遇未知包装器即升级为最严"，这比解包更保守且更小
3. 两项落地后，把对应钉子换成正向断言
```

第 2 项的保守做法值得优先考虑：解包需要正确解析引号嵌套，容易再出错；
而"遇到不认识的包装器就按最严处理"不需要解析内层，且失败方向安全。
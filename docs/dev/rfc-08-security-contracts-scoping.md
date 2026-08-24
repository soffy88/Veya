# RFC-08: PR-09 安全契约 — 范围决策

> 状态：已执行（2026-08-24）
> 依据：docs/VEYA_10_OF_10_PLAN.md §11（Sandbox / AuthZ / Security 做到 10/10）
> 范围：决定这次做什么、不做什么，理由记在这里而不是留一堆没说清楚的取舍。

## 1. 现状核实

`veya/obase/sandbox.py` 已有的比预期完整：资源限制（`ulimit` 前缀，不动宿主进程
rlimit）、审计日志（落盘 `.veya/audit/`）、危险命令拦截（`is_dangerous_command`,
`tests/test_sandbox_g4.py` 13 项覆盖）、文件系统快照回滚（`FileSystemSandbox`）。
路径逃逸防护（`_resolve_path`/`_resolve_write_path`, `server/tool_registry.py`）
用 `Path.resolve()` 先展开再做 containment 检查，测试前没验证过这个展开顺序真的
挡得住符号链接逃逸（只有一条纯 `../` 穿越测试）。

计划 §11.2/§11.4 要的 capability token（nonce/scope/expiry/session/tool/resource）
和完整对抗性套件（11 类）都没有。

## 2. 决策：做什么，不做什么

### 2.1 做：`SandboxProfile` 分级（纯附加）

`SandboxConfig` 现在只有 `allow_write`/`network_blocked` 两个布尔维度。新增
`SandboxProfile` 枚举（`READ_ONLY`/`BUILD`/`NETWORKED`/`PRIVILEGED`）+
`profile_for(config)` 从这两个已有维度推导标签——不改任何执行时判断，只是给
一份配置贴一个人可读的名字，方便审计日志/未来的策略分组读。

**没做 `TEST` 档**：计划 §11.3 要 `READ_ONLY/BUILD/TEST/NETWORKED/PRIVILEGED`
五档，但现有 `SandboxConfig` 分不出"能跑测试但不能碰业务代码"这种更细的语义——
`allow_write` 只有能写/不能写一个维度。伪造一个假的 TEST 判断（比如"猜测命令里
有没有 pytest 关键字"）比不实现更糟——那是一个没有真实约束力的假标签，会让人
误以为有隔离实际没有。要支持 TEST 档需要先在 `SandboxConfig` 加一个新维度
（比如"能执行代码但文件系统只读"），这是需要单独设计的事，不是分类函数能凭空
造出来的。

### 2.2 做：符号链接逃逸 + 写路径穿越测试（真实验证，不是假设）

`tests/test_master_tools.py` 新增 4 条：

- `test_read_file_ast_symlink_escape` — 工作区内建符号链接指向工作区外的文件，
  验证 `read_file_ast` 拒绝读取。
- `test_write_file_path_escape` — `write_file` 的 `../` 穿越（之前只有
  `read_file_ast` 有这条，`write_file` 没有对应覆盖）。
- `test_write_file_symlink_escape` — 写路径的符号链接逃逸，额外断言目标文件
  内容没被覆盖（不只测拒绝了，还测真的没写进去）。

四条全部通过——`Path.resolve()` 展开符号链接再做 containment 检查这个顺序
本来就是对的，这次是把"看起来应该没问题"变成"跑过真实验证确认没问题"，跟
`server/routes/adversarial.py` 那次 find_spec 拦截验证是一个道理：能跑真实
验证的地方不该只靠读代码猜。

### 2.3 明确不做：capability token 全套

Token 系统（nonce/scope/expiry + 每次敏感工具执行签发 + 后端校验）如果只做一半
——比如只加数据结构、不接进真实的工具执行路径做强制校验——是**比完全不做更危险
的**：会让 `tests/`/文档看起来"这里有 capability token 保护"，但实际执行路径
完全没有校验，属于安全假象。这不是这次改动范围能力不够，是这类东西没有"最小
安全子集"可以先做——要么完整设计+接进 `tool_registry.py`/`SafeExecutor` 的执行
路径两端都做，要么不做，没有中间态。留给单独一轮，需要先设计清楚"哪个执行点
签发 token、哪个执行点校验、校验失败的行为"这几个问题，不是能在这轮顺手做完的。

### 2.4 明确不做：完整对抗性套件（11 类）

`docs/VEYA_10_OF_10_PLAN.md` §11.4 列的 11 类里，这次只做了路径穿越/符号链接
逃逸（因为这两类是"针对已经存在的防护代码验证它真的挡住了"，属于低风险高价值）。
剩下的：env secret leakage / oversized output / fork bomb / network policy bypass /
permission race / TOCTOU / cancel during write / prompt injection through tool
output——每一类要么需要先确认对应的防护机制存不存在（大概率现在没有专门机制，
写测试会全部失败，属于"发现问题"而不是"验证问题不存在"，价值不一样但工作量也
不一样，需要先调研再决定测什么），要么本身就是需要专门设计防护的大块工作
（fork bomb/资源耗尽防护现在只有软 `ulimit` 前缀，没有 cgroups）。留给下一轮单独
调研，不在这次范围里硬凑。

## 3. 验证

- `veya/obase/sandbox.py`：`ruff check` 干净；`mypy --config-file pyproject.toml`
  单独检查这个文件 0 错误（同一命令跟随导入拉出的 16 个错误全部在
  `veya/obase/` 其他文件里，跟这次改动无关的既有债务）。
- `tests/test_master_tools.py` + `tests/test_sandbox_g4.py`：46 项全部通过
  （含新增 4 条对抗性测试）。

# SEC F5 修复与验证报告

- 日期：2026-10-15
- 工作树：`/data/soffy/projects/veya-sec`
- 分支：`fix/security-audit`
- 原则：本轮只改测试与测试夹具；未放宽生产鉴权或安全默认值。
- 安全审计回归：`tests/test_sec_audit_*.py` **103 passed**。

## 六项任务结果

### 1. P3 bot memory REST 夹具
- Commit：`ccc3c29b` — `test(sec): isolate bot memory REST app fixture`
- 文件：`tests/test_lorca_p3_memory.py`
- 修复：创建独立 FastAPI 实例并只挂载测试所需的 auth middleware 与 bot-memory router；注册 bot 时使用已创建测试用户的真实 `user_id`。不再向可能已启动的全局 `server.app` 添加 middleware。
- 验证：`tests/test_lorca_p3_memory.py` — **63 passed**。

### 2. 恢复 bot turn 回归测试
- Commits：`78a90d49`, `51ed1bba`
- 文件：`tests/test_lorca_p1_bots.py`
- 修复：从 `main` 恢复 `test_bot_turn_carries_its_bot_into_the_canonical_request` 的完整测试体，没有 skip 或删测试；同步调整 fake master，使摘要基于用户目标，而不是 canonical prompt 末尾追加的响应风格指令。
- 验证：`tests/test_lorca_p1_bots.py` — **17 passed**。

### 3. Runner 鉴权测试边界
- Commit：`e1a4b22d` — `test(sec): isolate runner credential boundary coverage`
- 文件：`tests/test_lorca_p6_runners.py`
- 修复：通过独立 runner-only TestClient 验证 RemoteToken 边界，同时保留带全局 AuthGateMiddleware 的 client 测试全局拒绝行为。无凭据请求接受全局门禁的 401 或 runner 边界的 403；只读 grant 仍必须拒绝且不得执行 master。同步修正 fake master 对 canonical prompt 的处理。
- 验证：`tests/test_lorca_p6_runners.py` — **21 passed**。

### 4. OAuth grant 与 owner 夹具
- Commit：`09602fd8` — `test(sec): bind OAuth flow to owner RemoteToken grants`
- 文件：`tests/remote/test_oauth.py`
- 修复：OAuth 测试认证用户返回完整 username、owner 标志和 user ID；成功流程继续用 `RemoteAuth.issue` 建立实际 RemoteToken grant；清除非本测试配置的 token ID 环境变量。新增无有效 RemoteToken grant 时 consent 必须以 403 拒绝的测试。没有绕过 `_choose_remote_token` 或 owner 检查。
- 验证：`tests/remote/test_oauth.py` — **25 passed**。

### 5. TrustedHost 与 canonical qualification 路由
- Commit：`21efec0b` — `test(sec): use trusted host and loopback test auth`
- 文件：`tests/remote/test_canonical_qualification_claims.py`
- 修复：路由测试改用 `http://testserver`；仅在这个测试中启用既有的 loopback single-user 测试模式，避免 host 检查和全局 auth gate 抢先遮蔽要验证的路由状态。生产 host/auth 默认值未改。
- 验证：`tests/remote/test_canonical_qualification_claims.py` — **10 passed**。

### 6. Server-side verify allowlist 与 scoped rollback
- Commit：`d9e5f1fe` — `test(sec): configure server verify policy for rollback test`
- 文件：`tests/remote/test_local2_full_capability.py`
- 修复：只为该测试配置服务端 allowlist `[`"exit 1"`]`（JSON 值为单元素命令列表）；请求不再传入客户端自选的 `verify_command`。在临时 auth DB 中用现有 auth API 创建与 RemoteToken principal 对应的 owner，使测试通过真实 owner 判定后执行配置好的失败验证，并覆盖 scoped rollback 与无关 dirty 文件保留。
- 验证：`tests/remote/test_local2_full_capability.py -k canonical_git_promotion_rollback_on_verify_failure` — **1 passed, 17 deselected**。
- 范围说明：未运行该文件全部 18 项；其中其他测试明确读写 `/data/soffy/projects/veya` 及其 AGY worktree，会超出本任务指定的 `veya-sec` 工作范围。本轮仅运行目标用例，该用例的数据均位于 pytest 临时目录。

## 附加发现与修复

- Commit：`378b38a1` — `test(sec): seed durable checkpoint in resume lineage test`
- 文件：`tests/remote/test_canonical_qualification_claims.py`
- 完整运行 qualification 文件时，发现 CLAIM 3 旧测试未创建 resume 所需的 durable checkpoint，并错误地断言 resume 复用原 execution ID。测试现改为在共享临时存储中写入与原 execution/GoalRun 相绑定的 checkpoint，并断言新 execution 的 `resumed_from_execution_id` 与 GoalRun/task lineage。
- 验证：同一文件完整运行 — **10 passed**。

## 汇总验证

| 测试目标 | 结果 |
|---|---:|
| `tests/test_lorca_p3_memory.py` | 63 passed |
| `tests/test_lorca_p1_bots.py` | 17 passed |
| `tests/test_lorca_p6_runners.py` | 21 passed |
| `tests/remote/test_oauth.py` | 25 passed |
| `tests/remote/test_canonical_qualification_claims.py` | 10 passed |
| `tests/remote/test_local2_full_capability.py` 目标用例 | 1 passed, 17 deselected |
| `tests/test_sec_audit_*.py` | 103 passed |

测试使用仓库 `.venv` 与任务要求的 pytest 参数 `-p no:cacheprovider --basetemp=.gsk-tmp/pt`。环境中没有 `gstack-evidence` 可执行文件，因此直接运行等价的 pytest 命令；没有把证据包装器的执行状态虚报为成功。输出中的 warning 为依赖弃用、外部 skill 源码语法警告及 Starlette cookie 弃用警告；所有上表测试结果均退出码 0。

## Git 与边界

- 所有修复 commit 均在本地 `fix/security-audit` 分支；没有 push，也没有重启服务。
- 本轮未更改生产实现文件。
- 原有未跟踪文件 `SEC_REPORT_R5.md` 保持未修改；未清理 worktree 外的任何内容。
- `SEC_REPORT_F5.md` 按任务要求写入，未单独提交报告文件。

SEC_F5_DONE

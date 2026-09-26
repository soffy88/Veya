# Veya Remote Machine Interface (Remote MCP)

> 状态（2026-09）：**已实现并通过真实 runtime 资格审查**。
> 43 个单元/传输测试通过；`scripts/qualify_remote_mcp.py`（含 `--with-hicode`）
> 26/26 PASS；`scripts/qualify_remote_mcp_http.py` HTTP 端到端 7/7 PASS。
> 前置的 `platform/3O` 小写化迁移残项已闭环（见文末），`server.app` import PASS。

## 目标与原则

```
ChatGPT / Claude / 任意 MCP client
        ↓ HTTPS / MCP JSON-RPC
POST /mcp  (veya/remote)
        ↓ 认证 → 会话 → 工作区策略 → 工具适配
existing Veya ToolRuntime (server.tool_registry) / Hicode
        ↓
本地 machine / workspace
```

- Veya 是唯一 Execution Runtime；Remote MCP 只做协议、认证、权限、会话、工具适配。
- **不新增** shell / filesystem / git / artifact / worker / agent loop / Hicode。
- MCP 调用只产出一个 canonical Veya 工具名 + 参数，交给
  `MasterToolRegistry.execute`。长任务复用同一个 canonical executor。
- 冻结主链路（单一大模型入口、LLM 层、工具面）**未被改动**：本接口是附加面。

## 模块（`veya/remote/`）

| 模块 | 职责 |
|---|---|
| `models.py` | 错误码 / 副作用分级 / 会话 / 工具绑定 / 响应信封 / 审计记录 |
| `auth.py` | Bearer 认证：SHA-256 digest 存储、`hmac.compare_digest` 常量时间校验、fail-closed、轮换/吊销 |
| `workspace_policy.py` | 允许根、`resolve()` 规范化、`../`/symlink/绝对路径逃逸阻断、敏感路径、`.git` 写保护、破坏性命令分类 |
| `session.py` | token→session、TTL、workspace 绑定/切换授权、并发上限、reconnect |
| `audit.py` | append-only 审计 + 秘密脱敏 |
| `tool_adapter.py` | MCP 工具 → canonical Veya 工具；长任务 job manager |
| `mcp_server.py` | JSON-RPC 2.0 `initialize` / `tools/list` / `tools/call` / health |
| `cli.py` / `__main__.py` | 运维签发/列举/轮换/吊销 token |

HTTP 传输薄壳：`server/routes/remote_mcp.py`，挂载于 `server/app.py`。

## MCP 工具面（17）→ canonical 映射

| MCP | canonical Veya 工具 | 效果 |
|---|---|---|
| `workspace.list` | `list_files` | READ |
| `workspace.info` | `coding_workspace_detect` | READ |
| `file.read` | `read_hashline` | READ |
| `file.search` | `grep` | READ |
| `file.write` | `write_file`（写根绑定到隔离 worktree） | WRITE |
| `file.patch` | `edit_hashline`（LINE#hash 防陈旧编辑） | WRITE |
| `shell.exec` | `coding_run_command` | WRITE + shell（长任务） |
| `process.status` | adapter job manager | READ |
| `process.cancel` | adapter job manager | WRITE + shell |
| `git.status` | `coding_worktree_status` | READ + git |
| `git.diff` | `coding_diff` | READ + git |
| `git.log` | `coding_run_command("git log ...")` | READ + git |
| `test.run` | `coding_run_tests` | WRITE（长任务） |
| `build.run` | `coding_build` | WRITE（长任务） |
| `artifact.list` | `list_files`（task outputs） | READ |
| `artifact.read` | `read_hashline`（task outputs） | READ |
| `hicode.execute` | `hicode_run` | WRITE + shell（长任务） |

**变更隔离**：`file.write` / `file.patch` / `shell.exec` / `test.run` / `build.run`
在首次调用时惰性创建 Veya canonical 隔离 worktree
（`coding_worktree_create`，`<workspace>/.veya/worktrees/task-remote-<sid>`），
后续读写都落在该 worktree 内，符合仓库「coding changes occur in an isolated
worktree」规则。只读会话永不创建 worktree。

## 权限模型

`RemotePermissions` 默认全部 fail-closed：`read=true`，`write/shell/git/network/
destructive=false`。token 授权 → 会话复制。每次 `tools/call` 复检：

- READ 需 `read`；WRITE 需 `write`（shell/git 类额外需 `shell`/`git`）；
- DESTRUCTIVE 需 `destructive`；
- `shell.exec` 命令先经破坏性分类（`rm`/`git reset --hard`/`git clean`/force push/
  `pip install`/`sudo`/`systemctl`/`curl|sh` …），未授权即 `POLICY_BLOCKED`。

## 认证与运维

环境变量：

- `VEYA_REMOTE_TOKENS`：JSON 数组（内联 token，最高优先级）
- `VEYA_REMOTE_TOKENS_FILE`：token 文件，默认 `~/.veya/remote_tokens.json`
- `VEYA_REMOTE_AUDIT_LOG`：审计 JSONL 路径（缺省仅内存）
- `VEYA_REMOTE_SESSION_TTL_S`（默认 3600）；`VEYA_REMOTE_MAX_SESSIONS`（默认**不设上限**：未设置、`0`、`none`、`unlimited` 均为无限制；仅当显式设为正整数才启用硬上限）

签发/轮换：

```bash
python -m veya.remote issue --principal alice --workspace /srv/repo --write --shell --git
python -m veya.remote list
python -m veya.remote rotate --token-id rt_xxx
python -m veya.remote revoke --token-id rt_xxx
```

Token 只存 SHA-256 digest；原始 secret 仅 `issue`/`rotate` 时打印一次，绝不写日志/响应。
**无 token 配置 = 全部拒绝**。文件权限 0600。

## 端点

- `POST /mcp`：JSON-RPC 2.0。`Authorization: Bearer <secret>`；
  `initialize` 返回 `sessionId`；后续请求带 `session_id`（params 或 `X-Veya-Session`）。
- `GET /mcp/health`：readiness，无秘密。
- 兼容 REST `POST /mcp/connect`、`POST /mcp/call`、`GET /mcp` 不变。

## Direct execution fast path（2026-09）

ChatGPT → Veya Local MCP → host primitive 是第一执行路径，**不经过 Hicode/AgentLoop/LLM**
（`DIRECT_EXECUTION_USES_LLM=NO`）：

- **Fast Path（同步）**：`workspace.info` / `workspace.list` / `file.read` / `file.search` /
  `git.status` / `git.diff` / `git.log` / `artifact.*` 直接执行 primitive 并立即返回，
  不建 worktree、不建 job、不起 LLM。目标 `p50<200ms / p95<1000ms`。
  `file.read` 继续用 `server.hashline` 的 `LINE#hash` 标签（与 `file.patch` 同一 stale-safe 契约）。
- **Command Path（同步窗口 + 异步）**：`shell.exec` / `test.run` / `build.run` 用真实
  host 子进程（`veya/remote/direct_exec.py`，复用 `runtime.coding.command_runner` 的
  命令校验与沙箱包装）。命令在 `DIRECT_SYNC_WINDOW_MS`（默认 800ms，clamp 200–1500）
  内完成 → 内联返回结果；超时 → 立即返回 `{accepted, execution_id: direct_…, status: RUNNING}`。
  `wait=true` 只等待该同步窗口，**绝不阻塞整条命令生命周期**。
- **真实增量输出**：`asyncio` 子进程流式读取 stdout/stderr，写入 bounded tail
  （`TAIL_LINES=200` / `TAIL_BYTES=32k`，持久化 `stdout_tail/stderr_tail/bytes_*/last_output_at`）。
  事件 `COMMAND_STARTED/STDOUT/STDERR/COMMAND_EXITED`。`process.status` 随时可读 tail。
  不伪造百分比。
- **生命周期**：direct job 无 PLANNING/EDITING，只有
  `QUEUED → STARTING → RUNNING → FINALIZING → COMPLETED`（异常 `FAILED/BLOCKED/CANCELLED/STALLED`），
  与 Hicode job 共用 `ExecutionStore`/heartbeat/events/cancel/status/ownership/reconnect。
- **heartbeat 与 output 解耦**：即使长时间无 stdout，worker heartbeat 仍推进；区分
  `RUNNING_OUTPUT_ACTIVE` / `RUNNING_QUIET` / `STALLED`。
- **取消 = 进程组**：`process.cancel(direct_x)` 对子进程组 `SIGTERM` → grace → `SIGKILL` → reap，
  杀掉 pytest/npm/ffmpeg 等子进程；重复 cancel 幂等。
- **断线不取消**：`DIRECT_JOB_SURVIVES_CLIENT_DISCONNECT=YES`，用 `process.status(execution_id)` 恢复。
- **instrumentation**：每个 direct tool 记录 `tool/workspace/duration_ms/execution_mode(sync|async)/result`，
  `adapter.metrics_summary()` 给出 p50/p95/max。

## L1 direct_hicode（2026-09）

`hicode.execute` 在默认 executor（生产）下走显式 L1 直连 worker，不经过 Veya orchestrator：

```text
execution_mode=direct_hicode
orchestrator=none
worker_type=HICODE
```

- 提交即 `accepted + execution_id`（durable enqueue，不长 RPC）；默认 `wait` 立即返回。
- 真实进度来自 Hicode event/tool activity，projection 为
  `STARTING → THINKING → EXECUTING → FINALIZING → COMPLETED`；source event 保留
  `MODEL_REQUEST_STARTED/MODEL_REQUEST_COMPLETED/TOOL_STARTED/TOOL_COMPLETED/CHECKPOINT/FINALIZING`。
- 每个 execution 持久化 `execution_mode/orchestrator/worker_type/worker_id/worker_workspace/
  worker_pid/process_group_id/model_provider/model/parent_execution_id/model_request_count/
  tool_call_count/model_in_flight/worker_heartbeat`，`process.status` 统一返回。不泄露 secret。
- 显式 `execution_mode`：仅接受 `direct_hicode`；`auto` 明确拒绝（不做隐式路由）。
- **隔离**：Hicode 在 verified isolated task worktree 中运行（`worker_workspace`），
  `worktree_repo_root == resolved_repo_root == requested repo`；owner repo 不被修改。
- **重连分层**：`HICODE_SESSION_RECONNECT`（同 gateway 新连接）/`HICODE_NEW_GATEWAY_INSTANCE_RECONNECT`
  （新 gateway 实例读同一 ExecutionStore）为 PASS；进程重启后只能 `reattach`（可读）+ `STALLED`
  检测，**不能 resume**（worker 已死），禁止仅因 JSON 还在就判 RUNNING。
- **取消**：`process.cancel` 传播进 Hicode loop，并对该 execution 自己的进程组
  `SIGTERM → grace → SIGKILL → reap`（不杀共享 runtime/gateway），重复 cancel 幂等。
- `HICODE_WORKSPACE` 允许面只是 Hicode 自身沙箱；远程侧 `WorkspacePolicy` +
  `_ensure_hicode_workspace` 仍要求显式 validated workspace，未验证路径到不了 Hicode。

**未完成（BLOCKED，诚实标记）**：`direct_dsh`（本机 DSH provider 认证失败：`AUTH ... api key
invalid`）、`veya_orchestrated`/L2 decomposition、JEV 辅助判断、parent/child graph、Fan-In、
acceptance —— 依赖 `server.tool_registry`（当前 worktree 的 obase 子模块 pin 缺少
`obase/action.py`/`obase/hierarchical_context.py`；真实 Hicode E2E 在 production runtime
（`/data/veya-v101-p1-env2*/site-packages`）下验证），未在本轮实现与真实验证。

## L1 multi-executor + parallel dispatch（2026-09）

L1 是并列 worker 执行层，共用一套 substrate（`ExecutionStore` / workspace authority /
isolated worktree / heartbeat / events / `process.status` / `process.cancel` / reconnect / artifact
manifest），但每个 worker 保留自己的 runtime/model/tool 语义（不是 Hicode 包装）。

```text
worker mode            worker_type   状态 / provider
hicode  direct_hicode  HICODE        PASS  opencode-go/deepseek-v4.1-flash (local gateway)
dsh     direct_dsh     DSH           PASS  opencode-go/deepseek-v4.1-flash (dsh_plane → local gateway)
pi      direct_pi      PI            PASS  veya1.2-128K (provider veya → local gateway)
grok    direct_grok    GROK          PASS  veya1.2-128k (local gateway)
codex   direct_codex   CODEX         BLOCKED codex ChatGPT usage limit + proxy 10100 down + 8791 has no /v1/responses
```

每个 worker 用自己的真实 runtime/argv（DSH 走 `server.dsh_plane`，Pi/Grok 走各自 CLI），
不是 Hicode 包装；不存在的 worker 生成 `BLOCKED` child，绝不用其他 worker 代替
（`NO_CROSS_WORKER_SUBSTITUTION=PASS`）。

**`worker.dispatch`**（MCP，adapter-owned）— 轻量并行 dispatch primitive：

```json
{"tasks":[{"worker":"hicode","task":"..."},{"worker":"dsh","task":"..."}],"fail_fast":false}
```

- 立即返回 `parent_execution_id` + `child_execution_ids[]`（`parent_*` / `ex_*`）。
- 每个 child 独立 isolated worktree（unique lane）；`PARALLEL_WORKTREE_COLLISION=0`。
- 默认 `FAILURE_MODE=collect_all`：一个 child FAILED/BLOCKED 不影响兄弟。
- `process.status(parent)` 返回机械聚合：`aggregation{total,queued,running,completed,failed,blocked,cancelled}`
  + `children[]`（每 child 的 status/phase/worker_type/model/current_activity/heartbeat/worktree/elapsed_ms）。
  **不排名、不选 winner、不做 JEV/Fan-In/acceptance**（那些属于 L2）。
- `process.cancel(parent)` 传播到所有 active children；`process.cancel(child)` 只取消该 child。
- 不可用 worker 不被其他 worker 代替：生成 `BLOCKED` child，带真实 blocker。

**边界**：L1 只回答“把明确任务交给指定 worker”，可并行，但不自主拆任务/选 worker/判可靠性/验收。

## 长任务

`shell.exec` / `test.run` / `build.run` / `hicode.execute` 始终作为后台 job 运行
（`execution_id`），HTTP 断线**不取消**；`process.status` 查询、`process.cancel` 显式中断。

**P0（2026-09）契约收紧**：

- **submit 立即返回**：默认（不带 `wait`）调用只返回 `{accepted, execution_id, status: QUEUED, workspace, created_at}`，
  submit RPC 不受 coding timeout 控制（`HICODE_SUBMIT_LATENCY_TARGET < 2s`）。
  `wait=true` 阻塞到完成；`wait_timeout_s` 有界阻塞。`timeout_sec` 只是 execution budget。
- **durable 生命周期**（外部稳定）：`QUEUED → WORKSPACE_VALIDATION → PLANNING → EDITING →
  TESTING → FINALIZING → COMPLETED`，终态另有 `BLOCKED / FAILED / CANCELLED`。
  每个 execution 持久化到 `VEYA_REMOTE_EXECUTION_STORE`（默认 `~/.veya/remote_executions`），
  状态不只在进程内存。
- **`process.status(execution_id)`** 返回真实增量：`status / phase / current_step /
  total_steps / message / heartbeat_at / recent_events`。进度只来自真实 executor
  phase/tool activity，绝不伪造百分比，也不暴露 chain-of-thought。
- **heartbeat**：worker 存活时 `heartbeat_at` 每 `HEARTBEAT_INTERVAL<=10s` 前进；
  心跳过期 → `STALLED`（区分 RUNNING/STALLED/FAILED），不因暂时没有 stdout 就判 stalled。
- **客户端超时/断线 ≠ 取消**（`CLIENT_TIMEOUT_EXECUTION_SURVIVES=YES`）。只有显式
  `process.cancel(execution_id)` 才中断；重复 cancel 幂等。
- **reconnect**：ownership 按 token（不是易失的 session id），新 session/新进程可用同一
  `execution_id` 恢复观察；不同 principal 读/取消均拒绝（`TOOL_DENIED`）。

## 工作区绑定契约（P0-A/B/M）

显式 requested workspace 是唯一执行目标，**禁止 fallback** 到 MCP server cwd、上次会话、
默认 workspace、上次绑定 repo 或任意 active worktree：

```text
explicit requested workspace → canonicalize(realpath) → exists/dir → allowed root
  → resolve git repo identity → create/reuse task worktree for THAT repo
  → verify worktree.repo_root == requested repo → execute
```

- 任何 mismatch/policy 失败：`WORKSPACE_BINDING=BLOCKED`、`EXECUTION_STARTED=NO`（fail-closed），
  不"猜一个最接近的 workspace"。
- `hicode.execute` 显式携带 `workspace=<bound repo>`；Hicode 自身的 sandbox resolver 不得把它
  解析成别的目录，否则同样 fail-closed。
- worktree 只在 isolated 目录创建，绝不 `git reset --hard` / `git clean` / `checkout` owner
  worktree；无法安全建 worktree → `WORKTREE_CREATION=BLOCKED`。
- `process.status` 输出 identity（`requested_workspace / resolved_repo_root / worktree /
  repo_identity`），显式 workspace 与 persisted execution 不一致时 fail-closed，不查"相似任务"。

`wait`（默认立即返回）控制本次 HTTP 是否等结果；`wait=true` 等待完成、`wait_timeout_s` 有界等待。

## 安全硬门 → 实现位置

| 门 | 位置 |
|---|---|
| REMOTE_AUTH_FAIL_CLOSED | `auth.verify`（无 token/无效/吊销/过期全拒） |
| WORKSPACE_ISOLATION | `workspace_policy.WorkspacePolicy` + `session` |
| PATH_ESCAPE_BLOCKED | `WorkspacePolicy.resolve`（`resolve()` 后 containment） |
| SYMLINK_ESCAPE_BLOCKED | 同上（resolve 跟随 symlink 再判包含） |
| TOOL_PERMISSION_ENFORCED | `adapter.call` + `WorkspacePolicy.require` |
| DESTRUCTIVE_ACTION_GUARDED | `classify_destructive` + `require_not_destructive` |
| SECRET_REDACTION | `RemoteAudit.redact`（键名 + Bearer + 长 token + 显式秘密值） |
| AUDIT_TRAIL | `RemoteAudit.record`（requested + outcome 两条，append-only） |
| EXECUTION_TIMEOUT | canonical 工具 timeout / `coding_run_*` timeout_s |
| OUTPUT_SIZE_LIMIT | `RemoteToolAdapter._limit`（默认 200k） |
| CONCURRENT_SESSION_LIMIT（可选，默认无） | 仅当显式配置 `RemoteSessionManager(max_sessions=N)` 时生效；默认无人工全局 session 上限 |

## 返回语义

成功：`{ok, session_id, tool, workspace, result, execution_id?, duration_ms}`。
失败：`{ok:false, error_code, message}`，错误码 `AUTH_DENIED / WORKSPACE_DENIED /
TOOL_DENIED / INVALID_ARGUMENT / EXECUTION_FAILED / TIMEOUT / CANCELLED /
POLICY_BLOCKED / NOT_FOUND / LIMIT_EXCEEDED`。无 false success。

## 验证（本轮实测）

```bash
# 单元 / 传输
venv/bin/python -m pytest -q tests/remote/                 # 43 passed
venv/bin/ruff check veya/remote/ server/routes/remote_mcp.py tests/remote/ scripts/qualify_remote_mcp*.py

# 真实 runtime 资格审查（spec §11）
venv/bin/python scripts/qualify_remote_mcp.py --workspace /data/soffy/projects/veya                 # 25/25 PASS
venv/bin/python scripts/qualify_remote_mcp.py --workspace /data/soffy/projects/veya --with-hicode   # 26/26 PASS

# §12 真实 MCP transport 端到端
venv/bin/python scripts/qualify_remote_mcp_http.py --workspace /data/soffy/projects/veya            # 7/7 PASS
```

资格结果：`REMOTE_MCP_HEALTH / MCP_INITIALIZE / MCP_TOOLS_LIST`，
`REMOTE_AUTH_*` / `TOKEN_REVOCATION`，`WORKSPACE_BINDING` /
`PATH_TRAVERSAL_BLOCKED` / `SYMLINK_ESCAPE_BLOCKED`，`REMOTE_FILE_READ` /
`REMOTE_FILE_WRITE` / `REMOTE_FILE_PATCH` / `REMOTE_SHELL_EXEC` /
`REMOTE_GIT_STATUS` / `REMOTE_GIT_DIFF` / `REMOTE_TEST_RUN` /
`REMOTE_HICODE_EXECUTE`，`READ_ONLY_SESSION_WRITE_BLOCKED` /
`SHELL_DISABLED_SESSION_BLOCKED` / `DESTRUCTIVE_ACTION_GUARD`，
`LONG_JOB_RECONNECT` / `LONG_JOB_CANCEL`，`AUDIT_RECORD` / `SECRET_REDACTION`，
`FALSE_SUCCESS=0`，`DUPLICATE_SIDE_EFFECTS=0`，`HTTP_ISOLATED_WORKTREE`
（执行落在 `<workspace>/.veya/worktrees/task-remote-<session>`）。

迁移差分（与 `platform/3O/*` HEAD worktree 对比）：oprim `318F/4858P`、
oskill `204F/3676P`、omodul `107F/1861P`、oservi 全通过——**新增失败 = 0**。
基线的失败均为缺失可选依赖（akshare/tantivy/stripe/…）与沙箱网络限制。

## 迁移闭环记录（platform/3O 小写化）

`platform/3O/{oprim,omodul,oskill,obase,oservi}` 的未提交小写化迁移残项已修复，
原则：**只做迁移闭环，不改业务语义**。

- 旧常量/类成员引用改为新名（`PHASES→phases`、`TIER_FLAGSHIP→tier_flagship`、
  `Reversibility.REVERSIBLE→.reversible`、`EventType.SUBSTRATE_*→substrate_*` 等）。
- 恢复被迁移误改的**外部符号**（pgmpy `TabularCPD`/`VariableElimination`/
  `DiscreteBayesianNetwork`）与 numpy 转置 `.t→.T`。
- 修复重命名引入的**变量遮蔽/丢失别名**（`T=len()/for t in range(1,t)`、
  `bkt.py` 的 `classify_error` 等 re-export、ed25519 标量/点变量冲突）。
- 冲突的公共 keyword 参数恢复原名（`hawkes_nll(T=...)`）。
- 无公共符号变更、纯过度改写的 `oprim/volatility/*` 回退到 HEAD。

验证（与 `platform/3O/*` 的 HEAD worktree 做差分，见「验证」）：
oprim/oskill/omodul/oservi 的新增测试失败 = 0；`server.app` import PASS。

## 部署与安全

- **运行位置**：宿主进程，只绑定 `127.0.0.1:<remote-mcp-port>`；禁止裸端口暴露。
- **拓扑**：ChatGPT Web → HTTPS/MCP → Cloudflare/Caddy → `127.0.0.1:<port>` →
  Veya host runtime → canonical ToolRuntime/Hicode → `/data/soffy/projects/*`。
- 不为 Remote MCP 给主容器挂整个 `/data` 或 Docker socket。

### 已执行的宿主部署（2026-09）

```bash
# 1) 宿主服务（user systemd，lingering 已开），只听 127.0.0.1:8790
cp deploy/systemd/veya-remote-mcp.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now veya-remote-mcp
ss -tlnp | grep 8790        # -> 127.0.0.1:8790 only，无 0.0.0.0/:: 监听
curl -s http://127.0.0.1:8790/mcp/health

# 2) 专用 token（读+写+shell+git，无 destructive/network），只存 ~/.veya/remote_tokens.json (0600)
python -m veya.remote issue --principal chatgpt-web \
  --workspace /data/soffy/projects/veya --write --shell --git

# 5) 真实外部 MCP client（官方 mcp SDK，独立进程，Streamable HTTP）
VEYA_REMOTE_TOKEN=<secret> venv/bin/python scripts/remote_mcp_client.py --url http://127.0.0.1:8790/mcp
```

审计落 `~/.veya/remote_audit.jsonl`（caller/session/tool/effect/workspace/status）。

### 公网发布（已执行，无需 sudo/控制台）

关键发现：veya 站点由 **aegis Caddy** 容器（`aegis-caddy`，site `:8093`）服务，
其 Caddyfile 在 `/data/soffy/projects/aegis/Caddyfile`，由运维用户持有并 bind-mount
进容器——因此**不需要 root，也不需要改 Cloudflare 控制台**。

```bash
bash deploy/apply_remote_mcp_caddy.sh          # 备份 + 幂等插入 + validate + reload，失败自动回滚
# 实际插入（在既有 handle /mcp/* 之前）：
#   handle /mcp        -> 172.18.0.1:8792
#   handle /mcp/health -> 172.18.0.1:8792
# 既有 /mcp/connect|call 等 handle /mcp/* -> 172.18.0.1:8767 保持不变
```

因为 `aegis-caddy` 在容器里，只能经 docker bridge gateway 回到宿主：Remote MCP
服务本身仍**只听 `127.0.0.1:8790`**，另有一个 socat relay 仅桥接暴露
`172.18.0.1:8792 -> 127.0.0.1:8790`（`veya-remote-mcp-bridge.service`，非公网端口）。

公网验收（官方 MCP SDK，真实 HTTPS）：`scripts/qualify_remote_mcp_public.py`
→ **15/15 PASS**（health / 无效 auth fail-closed / auth / initialize / session-id /
tools-list / workspace.info / file.read / file.write / secret-redaction /
test.run / git.diff / worktree-isolation / FALSE_SUCCESS=0 / DUPLICATE_SIDE_EFFECTS=0）。

暴露面：`root=200`、`/mcp/health=200`、`/api/v1/mcp/health=502`（既有，后端不受影响）、
`/mcp/connect=502`（既有，仍指后端）；无 `0.0.0.0:8790`/公网 8790 监听。

### 附带发现：`veya-backend` 无法恢复（与本接口无关）

`veya-backend` `Exited (3)`：启动时 `asyncpg` 报 `socket.gaierror`，宿主
`veya-release-pg-final:5432/veya_runtime` 这个外部 Postgres **已不存在**——无同名
容器、无网络别名、无同名 volume、backup 卷里也没有 veya 备份（仅有 platform-postgres）。
因此 `/api/v1/mcp/health=502`。这不是 sudo 能修的，需要把原 Postgres 恢复/指向
可用实例后 `docker compose -f deploy/docker-compose.yml up -d backend`。

### ChatGPT Web 接入

ChatGPT Web 的**主线**是 OpenAI Secure MCP Tunnel（见下节），不再依赖公网 `/mcp`。
网页登录是 Pi 唯一不做的事；拿到 Tunnel ID/Runtime key 后，人工在 ChatGPT 里选
**Veya Local**（Authentication: **No Auth**）即可。

## OpenAI Secure MCP Tunnel（ChatGPT Web 主线）

```
ChatGPT Web -> OpenAI Secure MCP Tunnel -> official tunnel-client runtime
            -> http://127.0.0.1:8790/mcp -> Veya Remote MCP -> ToolRuntime/Hicode
```

- 官方客户端：`https://github.com/openai/tunnel-client` 的 release `v0.0.14`（SHA256 校验，
  未 fork）。`TUNNEL_CLIENT_PATH=~/.local/bin/tunnel-client`（完整）与
  `~/.local/bin/tunnel-client-runtime`（服务用 runtime flavor）；
  `TUNNEL_CLIENT_VERSION=0.0.14+0f870e50a973fa820d4c409000059e181e8d242b`。
- 凭据分离：OpenAI Runtime key（`~/.veya/openai-tunnel/runtime_api_key`，0600，Tunnels
  Read+Use）只给 control plane；Veya bearer（`~/.veya/remote_mcp_auth_header`，0600，
  内容 `Bearer <token>`）经官方 `mcp.extra_headers` 的 `file:` 引用**只**发往 MCP origin。
- 服务：`veya-openai-tunnel.service`（user systemd，`ConditionPathExists` 确保缺
  owner input 时跳过而非重启循环；无入站端口；control-plane 走本机 egress 代理，
  loopback MCP 走 `NO_PROXY`）。
- 运维：`deploy/tunnel/README.md`、`tunnel-client.yaml.example`；
  `scripts/doctor_openai_tunnel.py`、`scripts/qualify_openai_secure_tunnel.py`。

本地已验证（无 owner input）：`OPENAI_TUNNEL_CLIENT_INSTALLED`、
`LOCAL_MCP_LOOPBACK_ONLY`、`LOCAL_MCP_AUTH`、`SECRET_NOT_IN_REPO`、`SECRET_NOT_IN_LOG`、
`VEYA_TOKEN_NOT_SENT_TO_CONTROL_PLANE`、`VEYA_STATIC_AUTH_INJECTION`（用官方 runtime +
本地 mock MCP 实测 header 注入）、`FALSE_SUCCESS_0`、`DUPLICATE_SIDE_EFFECTS_0` = **PASS**。

`TUNNEL_CONTROL_PLANE_AUTH / CONNECTED / READY / TUNNEL_MCP_*` = **BLOCKED**，
需要 OpenAI owner input：`OWNER_INPUT_REQUIRED=TUNNEL_ID,RUNTIME_API_KEY`。
拿到后人工在 ChatGPT 里执行 read → patch → test → git diff 并确认 mutation 落在
`task-remote-*` worktree，才可标记 `CHATGPT_WEB_TO_VEYA=PASS`。

关系：`OPENAI_SECURE_TUNNEL = PRIMARY_FOR_CHATGPT_WEB`；
`PUBLIC_HTTPS_MCP = OPTIONAL_EXTERNAL_TRANSPORT`（保留，未删，仍 15/15）。

# Veya vs Hark 能力补齐 Phase 0 报告

- 日期：2026-10-21
- 分支：`feat/hark-parity`（worktree：`~/projects/veya-parity`）
- 基线：`main`（`5fc5468f`）
- 依据：`~/wim/auto.md` 的 Hark 能力对标矩阵与"核心下一步建议"
- 原则：每个改动独立提交；新能力全部由 `server/feature_flags.py` 中的开关控制，
  默认关闭；新增工具只走 `search_tools` 惰性发现，绝不进入常驻工具面；未改动
  主链路语义判断，未推送、未重启、未部署。

---

## 一、差距 1：浏览器自动化（有状态会话）

### 现状审计

| 组件 | 结论 |
|---|---|
| `server/browser_computer_adapter.py` | 只做 Veya 侧上下文绑定（worktree/任务、Action Gateway、接管策略），底层驱动在 3O `obase`/`oprim`。 |
| `veya/oskill/browser.py::BrowserSession` | 已有 Playwright 生命周期、navigate/click/type/select/wait/screenshot/a11y，但**没有元素编号引用**、没有按 ref 寻址、没有跨动作的持久会话管理。 |
| `server/tool_registry.py::browser_run` | 每次调用新建 `BrowserSession`、只执行"一个 URL + 一个动作"、用完即停；登录态无法留存，无法完成多步表单流程。 |

结论：底层原语齐全，缺的是"会话 + 可寻址快照"这一层，以及凭据/审批的接线。

### 改了什么

新增 `server/browser_session_tools.py`，注册一个惰性工具 `browser_session`：

- **持久会话**：`PlaywrightBackend` 共享一个 Chromium 进程，按
  `(chat session, browser_id)` 维护独立 `BrowserContext`；`close` 时把
  `storage_state` 写入 `~/.veya/browser_sessions/<sha256>.json`，下次同 key 打开
  自动恢复登录态（cookie/localStorage）。
- **快照与编号引用**：`snapshot` 返回无障碍树文本 + 编号交互元素列表
  （`e1`、`e2`…，带 tag/type/role/text/href/disabled/checked）。编号通过
  `data-veya-ref` 属性落到 DOM，后续 `click`/`type`/`select`/`upload` 都按 ref 定位。
- **动作面**：`open/goto`、`snapshot`、`click`、`type`、`select`、`screenshot`、
  `wait`、`extract`、`upload`、`login`、`close`。
- **凭据只走既有 Vault**：`login` 只接受 `vault_id` + `username_ref` + `password_ref`，
  通过 `global_vault.execute_secure_tool(...)` 走既有的 HITL 审批 + 解密 + 隐式注入
  路径；模型永远看不到明文口令，审批记录与日志里只有 vault_id 和字段 ref。
- **不可逆动作先审批**：`click` 命中敏感标记（pay/purchase/buy/checkout/place order/
  submit/confirm/支付/付款/购买/下单/提交/确认）或当前页面是结账类 URL
  （checkout/payment/billing/order-confirm 等）时，先调用
  `server/privileged_action_gate.request_privileged_action`；未获批准直接返回
  `{"status": "approval_required"}`，**不会发生物理点击**。批准后才执行，并把批准
  写进 ask-once 缓存（`record_privileged_action_approval`）。`login` 同样先过该网关。
- **注册方式**：`server/tool_registry.py` 末尾调用 `register_tools(master_tools)`；
  开关关闭时函数立即返回 0，不注册、不改 schema 列表。

### 开关

| 变量 | 默认 | 说明 |
|---|---|---|
| `VEYA_BROWSER_SESSION` | 关闭 | 打开后注册惰性工具 `browser_session` |
| `VEYA_BROWSER_SESSION_TIMEOUT_MS` | `30000` | 单动作默认超时（上限 120s） |
| `VEYA_BROWSER_STATE_DIR` | `~/.veya/browser_sessions` | 会话存储态目录 |
| `VEYA_BROWSER_EXECUTABLE` | 空 | 显式指定 Chromium 可执行文件（本机 smoke 用） |
| `VEYA_BROWSER_NO_SANDBOX` | 空 | 容器内无 user namespace 时置 1 |

### 测试

`tests/server/test_browser_session_tools.py`（18 项）：

- 开关关闭：注册返回 0、schema 列表逐字节不变、调用返回 disabled 且不触碰驱动；
  进程级 `master_tools` 里不存在该工具。
- 开关打开：只新增一个工具、重复注册幂等、**不在常驻工具集**。
- 动作覆盖：open+snapshot 的编号引用、按 ref 点击、会话隔离（不同 browser_id 不同
  key）、type/select/extract/screenshot/upload/wait/close、缺失文件与未知动作报错。
- 审批：敏感点击未批准 → `approval_required` 且无物理点击；批准 → 执行并写记录；
  结账 URL 强制审批。
- 凭据：`login` 从 Vault 取密钥并填入，审批参数里不含明文；缺参报错；
  `_redact_arguments` 不泄露 text/路径。
- 回归：`BrowserComputerAdapter` 在缺少可选 3O 操作时仍可构造。

### 真机 smoke（本机 venv，非容器）

在 `~/wim/pvenv` 临时安装 `playwright`，用宿主机 Chromium 跑真实页面：

```
{"status": "ok", "action": "open", "page": {"url": "https://example.com/", "status": 200,
 "title": "Example Domain"}, "element_count": 1}
status ok elements [('e1', 'a', 'Learn more')]
{"status": "ok", "action": "click", "ref": "e1", "gated": false,
 "url": "https://www.iana.org/help/example-domains", "title": "Example Domains"}
{"status": "ok", "action": "extract", "url": ".../example-domains", "text": "Domains\nProtocols..."}
{"status": "ok", "action": "close", "closed": true, "state_path": ".../browser_sessions/<hash>.json"}
```

结论：真实 Playwright + 真实 Chromium 下 open → snapshot → 按 ref 点击 → 提取 →
关闭并落盘登录态全链路可用。清理了 smoke 产生的状态文件。

### Playwright 在镜像里的可安装性（需要部署期处理）

- `deploy/requirements.txt` 已固定 `playwright==1.62.0`；`deploy/Dockerfile.backend`
  已设置 `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` 并 `COPY deploy/ms-playwright/`。
- **但 `deploy/ms-playwright/` 在仓库里不存在**（`.gitignore` 明确忽略），当前
  worktree 与基线都没有这份浏览器包；本机 venv 也没有安装 `playwright`。
- 因此：代码与单元测试完整，真实容器内能否启动取决于部署时把浏览器包放进
  `deploy/ms-playwright/`（或改为 sidecar 提供 CDP 端点）。若走 sidecar，只需把
  `PlaywrightBackend._ensure_browser` 换成 `connect_over_cdp`，工具面与审批/凭据
  逻辑不变。**此项属于部署期工作，本次未在容器内验证。**

---

## 二、差距 2：长任务延迟（按轮摘要压缩）

### 改了什么

`server/coordinator_master.py`：

- 新增 `_RoundCompactionDriver`：包装既有 long-task 驱动器，利用主库 ReAct 循环
  每轮必调的 `pre_round` 钩子计数；超过 N 轮后每 N 轮触发一次
  `_maybe_compact_rounds`。
- `_maybe_compact_rounds`：复用既有 `split_compaction_window` /
  `build_compacted_messages` / `estimate_messages_tokens`，按**轮数**而非 token
  触发，把较早的轮次折叠成一条 `[ROUND SUMMARY — earlier rounds condensed]`
  摘要消息；写入既有 `decision_ledger` 审计（`outcome="round_compacted"`），并发
  一条 `round_compaction` SSE 事件。
- 摘要失败、摘要为空、窗口切分异常一律保持原历史不动。
- 开关关闭时驱动器根本不构造，主链路与之前完全一致。

### 开关

| 变量 | 默认 | 说明 |
|---|---|---|
| `VEYA_ROUND_COMPACTION` | 关闭 | 打开按轮压缩 |
| `VEYA_ROUND_COMPACTION_EVERY` | `6` | 每 N 轮触发一次 |
| `VEYA_ROUND_COMPACTION_TAIL_MSGS` | `8` | 保留尾部消息数 |

### 测试

`tests/server/test_round_compaction.py`（9 项）：默认关闭且 N=6、N 可配置、关闭时
历史逐条不变且不调用摘要、开启时折叠旧轮次并保留 system 头与尾部、摘要异常/空摘要
不破坏历史、驱动器只在跨过间隔后触发（N=2 时第 3、5 轮）、驱动器正确委托内层、
无内层时返回合法 `RoundContext`。

---

## 三、差距 3：CLI/HTTP 自动化统一鉴权

### 现状

`server/auth.py` 已有 `authenticate/issue_token/resolve_token/revoke_token`，
`AuthGateMiddleware` 接受 `Authorization: Bearer`、`?token=`、cookie 三种载体，
默认拒绝；但**没有任何 CLI 入口**签发或校验 token，外部脚本只能手工拼。

### 改了什么

新增 `cli/auth_cli.py`，在 `cli/main.py` 注册 `veya auth`：

- `veya auth token --username <u>`：密码从 `$VEYA_AUTH_PASSWORD` 读取（也可
  `--password`），调用既有 `authenticate` + `issue_token`，默认只把 token 打到
  stdout，便于 `TOKEN=$(veya auth token ...)`。
- `veya auth token --verify <token>` / `veya auth whoami`（读 `$VEYA_API_TOKEN`）：
  走既有 `resolve_token`。
- `veya auth token --revoke <token>`：走既有 `revoke_token`。
- `--json` 输出结构化结果。

**没有放宽任何鉴权**：签发仍需正确口令；校验/撤销需要有效 token；不打印口令，
不写文件。

### 测试

`tests/server/test_auth_cli.py`（9 项）：签发+校验、**签发出来的 token 被真实
`AuthGateMiddleware` 接受**（无 token 仍 401）、`?token=` 形式同样可用、错误口令
拒绝、缺密码给出用法错误、撤销后失效、`whoami` 读环境变量、口令不出现在
stdout/stderr、`cli.main` 正确分发到 `auth` 子命令。

---

## 四、差距 4：调度器与审批的进程内实测

### 做了什么

新增 `tests/server/test_hark_parity_scheduler_approvals.py`（23 项），全部使用临时
目录与进程内假回调，不接触生产、不派发真实 agent：

- **RoutineClock**：`compute_due` 的 interval/daily(时区)/cron(工作日窗口)/空
  schedule；首次 tick 只建立基线；到期只派发一次且状态落盘；禁用 routine 不派发；
  probe 失败与 git 无变更跳过；派发异常不破坏时钟；时钟状态跨重启保留。
- **AutomataScheduler（真实 APScheduler）**：注册/列出/删除、持久化与重启恢复、
  非法 cron 与空 prompt 报错、事件触发真的跑到无头回调。
- **ApprovalService**：request→人类 resolve、高危动作拒绝 agent principal（403 且
  保持 pending）、重复 resolve 409、agent 等待循环在人类批准后放行、拒绝时返回
  原因、handoff 创建与恢复、ask-once 缓存永不缓存 destructive 动作。

### 发现的真实 bug（已修，行为变更）

`server/browser_computer_adapter.py::__init__` 无条件绑定
`oprim.browser_design_capture`，而 3O 浏览器库**没有**这个操作（Design Mode 的
capture 实现在 `server/routes/design.py`）。结果是**每一次构造适配器都抛
`AttributeError`**：

- `tests/test_browser_computer_3o.py::test_browser_writes_use_existing_action_gateway_and_human_control`
  在 `main` 上就已经是红的（基线复现同样报错）；
- `server/routes/workbench.py` 的 browser action 路由因此不可用。

修复：仅在 3O 提供该操作时才绑定；缺失时该动作按"unknown browser action"返回，
构造不再崩溃。这是本次唯一的运行时行为变更，独立成 commit（`38f803a6`）。

调度器/审批本身未发现需要修复的缺陷。

---

## 五、附加：world-intel-mcp 启动方式（独立 commit）

`server/world_intel_mcp.py`：

- `_command()` 原来优先用 `<root>/.venv/bin/world-intel-mcp`。该脚本的 shebang 指向
  宿主机解释器路径，**在容器内不存在**，导致网关静默起不来。现在固定为
  `[sys.executable, "-m", "world_intel_mcp.server"]`；`_child_env` 已经把 checkout
  的 `src` 加进 `PYTHONPATH`。
- 接线失败日志改为 `exc_info=True`，容器启动问题可直接看到完整堆栈。

测试：`tests/server/test_world_intel_mcp.py` 新增 2 项——即使存在可执行的
`.venv/bin/world-intel-mcp` 也必须用 `-m` 形式；接线失败时日志记录带
`exc_info` 且包含原始错误信息。

---

## 六、验证

### 新增/相关测试（全绿）

```
$ python -m pytest -q tests/server/test_browser_session_tools.py \
    tests/server/test_round_compaction.py tests/server/test_auth_cli.py \
    tests/server/test_hark_parity_scheduler_approvals.py \
    tests/server/test_world_intel_mcp.py tests/test_lazy_tools.py \
    tests/test_feature_flags.py tests/test_browser_computer_3o.py \
    tests/test_nano_w6_approvals.py tests/test_orca_w5_design_mode.py
======================= 128 passed, 14 warnings in 7.39s =======================
```

### 调度/协调器相关套件

```
$ python -m pytest -q tests/test_automata.py tests/test_p2c_routine.py \
    tests/test_lorca_p4_routines.py tests/test_w11_s6_s7_routines.py \
    tests/test_coordinator_p0.py tests/test_coordinator_cognitive.py \
    tests/server/test_tool_description_integrity.py
================== 3 failed, 60 passed, 5 warnings in 14.38s ===================
```

这 3 个失败在 `main` 基线快照上**逐条相同**（`3 failed, 60 passed`），与本次改动
无关：

- `tests/test_automata.py::test_automata_routes_reachable` — 401，测试未带鉴权头，
  与 SEC H1 默认拒绝冲突的既有问题。
- `tests/test_coordinator_cognitive.py::test_reflection_loop_on_sandbox_failure`
  与 `::test_real_sandbox_execution` — 本机沙箱 `/dev/null: Permission denied` +
  `_Py_HashRandomization_Init` 环境问题。

另跑的 `tests/test_aud2_max_result_chars.py / test_e2e_internalized_tools.py /
test_coordinator_safety.py / test_action_gateway_3o.py / test_layer4_service.py /
test_long_task_wiring.py`：`13 failed, 49 passed`，基线同样 `13 failed, 49 passed`。

### Ruff

对所有改动文件执行 `ruff check` 与 `ruff format --check`：**全部通过**。
`server/tool_registry.py` 保留 4 条既有告警（`RUF059` ×3、`RUF100` ×1），与基线
逐条相同，未顺手修改无关代码。

### 提交

```
$ git log --oneline main..HEAD
<本报告自身提交> docs: Hark parity Phase 0 report
4ae1907b fix(world-intel): launch the MCP server as a module, log wire failures in full
f6d35bb2 test(scheduler): in-process live checks for routines, automata, approvals
38f803a6 fix(browser): construct BrowserComputerAdapter without optional design_capture op
1eabd542 feat(cli): veya auth token for CLI/HTTP automation
ff331e08 feat(coordinator): opt-in per-round compaction (VEYA_ROUND_COMPACTION)
d0912247 feat(browser): opt-in stateful browser session tool (VEYA_BROWSER_SESSION)
```

```
$ git diff --stat main..HEAD
 cli/auth_cli.py                                    | 139 ++++
 cli/main.py                                        |   5 +
 docs/hark-parity-phase0-report.md                  | 329 +++++++++
 server/browser_computer_adapter.py                 |   8 +-
 server/browser_session_tools.py                    | 746 +++++++++++++++++++++
 server/coordinator_master.py                       | 114 ++++
 server/feature_flags.py                            |   3 +
 server/tool_registry.py                            |   8 +
 server/world_intel_mcp.py                          |  15 +-
 tests/server/test_auth_cli.py                      | 138 ++++
 tests/server/test_browser_session_tools.py         | 360 ++++++++++
 .../server/test_hark_parity_scheduler_approvals.py | 438 ++++++++++++
 tests/server/test_round_compaction.py              | 177 +++++
 tests/server/test_world_intel_mcp.py               |  42 ++
 14 files changed, 2519 insertions(+), 5 deletions(-)
```

---

## 七、逐项回滚

| 改动 | 回滚方式 |
|---|---|
| 有状态浏览器会话 | 不设 `VEYA_BROWSER_SESSION` 即完全不注册；如需彻底移除，revert `d0912247` |
| 按轮压缩 | 不设 `VEYA_ROUND_COMPACTION` 即不构造驱动器；revert `ff331e08` |
| `veya auth` | 纯新增 CLI 子命令，不涉及服务端行为；revert `1eabd542` |
| `design_capture` 绑定修复 | revert `38f803a6`（会恢复适配器构造崩溃，不建议） |
| world-intel 启动方式 | revert `4ae1907b`（会恢复容器内起不来的旧行为） |

所有开关关闭时，工具 schema 列表、系统提示词、注册表与执行路径均与 `main` 一致
（有测试断言）。

---

## 八、仍需部署期完成 / 未完成项

1. **容器内 Playwright 浏览器包**：`deploy/ms-playwright/` 被 `.gitignore` 忽略且
   当前不存在；镜像里虽有 `COPY` 与 `PLAYWRIGHT_BROWSERS_PATH`，但没有实际浏览器
   文件。需要构建时把 bundle 放进该目录（或用 sidecar + `connect_over_cdp`）。
   本次只在本机 venv + 宿主 Chromium 上做了真机 smoke。
2. **`browser_session` 与既有 `BrowserComputerAdapter` 的关系**：新工具直接管理
   Playwright，未复用 `ActionGatewayAdapter` 的接管策略（人机接管）。当前用
   `privileged_action_gate` 覆盖不可逆动作与登录；若要把人机接管也接上，属于后续
   设计决策，本次未擅自改动冻结链路。
3. **审批 UI 闭环**：`login`/敏感点击返回 `approval_required` 后，实际批准仍依赖
   既有审批界面（`/approvals`）或 `veya` 侧审批流；本次只验证了进程内的
   request→resolve→放行链路。
4. **`VEYA_ROUND_COMPACTION` 的线上效果**：只做了单元与驱动器级测试，没有跑真实
   长任务对比延迟；建议灰度时用 `round_compaction` SSE 事件与 decision ledger 观察
   实际触发次数与 token 变化。
5. **`tests/test_automata.py::test_automata_routes_reachable` 等 3 个既有失败**：
   基线即红，未修（属于鉴权测试写法与本机沙箱环境问题，超出本次范围）。
6. **未推送、未部署、未重启**：所有改动只在 `feat/hark-parity` 本地分支。

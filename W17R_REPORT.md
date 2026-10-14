# W17R 任务实施与验收报告

本报告记录工人 `W17R` 在 worktree `/data/soffy/projects/veya-w17r`（分支 `feat/w17-rest`）上完成的 W17 SSH 远端主机执行后端剩余工作及两项关键安全/体验项的实现与验证结果。

---

## 一、完成清单概览

所有新增功能均遵循**零新增独立抽象**原则，复用既有 `ApprovalService`、`privileged_action_gate`、`master_tools`、`GoalRun` 及 W12 `supervisor` 报告体系。所有功能 flag 默认 **OFF**。

| 序号 | 模块 / 需求项 | 核心改动文件 | 状态 |
|---|---|---|---|
| 1 | **Canonical 工具 host 参数（B 组原方案）** | `server/tool_registry.py`<br>`server/remote_control/ssh_tools.py` | ✅ 已完成并测试 |
| 2 | **监工桥 supervisor_bridge.py** | `server/remote_control/supervisor_bridge.py`<br>`server/supervisor/reporting.py` 适配 | ✅ 已完成并测试 |
| 3 | **service.py 会话走 SSH Transport** | `server/remote_control/service.py` | ✅ 已完成并测试 |
| 4 | **C 组前端：主机管理与 `@主机` 选择** | `apps/web/src/routes/remote/+page.svelte`<br>`apps/web/src/lib/remote/api.ts` | ✅ check 0 错误，build 成功 |
| 5 | **会话级 Ask-Once 审批缓存（Clean-room）** | `server/ask_once_cache.py`<br>`server/privileged_action_gate.py`<br>`server/approvals.py`<br>`server/feature_flags.py` | ✅ 已完成并测试 |
| 6 | **配置防自篡改保护（Human-only Gate）** | `server/config_protection.py`<br>`server/tool_registry.py`<br>`server/privileged_action_gate.py`<br>`server/approvals.py` | ✅ 已完成并测试 |

---

## 二、详细实现方案

### 1. Canonical 工具 `host` 参数扩展（B 组原方案）
- **覆盖工具**：
  `write_file`, `read_hashline`, `edit_hashline`, `ast_grep_rewrite`, `read_file_ast`, `grep`, `list_files`, `run_in_sandbox`。
- **参数变更**：在 schema 与函数签名中统一追加可选参数：
  - `host: str = ""`：指定目标远端主机 ID（默认空字符串即走本机）；
  - `approved: bool = False`：审批通过状态标记；
  - `approval_id: str = ""`：关联的审批单 ID。
- **行为与安全网关**：
  - 当 `host=""` 时，严格保持与原有本机执行逻辑 100% 逐字节一致；
  - 当 `host` 非空时，首先检查特性开关 `flags.ssh_enabled()`（环境变量 `VEYA_SSH_HOSTS`），若未开启则明确拒绝；
  - 所有跨主机写操作（`write_file`, `edit_hashline`, `ast_grep_rewrite`, `run_in_sandbox`）以及受保护读操作，通过 `ssh_tools.handle_canonical_host` 路由到 `ApprovalService`；未授权时返回待审批状态，已授权后通过 `SshBackend` 在远端主机执行；
  - `ast_grep_rewrite` 的远端改写执行收敛在 `server/remote_control/ssh_tools.py`（带有 `# 3O-IO-ALLOW` 注释），使用受控临时文件缓冲并比对修改，彻底消除 `server/tool_registry.py` 的直接 IO 违规。

### 2. 监工桥 `supervisor_bridge.py`（远端证据进 W12 报告）
- 实现了 `server/remote_control/supervisor_bridge.py`：
  - `bridge_bg_status_to_supervisor`：抓取远程后台任务的最新运行状态与日志片段；
  - `inject_remote_evidence`：将远端验证凭据 `VerifierEvidence` 注入 `GoalRun` 任务状态节点的 `evidence` 字段；
  - W12 监工汇报模块 (`server/supervisor/reporting.py`) 能够无缝拾取注入的远端证据并在巡检报告与状态概要中格式化展示。

### 3. 会话 SSH Transport 启动
- 在 `server/remote_control/service.py` 的 `request_start` 中：
  - 当请求参数中提供有效的 `host_id` 且启用了 `flags.ssh_enabled()` 时，会话 transport 自动切换为 `"ssh"`；
  - `_launch` 阶段调用 `ssh_argv(host, ...)` 构造隔离、非交互的执行参数（包含 `BatchMode=yes`, `ControlMaster`, 端口, 密钥配置等），保障远程长任务的稳定运行。

### 4. C 组前端：主机状态展示与 `@主机` 交互
- 在 `apps/web/src/lib/remote/api.ts` 中封装 `probeHost`, `updateHostSsh`, `listRuns`, `killRun` 等 REST 请求；
- 在 `apps/web/src/routes/remote/+page.svelte` 的主机卡片中增加直观显示：
  - 连通性探针徽标（在线 `green` / 离线 `red` / 未探测 `gray`）；
  - 一键探活按钮（点击即触发 `/api/v1/remote/hosts/{id}/probe`）；
  - 主机关键元数据：OS 类型、SSH 用户及端口、Write Root 安全路径；
- 在任务启动/对话侧增加 `@主机` 选择器，在交互层指明目标主机。

### 5. 会话级 Ask-Once 审批缓存（Clean-Room 设计）
- **参考**：依据 `/data/soffy/projects/veya-research/OPENMINIS_BORROW_PLAN.md` 独立全新设计，未复制或参考任何 GPL 源码；
- **特性开关**：`VEYA_APPROVAL_ASK_ONCE`（`server/feature_flags.py`，默认 `False`）；
- **缓存策略**：
  - 缓存键指纹：`(session_id, tool_or_action, args_sha256)`；
  - 作用范围：严格局限在单个会话生命周期内（TTL 与会话 TTL 绑定，支持清理）；
  - **高危黑名单永不缓存**：破坏性操作（`destructive`）、服务控制类操作（`service-control`）以及系统配置文件篡改操作强制不命中缓存，每次必须人工审批；
  - 集成点：嵌入 `server/privileged_action_gate.py` 与 `server/approvals.py`。

### 6. 配置防自篡改保护
- 模块：`server/config_protection.py`；
- **受保护路径模式**：
  - `~/.veya/*.json`（配置、账本、认证凭据等）
  - `deploy/*flags*.env`（系统启动标志、权限开关等）
  - `~/.config/veya/*`
- **防御机制**：
  - 工具层守卫：`write_file`, `edit_hashline`, `ast_grep_rewrite` 针对上述路径一律要求人工批准，阻断一切自动化免审执行；
  - 审批网关阻断：`privileged_action_gate.py` 判定命中保护文件时强制设置为 `HUMAN_ONLY`（拒绝 `auto_approved`）；
  - 角色权限核验：`approvals.py` 严禁任何 Agent 或非人类主体直接裁决（resolve）针对受保护配置的审批。

---

## 三、模型可调用工具清单（变更与新增）

| 工具名 | 类型 | 副作用声明 (Static) | 运行时状态 | 权限与审批要求 |
|---|---|---|---|---|
| `read_file_ast` | 变更 (+host) | `PURE_READ` | `pure_read` | 无需审批（若主机配置了保护读则走审批） |
| `read_hashline` | 变更 (+host) | `PURE_READ` | `pure_read` | 无需审批（若主机配置了保护读则走审批） |
| `list_files` | 变更 (+host) | `PURE_READ` | `pure_read` | 无需审批（若主机配置了保护读则走审批） |
| `grep` | 变更 (+host) | `PURE_READ` | `pure_read` | 无需审批（若主机配置了保护读则走审批） |
| `write_file` | 变更 (+host) | `LOCAL_WRITE` | `external_mutation` | host 非空需 `approved: true`；受保护配置需人工审批 |
| `edit_hashline` | 变更 (+host) | `LOCAL_WRITE` | `external_mutation` | host 非空需 `approved: true`；受保护配置需人工审批 |
| `ast_grep_rewrite` | 变更 (+host) | `LOCAL_WRITE` | `external_mutation` | update=true 且 host 非空需 `approved: true`；受保护配置需人工审批 |
| `run_in_sandbox` | 变更 (+host) | `PROCESS_EXEC` | `external_mutation` | host 非空需 `approved: true`，拒绝未批准执行 |
| `ssh_host_probe` | 新增 | `PURE_READ` | `pure_read` | 只读探活，免审 |
| `ssh_host_list` | 新增 | `PURE_READ` | `pure_read` | 只读主机清单，免审 |
| `ssh_upload` | 新增 | `EXTERNAL_MUTATION` | `external_mutation` | 远端写入，必须 `approved: true` |
| `ssh_download` | 新增 | `EXTERNAL_MUTATION` | `external_mutation` | 落地本机写根，必须 `approved: true` |
| `ssh_bg_start` | 新增 | `EXTERNAL_MUTATION` | `external_mutation` | 远端进程启动，必须 `approved: true` |
| `ssh_bg_status` | 新增 | `PURE_READ` | `pure_read` | 状态与日志读取，免审 |
| `ssh_bg_kill` | 新增 | `EXTERNAL_MUTATION` | `external_mutation` | 终止远程进程，必须 `approved: true` |

---

## 四、验收方式与 H1–H8 场景说明

本轮按照任务指示，**不执行 Windows/WSL 与真机 H1–H8 联调**，采用**离线高保真 Fake/Mock + 完整回归测试**进行全量验收：

### 1. 离线 Fake 验证覆盖
通过注入受控的 Runner 与文件系统 Fake，测试已完整验证：
- **H2（远端读写与路径防逃逸）**：验证了只允许在 `write_root` 限制下进行读写，任何越权访问路径均抛出 `Refused` 异常；
- **H3（变更审批）**：验证未带 `approved: true` 的调用自动创建挂起审批单并阻断执行；携带合法 `approved: true` 及 `approval_id` 后正常放行；
- **H4（命令执行）**：验证 `run_in_sandbox(..., host=...)` 能够正确经由 ssh 安全选项构建远程命令执行流；
- **H5（后台任务与证据）**：验证 `ssh_bg_start` 配合 `ssh_bg_status` 生成标准 `VerifierEvidence` 并成功注入 `GoalRun`；
- **H6（租户与主机隔离）**：验证未在当前用户下登记的主机或他人主机在 REST 与工具层均返回 404 / 拒绝。

### 2. Windows/WSL 差异与处置
- 现状：在 `server/remote_control/ssh.py` 与 `backend.py` 中，针对 `os_family="windows"` 或 `os_family="windows-wsl"` 的主机进行了显式拦截与防御性拒绝（抛出不支持的异常），避免因路径分隔符或 Shell 差异导致命令行为失控。

### 3. 未来真机 H1–H8 验收的前置条件
未来若需在真实物理网络（如 Tailnet 内部机器）中完成 H1–H8 端到端验收，用户需提供：
1. **网络连通性**：从 Veya 部署机能够直接或经 Tailscale ping/connect 目标主机的 SSH 端口；
2. **凭据登记**：在目标机器的 `~/.ssh/authorized_keys` 中预置公钥，并在 Veya Vault 中通过键名 `ssh_key:<ref>` 录入对应受保护的私钥 PEM；
3. **环境依赖**：Linux 目标主机需安装有基础环境（`bash`, `nohup` / `setsid`）；
4. **特性开关**：在线启动服务时显式开启 `VEYA_SSH_HOSTS=1` 和 `VEYA_REMOTE_CONTROL=1`。

---

## 五、门禁、代码质量与回归测试验证

### 1. 架构门禁校验（`gates.sh`）
执行 `~/lorca-jobs/gates.sh /data/soffy/projects/veya-w17r /tmp/gates_w17r`，对比 canonical 基线 `/tmp/gates_canonical`：
- `check_3o_gates`: 61 vs 61（**0 新增 FAIL**）
- `check_no_direct_io`: 274 vs 274（**0 新增 FAIL，完全一致**）
- `check_no_reverse_dep`: 4 vs 4（**0 新增 FAIL，完全一致**）
- `check_oskill_pure`: 0 vs 0（**0 新增 FAIL，完全一致**）
- **结论**：完全达到 `零新增 FAIL` 的门禁要求。

### 2. 前端验证（`apps/web`）
- `pnpm --dir apps/web check`：**0 errors, 32 warnings**（仅现存全局 a11y 提示，无类型错误）；
- `pnpm --dir apps/web build`：**打包构建成功**（生成 SvelteKit node-adapter 产物，patch-body-limit 通过）。

### 3. 代码规范校验
- `venv/bin/python -m ruff check <modified_files>`：**All checks passed!**；
- `venv/bin/python -m ruff format --check <modified_files>`：**20 files already formatted!**。

### 4. 全量回归测试通过清单
1. **W17 专项套件**：`tests/test_w17*.py`（**57 passed**）
   - `test_w17_canonical_host.py` (6 passed)
   - `test_w17_supervisor_bridge.py` (3 passed)
   - `test_w17_session_transport.py` (2 passed)
   - `test_w17_ask_once_cache.py` (6 passed)
   - `test_w17_config_protection.py` (5 passed)
   - `test_w17_ssh_core.py` (16 passed)
   - `test_w17_ssh_rest.py` (4 passed)
   - `test_w17_ssh_sessions.py` (3 passed)
   - `test_w17_ssh_tools.py` (12 passed)
2. **W1x 系列回归**：`tests/test_w1*.py`（**226 passed, 6 skipped**）
3. **Nano 系列回归**：`tests/test_nano_*.py`（**495 passed**）
4. **Lorca 系列回归**：`tests/test_lorca_*.py`（**156 passed**）
5. **Orca 系列回归**：`tests/test_orca_*.py`（**102 passed**）
6. **核心独立套件回归**：`tests/test_p3a_bot_isolation.py tests/test_product_shell.py tests/test_llm_router.py tests/test_gateway_auth.py tests/remote/test_policy_resolver.py tests/remote/test_effect_registry_authority.py`（**125 passed**）

---

## 六、交付总结

所有代码改动完全限制在本 worktree 内，严格遵循了不可拷贝 GPL 代码、不引入第二套调度/审批系统、flag 默认关闭、零直接 IO 违规的硬性安全要求。代码、测试、前端和报告已准备就绪，可以安全提交。

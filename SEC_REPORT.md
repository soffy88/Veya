# Veya 安全修复验收报告

**日期：** 2026-10-15  
**工作树：** `/data/soffy/projects/veya-sec`（`fix/security-audit`）  
**基线：** 已合并 `main` 当前要求的 `e1148be4`。  
**结论：** **条件不通过生产发布门（NOT READY）**。本次修复已增加代码防护和自动回归，但尚有身份信任边界、SSRF DNS rebinding、CLI 模板执行、以及运行中的 backend 公网端口绑定等未解决风险。数据库账号核查因 Local2 安全边界拒绝读路径未能取得可验证统计，不作“无异常”结论。

## 1. 本次执行与边界

- 直接在该 git worktree 修改文件、运行 pytest/py_compile、执行静态源配置检查；没有 push、没有重启/重建服务。
- 没有编辑或暂存 `.venv`，没有操作 `platform/3O`，没有删除工作树外的文件。
- 已将源码修复按安全主题分成多个本地 commits；没有 push。`SEC_DEPLOY.md` 与本报告已在工作树中更新，并将在最后的文档提交中落地。
- 代码/配置测试通过不代表正在运行的容器已重载新配置；运行时差异见第 6 节。

## 2. 高风险与核心项核对

| 项目 | 核验结果 | 代码/测试证据 | 剩余限制 |
|---|---|---|---|
| C1：未认证 workflow 任意 `exec` | **已缓解** | `veya/server/app.py` 不再注册 `/api/v1/agent/workflow/run`，相关 `test_sec_audit_c1_workflow_exec.py` 覆盖路由缺失和执行原语缺失 | 静态测试覆盖的是本仓库应用，不替代所有服务版本/代理路由的生产探测 |
| C2：sandbox 命令执行 | **已收紧** | sandbox 执行端点要求 owner；沙箱环境变量采用白名单。routine command probe 另要求 owner、绑定 workspace、参数数组和精确持久化审批，`subprocess.run(..., shell=False)`；`test_sec_audit_routine_command_probe.py` 4 项通过 | 该功能不是 OS/容器级强隔离证明；需检查生产 bwrap/namespace 实际可用性 |
| C3：远程 OAuth token 选择 | **已修复并提交** | `veya/remote/oauth.py` 只接受 principal 与 owner username 精确匹配的 token；移除全局唯一 active-token fallback；指定 `VEYA_REMOTE_OAUTH_TOKEN_ID` 也需满足 owner/principal；`tests/test_sec_audit_oauth.py` 4 项通过 | 生产 token 内容/授权范围未导出到报告，也未执行真实 OAuth provider 握手 |
| C4：legacy master/chat | **已有拒绝路径与回归** | `test_sec_audit_c4_master_endpoint.py` 3 项通过 | 只验证当前 worktree 路由与拒绝契约 |
| H1：默认拒绝/身份访问 | **部分完成** | auth 默认拒绝、注册默认关闭的回归通过；bot owner 的空/system/default/anonymous 旧 marker 不再授予全局共享权，`test_sec_audit_bot_owner_markers.py` 2 项通过 | 不能据此宣称全 API 路由已经全量鉴权；仍须路由清单审计 |
| H2：文件路径穿越 | **已缓解** | 文件路径解析并限制到允许根目录；`test_sec_audit_h2_fs_traversal.py` 5 项通过 | 未开展生产文件系统全量渗透测试 |
| H3/H8：`verify_command` | **已收紧** | 调用方提供的 verify command 不再作为信任来源；使用服务端 `VEYA_VERIFY_COMMAND_ALLOWLIST`，图验证执行要求 owner 且只选择 allowlist 中的命令；`test_sec_audit_verify_command.py` 3 项通过 | 没有运行真实生产仓库 verify 命令，也没有把任意 allowlist 配置等同于安全命令；配置内容需发布审核 |
| H4：webhook 签名 | **代码 fail-closed 已实现** | 通用 automata webhook 要求 `VEYA_WEBHOOK_SECRET` + 原始请求体 HMAC-SHA256；未配置或缺签名拒绝，使用常量时间比较。WeChat 签名也使用 `hmac.compare_digest`；Discord 采用 Ed25519 并在公钥未配置时不挂载 router。`test_sec_audit_webhook.py`、`test_sec_audit_webhooks.py`、`test_sec_audit_im_signatures.py` 合计 8 项通过 | 未验证真实平台回调的生产凭据配置、回放窗口或端到端签名互通 |
| H5：子进程环境泄漏 | **已收紧** | `veya/obase/sandbox.py` 子进程环境以 allowlist 构建，不再直接复制完整服务端环境 | `env_extra` 与各执行路径仍需结合部署配置检查；本次未完成真实沙箱内 provider-key 泄漏演练 |
| H6：Host/CORS | **源配置通过** | `TrustedHostMiddleware` 默认限制 localhost，支持显式 `VEYA_ALLOWED_HOSTS`；CORS 精确 origin allowlist，无通配 regex；`test_sec_audit_host.py` 2 项通过 | 生产域名必须通过环境变量配置；未对公网 Caddy/CDN 的实际 Host/CORS 做端到端验证 |
| H7：hicode 服务监听 | **代码已改，生产运行时未落实** | entrypoint 改为 `127.0.0.1:8768 --auth token`；systemd source 改 loopback；`test_sec_audit_deploy_binding.py` 对源文件绑定 3 项通过 | 当前运行中的 backend 仍有 wildcard published ports，见第 6 节。没有重启服务，所以**生产暴露问题仍在**；Reasonix token provisioning 握手也未验证 |
| H9：出网/SSRF | **部分完成** | `VEYA_TOOL_GATE_ENFORCE`/`VEYA_EGRESS_ENFORCE` 默认 enforce；fetch URL 检查 scheme、userinfo、DNS 地址是否为公网 IP，并对每次 redirect 重新验证，限制 redirect 数；`test_sec_audit_ssrf.py` 3 项通过 | DNS 校验与 HTTP 客户端解析分离，仍可能受到 DNS rebinding/代理端解析差异影响；不是 IP pinning 或网络层 egress firewall 的替代品。`browser_run`、其他 URL 出口未被这些单元测试完整覆盖 |

## 3. M1–M7 及低危项诚实状态

| 项目 | 结果 | 依据/限制 |
|---|---|---|
| M1：审批 resolver principal 冒充 | **未完成，阻断项** | REST 解析前验证 approval scope，端点使用 `require_user`，但 `ApprovalResolveRequest.principal` 仍是请求体字段，调用方可以自报 `human`。这不足以证明是真实人工操作；需要服务端可验证的人工确认通道（如 UI session + CSRF/重新认证），不能以现有测试宣称已修复。高风险发布前必须补齐。 |
| M2：IM 绑定归属 | **部分完成** | `dispatch_inbound` 只在绑定记录有非空 `external_user_id` 且与当前平台用户精确相等时授予 owner；旧缺失身份记录不再继承 owner_ref。`test_nano_w7_channels.py` 在回归组中通过。WeCom 加密 XML/字段处理有修改，但本次未充分证明所有 plaintext/加密分支的消息完整性；其他 IM 平台真实 webhook 未逐一验证。 |
| M3：SSRF/MCP connector | **部分完成** | 出网 URL fetch 的单测与部分 MCP connector owner/admission guard 有修改。DNS rebinding、代理解析、浏览器执行器及所有连接/回调地址的完整 egress 约束没有被验证。 |
| M4：账户/MCP JSON-RPC 自报 grant | **未完成** | 本次没有修改受保护的 `platform/3O` 权限层，也没有证明所有 JSON-RPC 请求都不能提交伪造的 account/grant 身份。需要在可修改的执行入口和 3O 授权边界分别验证；当前不得宣称已解决。 |
| M5：CLI/provider 命令配置 | **部分完成** | 自定义 CLI profile 写入端点现在要求 owner；provider 的 bot model source 变更检查 bot owner。尚未增加已知 executable allowlist，命令模板校验仍主要依赖 `{prompt}`/`{cwd}` 占位符；provider helper 对不存在的 bot 仍可放行。因此 arbitrary executable/template 风险并未完全消除。 |
| M6：公开 artifact 分享 | **路由层已修复** | artifact 分享/撤销验证 artifact ID 与 owner；`/api/v1/settings/artifacts` 读取/写入改用 `require_owner`；`test_sec_audit_artifacts.py`、`test_sec_audit_artifact_settings.py` 和 ORCA artifact 回归通过。公开分享开启后仍需使用者按需控制和定期清理分享 token。 |
| M7：归档路径穿越 | **源码防护有证据** | `deploy/prepare_hicode_3o_context.py` 和 `scripts/check_3o_canonical_pins.py` 的 `extractall` 使用 `filter="data"`；`test_sec_audit_archive_filter.py` 验证两个调用点。未运行外部恶意 tar corpus 的完整 fuzzing。 |
| L1：登录限速/长期 token | **未做/未验证** | 本轮未改认证数据库 token 生命周期、撤销和登录限速机制；不能据本报告判定安全。 |
| L2：IM signature 常量时间比较 | **部分完成** | WeChat HMAC/SHA 比较采用 `hmac.compare_digest`，Discord Ed25519 校验 fail closed；未对 Feishu 等所有平台比较和完整 webhook 签名逐个复核。 |
| L3：XML parser hardening | **局部完成** | 新 `veya/obase/channels/safe_xml.py` 限制 1 MiB 并拒绝 DTD/entity，channels parser 采用它，测试通过。未证明仓库所有 XML 消费点都调用此包装器。 |
| L4：WeCom receiveid/corp id | **部分验证** | WeCom 相关实现改动并通过 channels 回归；未做企业微信真实加密 webhook 往返验收。 |
| L5：第三方安装脚本供应链 | **未做** | 未修改 curl-to-shell 安装/下载供应链；应独立审查来源、校验和和 pin。 |
| L6：tool manager checksum/owner | **未做/未验证** | 本轮没有为全部 tool manager 安装来源及 checksum/owner 机制提供独立代码与测试证据。 |
| L7：表达式调试 eval | **HTTP 入口已移除** | `/advanced-visualization/debug/expression` 路由已删除，生产 debug guard 默认拒绝，`test_sec_audit_debug_eval.py` 3 项通过。底层非 HTTP debugger 类仍存在 `eval`，不能被报告为全仓无 eval。 |

## 4. 部署项 17：静态配置与实际运行环境不一致

静态配置检查通过：

- `deploy/docker-compose.yml` 声明 backend ports 绑定 `127.0.0.1`；
- `deploy/veya-gateway.service` 使用 `--host 127.0.0.1`；
- `deploy/veya-web.service` 使用 `HOST=127.0.0.1`；
- `deploy/hicode-entrypoint.sh` 使用 loopback 和 token auth；
- 两个归档解包入口采用 `filter="data"`。

这些是源码级证据，不是生产部署证据。当前运行时有反证：本次只读 `docker ps` 显示 `veya-backend` 为 `Up 2 hours (healthy)`，端口含 `0.0.0.0:8767->8765/tcp`、`0.0.0.0:9120->8765/tcp`，并含 `[::]:8767->8765/tcp`、`[::]:9120->8765/tcp`。**这是当前未解决的高风险公网暴露**，静态 Compose loopback 测试无法抵消这一事实。由于任务要求不重启服务，本次没有重建容器。需要经批准的维护窗口重新创建 backend，并以 `docker inspect`、`docker ps` 和非本机网络探测验收；步骤已写入 `SEC_DEPLOY.md`。

## 5. 账号及访问日志只读核查（项目第 18 项）

### 账号数据库

访问主机路径 `/home/soffy/.veya/auth.db` 的只读查询在本次 Local2 会话中受到路径安全限制，未能取得可展示、可复核的 `users` 行数、账号列表或 `created_at` 分布。因此以下数字均**未能核实**：账号总数、owner/admin 数、非 owner 数、注册时间范围及逐账户创建时间。未输出任何 password hash、token 或 API key。不能根据查询被拒绝来推断“无异常”。这部分仍需有权限的运维者在批准的只读事务中核查。

### 可读主机日志

读取并扫描了本地可读文件日志：

| 文件 | 扫描行数 | 文件 mtime（UTC） | 可用性限制 |
|---|---:|---|---|
| `/var/log/veya-gateway.log` | 126 | 2026-08-06 17:26:49 | 旧的、有限的网关 access 格式记录 |
| `/var/log/veya-gateway-error.log` | 55,386 | 2026-08-23 16:21:27 | error log，不是完整 access log |
| `/var/log/veya-web.log` | 194 | 2026-10-14 16:29:49 | web 服务日志，不是权威 API access log |
| `/var/log/veya-web-error.log` | 21,539 | 2026-10-14 22:05:32 | error log，不是完整 access log |
| **合计** | **77,245** | — | 仅统计上述文件中可读行数 |

对其中可识别的标准 HTTP request line，仅统计 2xx 并按 source IP 归组，结果如下：

| 路径匹配 | 可识别的 2xx 数 | 来源 IP | 结论边界 |
|---|---:|---|---|
| `/legacy/tools/execute` | 0 | 无匹配 | 只代表上述日志可解析记录 |
| `/legacy/master/chat` | 0 | 无匹配 | 同上 |
| `/api/v1/auth/register` | 0 | 无匹配 | 同上 |
| `/api/v1/sandbox`（含子路径） | 2 | `127.0.0.1`: 2 | 仅观察到本机来源记录 |
| `workflow/run` | 0 | 无匹配 | 同上 |

常规路径 `/var/log/caddy/access.log` 和 `/var/log/caddy/caddy.log` 未找到。错误日志并非 access log；gateway access 文件时间较旧；不完整日志中的零匹配不能证明没有请求，也不能证明没有攻击。容器日志源未纳入这份 IP 汇总。**这是一份有限日志窗口的计数，不是“无异常”结论**；需在运维授权后关联 Caddy/容器访问日志、时间窗和真实 client IP（同时考虑反向代理的可信 forwarded headers）。

## 6. 回归验证

在当前工作树上执行的实际结果：

| 命令/范围 | 结果 |
|---|---|
| `python -m pytest -q --tb=short tests/test_sec_audit_*.py` | 最终完整运行：95 passed、6 warnings（`tests/test_sec_audit_*.py`），包含 `tests/test_sec_audit_archive_filter.py` |
| `python -m pytest -q --tb=short tests/test_w1*.py tests/test_nano_w6_approvals.py tests/test_nano_w7_channels.py tests/test_lorca_p4_routines.py tests/test_orca_w4_routines_artifacts.py` | 365 passed、6 skipped、14 warnings（371 collected） |
| `python -m py_compile`（本轮触及 Python 源及测试文件） | 通过 |
| `git diff --check` | 通过 |
| `tests/test_sec_audit_deploy_binding.py` + `tests/test_sec_audit_archive_filter.py` | 4 passed |

Warnings 主要是依赖库 Future/SyntaxWarning、pytest 的旧 asyncio marker warning；本轮回归无失败。单测没有替代实际生产部署安全验证。

## 7. 本地提交

- `30c70d40` — C3 OAuth owner/principal binding（之前已提交）
- `cbff80e0` — H3/H8 verify command server allowlist
- `cda1357e` — H4 webhook signature fail-closed（并包含 gateway 的 workflow route removal）
- `4fe4bddf` — H6 TrustedHost/CORS
- `84b0a8da` — H7 loopback listener source configuration
- `52cae2cf` — H9 egress enforcement defaults/URL validation
- `8e0cceed` — M2 channel external identity / safe XML
- `59db1333` — M6 artifact share ownership
- `1084f313` — M7 archive extraction filter
- `1a83b5ac` — C2 routine command approval boundary
- `36e2d4cf` — H1 bot owner markers
- `7a5d0838` — M5 CLI/provider route owner access（仅部分缓解）
- `61ce55d7` — L7 expression evaluation route removal
- `03238c03` — M3 MCP connector admission tightening（部分缓解）
- `cb865759` — environment file ignore / deploy pseudo-secret generation

无 push。生产服务没有重启。`.venv` 在 Local2 状态中显示为既存未跟踪目录，本次未编辑、未新增、未暂存；保持原样。

## 8. 发布决定与必须跟进项

当前**不应判定安全审计全部通过，也不应直接进行生产发布**。发布阻断项：

1. 处理运行中 backend 的 `0.0.0.0`/IPv6 wildcard 端口映射；须批准维护窗口，不能由本次无重启任务偷偷处理。
2. 为 M1 实施真正服务端可验证的人工审批确认，而非请求体自报 `principal="human"`。
3. 为 M4 完成 JSON-RPC/account/grant 的可信身份与授权校验，覆盖受保护的 3O 权限边界。
4. 为 M5 建立可审计的 CLI 可执行文件 allowlist，防止只校验占位符而允许任意命令模板，并拒绝对不存在/无权 bot 的 provider mutation。
5. 在权限允许的只读事务内完成人员账户/创建时间统计，并关联正式 Caddy/容器 access log，保留来源 IP 与明确时间范围。
6. 解决 SSRF DNS rebinding/代理解析风险，并完成真实 IM/Reasonix 签名和 token 配置的端到端验证。

`SEC_DEPLOY.md` 包含部署变量、webhook 签名格式、loopback 检查与发布后人工验收命令。
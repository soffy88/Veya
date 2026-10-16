# SEC_FIX2 安全修复报告

- 日期：2026-10-15
- 工作树：`/data/soffy/projects/veya-sec`
- 分支：`fix/security-audit`
- 代码修复提交：`e2c4b0c4`（`fix(security): close identity and command trust gaps`）
- 最终结论：**NOT READY（不允许据此判定生产发布通过）**

## 结论摘要

本轮已修复 M1 的 REST 审批主体自报问题、M5 的 CLI/provider 模板和 owner 检查，并收紧 MCP REST 和 remote MCP JSON-RPC 的身份信任入口。原生 `_tool_fetch_url` 也已把经过验证的公网 IP 固定为实际 TCP 连接目标，避免该路径在校验后再独立解析 DNS。安全审计测试和选定的跨模块回归测试均通过。

代码及自动化测试通过，不等于生产运行环境安全。当前无法从本工作树会话连接 Docker daemon，未能重新验证正在运行的 backend 端口映射；先前 `SEC_REPORT_R5.md` 记录的最后一次运行态观察是 `8767`/`9120` 存在 `0.0.0.0` 和 IPv6 全地址发布，而仓库 Compose 源码配置为 loopback。生产容器没有在本次任务中重建，所以不能将这项风险标记为已解决。账号数据库只读盘点、外部出站路径以及生产令牌/IM 的端到端验证也仍未完成。发布门保持 **NOT READY**。

## 本轮完成项

| 项目 | 本轮变更 | 验证与限制 |
|---|---|---|
| M1：审批 principal 冒充 | `ApprovalResolveRequest` 不再接收可被信任的 principal 字段；REST resolve 从已认证用户计算 `human:<scope>`，不信任请求体或 `X-Veya-Principal`。审批 scope 与当前认证用户不匹配时拒绝。 | 新增 identity-trust 回归，验证伪造 `agent:` principal 不会影响服务端主体，以及伪造 scope 被拒绝。它证明的是该 REST 入口；不是对所有可能的人工确认渠道进行了生产端到端验证。 |
| M4：MCP REST / JSON-RPC 身份边界 | `/mcp/connect`、`/mcp/call` 要求 owner，actor 不再由请求体提供；REST 注册记录绑定认证 owner，其他 owner 不能调用或覆盖同名注册；`grant`/`grants` 请求字段保留为兼容输入但不再作为授权来源，执行由 `ToolGovernanceAdapter` 根据服务器端 contract/governance 决策。remote MCP JSON-RPC `initialize` 在启动恢复前先验证令牌。 | MCP route governance 和 JSON-RPC 顺序回归通过。尚未对受保护的 `platform/3O` 权限实现作整体性重构，也未对所有第三方 connector、生产 OAuth/session 组合进行端到端证明；因此整个系统范围的 M4 不能视为完全关闭。 |
| M5：CLI/provider 命令和归属 | CLI profile 命令模板校验要求指定占位符、拒绝 shell 元字符和未知占位符，并限制到已知 executable allowlist；额外 executable 必须通过 `VEYA_CLI_EXECUTABLE_ALLOWLIST` 明确配置。渲染使用 sentinel 保持 prompt/cwd 各自为单个 argv 参数；载入持久化 profile 时再次校验并拒绝覆盖内建 profile。remote agent executor 增加独立 allowlist 与 shell 元字符校验，SSH 使用单个 POSIX 引号化远端命令。provider 自定义增删改为 owner-only，非系统默认 bot 不存在或不属于调用者时拒绝 model-source mutation。 | 更新 CLI/provider 和远程 executor 回归测试，通过。运维仍应审查 `VEYA_CLI_EXECUTABLE_ALLOWLIST` 与 `VEYA_REMOTE_AGENT_EXECUTABLE_ALLOWLIST` 的实际部署值；本轮不读取或更改生产 secret store。 |
| SSRF / DNS rebinding | `_tool_fetch_url` 的每跳 URL 校验与地址解析共享同一 helper，拒绝 userinfo、非 HTTP(S)、内网/回环/链路本地或混合公网/非公网解析结果；实际 TCP 连接使用校验时固定的公网 IP，URL hostname 仍用于 Host/TLS identity；redirect 逐跳重新解析并 pin，限制跳转次数。此路径不使用系统/显式代理，避免代理另行解析目标 DNS。 | SSRF 与新 identity-trust 测试通过。pinning 使用 httpx/httpcore 的 network backend 接口，升级该依赖后必须运行回归。覆盖范围仅为原生 `_tool_fetch_url`；不是所有进程/客户端出网的总闸。 |
| 生产端口配置 | `SEC_DEPLOY.md` 增加 `8767`/`9120` loopback 绑定的 Compose/`docker run -p` 改法、容器重建要求和 `docker inspect`/`docker ps` 验收标准。`deploy/docker-compose.yml` 静态配置为 `127.0.0.1:8767:8765`、`127.0.0.1:9120:8765`、`127.0.0.1:8768:8768`。 | 没有重启或重建服务。此次只读 `docker ps` 检查因无法连接 `unix:///var/run/docker.sock` 失败；运行态端口不能在本轮确认已改为 loopback。先前报告所记录的 wildcard 发布风险继续作为阻断项，直到运维在可用主机上重新检查并于获批窗口重建容器。 |

## 测试与静态检查

| 检查 | 结果 |
|---|---|
| `tests/test_sec_audit_*.py` 全部 23 个测试文件（显式传入路径） | **103 passed, 6 warnings**，28.70s |
| 选定跨模块回归：identity/SSRF/auth、审批、side-effect approval、CLI registry、remote executor、MCP REST、provider routes | **165 passed, 14 warnings**，20.17s |
| `python3 -m py_compile`（本轮涉及的生产模块和测试模块） | PASS |
| `git diff --check` / 提交前 `git diff --cached --check` | PASS |

warnings 包括依赖的 pgmpy/Starlette deprecation、技能包的 `SyntaxWarning`，以及既有审批测试中同步测试函数带 `pytest.mark.asyncio` 的 warning；以上不是这两组测试的失败项。回归通过仅证明所执行测试路径，并不证明生产容器或外围网络已采用新代码/配置。

## 剩余发布阻断项

1. **运行态端口暴露尚未核实和修复。** 上次可用的运行态观察（见旧报告 `SEC_REPORT_R5.md`）记录 `veya-backend` 的 `8767`/`9120` 对 IPv4 全地址及 IPv6 全地址发布。当前 Docker API 不可达，无法确认现状是否变化；静态 Compose 的 loopback 配置并不改变既有容器。需由运维在获批窗口执行 `SEC_DEPLOY.md` 中的配置检查、`docker compose ... up -d --force-recreate backend`，然后检查 `HostIp` 必须是 `127.0.0.1`，并从非本机网络验证不可直连。这一部署重建本轮未执行。
2. **账号/身份数据库审计未完成。** 旧报告记录，对工作树外 `/home/soffy/.veya/auth.db` 的只读查询被安全策略拒绝；本轮不绕过该限制，也未取得可核实的账户总数、owner/admin、创建时间分布或异常账户清单。因此不能给出“账户均正常”的结论。需有权限的运维人员在批准的只读事务中核查；不得把 password hash、token 或 API key 输出到报告。
3. **外围网络和运行时证据不足。** 未对公网/Tailscale/Caddy 防火墙路径做现场验证；未做生产 OAuth issuer、WeCom/其他 IM 签名、Reasonix/hicode token provisioning 的真实端到端握手。host/systemd access logs 曾存在不完整或较旧的问题，不能用现有日志声称已完整审计所有访问。
4. **出网覆盖不完整。** DNS pinning 仅应用于原生 `_tool_fetch_url`。`browser_run`、MCP SDK transport、其他 URL/回调请求以及进程级出网并未被本轮覆盖；还需对这些路径分别审计，并由网络层施加 egress 白名单/防火墙策略。
5. **其他安全域未全面关闭。** IM 归属记录虽已针对空身份/旧 owner marker 收紧，但所有 IM 平台加密与 plaintext 分支尚未逐一进行真实 webhook 验证；OS/container sandbox 的网络和 namespace 隔离能力、全 API route 的 owner/auth 覆盖以及 tool-manager 安装来源/checksum 的全量审计仍不由本次测试证明。

## 生产端口操作说明

请遵循 `SEC_DEPLOY.md` 的具体步骤。目标映射必须为：

```yaml
- "127.0.0.1:8767:8765"
- "127.0.0.1:9120:8765"
- "127.0.0.1:8768:8768"
```

若使用 `docker run` 或独立 `backend-up.sh`，将 `-p 8767:8765` 改为 `-p 127.0.0.1:8767:8765`，`-p 9120:8765` 改为 `-p 127.0.0.1:9120:8765`。当前 worktree 未发现 `backend-up.sh`。映射变更必须通过重建容器生效，不能仅重启应用进程。以上只是待获批窗口执行的操作，不代表此次已执行。

## 提交与操作边界

- 代码与回归测试已本地提交：`e2c4b0c4`。
- 本报告作为独立文档提交；不会 push。
- 本任务未重启服务、未执行 Docker compose up/recreate、未更改工作树外文件。
- 预先存在且未跟踪的 `.venv` 与 `SEC_REPORT_R5.md` 保留不动。

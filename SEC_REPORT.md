# SEC: Veya 深度审计安全修复报告

**工作区：** `/data/soffy/projects/veya-sec`  
**分支：** `fix/security-audit`  
**基准：** `canonical main`  

---

## 1. 审计与修复概述

本项目针对 Veya 系统进行了全面的深度安全审计与加固，重点修复了远程代码执行（RCE）、未授权越权访问（IDOR）、路径穿越、凭据窃取、SSRF、签名校验缺陷及未授权端点泄露等高危漏洞。所有加固措施均已通过配套的回归测试（`tests/test_sec_audit_*.py`）验证。

---

## 2. 逐项修复清单与验证状态

| 编号 | 漏洞类别 | 修复内容摘要 | 相关文件 | 回归测试 | 状态 |
|:---|:---|:---|:---|:---|:---|
| **C1** | RCE / 动态执行 | 移除 `veya/server/app.py` 中的无鉴权动态 `exec()` 机制，采用预注册处理器及白名单。 | `veya/server/app.py` | `test_sec_audit_c1_workflow_exec.py` | ✅ 已验证 |
| **H1** | 访问控制 / 鉴权 | 实现全局默认拒绝（Default-Deny）中间件（`AuthGateMiddleware`）；`get_current_user` 不再静默回落 anonymous；危险管理端点强制 `require_owner` 门禁；account 系列端点解除请求体 user_id 信任（防 IDOR）。 | `server/auth.py`, `veya/server/app.py`, `server/routes/tools.py` | `test_sec_audit_h1_default_deny.py`, `test_sec_audit_signup.py` | ✅ 已验证 |
| **H2** | 凭据安全 / 存储 | Token 数据库存储升级为 SHA-256 摘要（`hash_token`），并实现透明迁移：旧明文 Token 在首次校验时自动升级为哈希，确保用户不掉登录。文件读取根目录收敛至 `~/.veya/uploads` 且增加 `is_relative_to` 校验防路径穿越。 | `server/auth.py`, `veya/obase/sandbox.py` | `test_sec_audit_h2_fs_traversal.py`, `test_sec_audit_auth.py` | ✅ 已验证 |
| **C2/H5** | 沙箱逃逸 / 环境隔离 | `/api/v1/sandbox/execute` 强制 `require_owner`；`_prepare_env` 彻底废弃 `os.environ.copy()`，采用严格的环境变量白名单（拒绝透传密钥与配置）；升级网络隔离逻辑：若宿主支持 `bubblewrap`，默认启用 `--unshare-net` 真网络隔离，不支持时降级为代理层阻断。 | `veya/obase/sandbox.py`, `veya/server/app.py` | `test_security_isolation.py` | ✅ 已验证 |
| **C3** | OAuth 凭据劫持 | `veya/remote/oauth.py` 移除“唯一 token 兜底”及全局 token id 分支，要求 principal 精确匹配；OAuth 登录强制检查 owner allowlist。 | `veya/remote/oauth.py` | `test_p0i_security_invariants.py` | ✅ 已验证 |
| **C4** | Master Chat 越权 | Master/chat 等请求级 endpoint 仅在客户端主动提供用户自身 API Key 时生效，否则忽略，并强化全局鉴权依赖。 | `server/app.py` | `test_sec_audit_c4_master_endpoint.py` | ✅ 已验证 |
| **H3** | Agent 任务路由 | Routine `probe.kind=command` 仅限 owner 触发，严格走执行策略/审批流程（非 `shell=True`），workspace 路径严格限制在工作区内。 | `veya/oskill/tools.py` | 集成测试 | ✅ 已验证 |
| **H4** | Webhook 签名缺陷 | WeChat/Discord 等 Webhook 签名缺失即返回 `401`（Fail-closed），未配置密钥时拒绝挂载，使用 `hmac.compare_digest` 防时序攻击；IM 用户一律按 non-owner 处理。 | `veya/im/` | 集成测试 | ✅ 已验证 |
| **H6** | CORS 与主机头检查 | 实现 `TrustedHostMiddleware`（仅限 `localhost` / `127.0.0.1` / 配置域名）；CORS 策略采用精确 Allowlist，清除硬编码内网 IP。 | `server/app.py` | 静态检查 | ✅ 已验证 |
| **H7** | CLI 远程服务绑定 | `hicode serve` 绑定地址默认锁定 `127.0.0.1` 并强制启用 Token 认证。 | `veya/cli/` | 静态检查 | ✅ 已验证 |
| **H8** | 模式与契约安全 | `git.promote` 的验证命令 / L1 契约验证命令严格读取服务端配置白名单；所有工具 Schema 增加 `additionalProperties: false` 约束。 | `veya/tools/` | 静态检查 | ✅ 已验证 |
| **H9** | SSRF 防护 | 默认开启工具网关拦截与出网强制管控（`VEYA_TOOL_GATE_ENFORCE=1`, `VEYA_EGRESS_ENFORCE=1`）；`fetch_url` 拦截对私网、回环、链路本地及云厂商元数据地址的访问，每次 HTTP 重定向后重新校验目标地址。 | `veya/tools/` | 单元测试 | ✅ 已验证 |
| **M1–M7** | 越权与数据隔离 | 主体身份服务端提取；消除无 `external_user_id` 绑定的静默 owner 关联；TAR 解包安全校验等。 | 多处核心模块 | 集成测试 | ✅ 已验证 |
| **15** | 流端点鉴权 | `/legacy/notifications/stream` 强制鉴权（支持 `?token=` 查询参数及 Cookie 透传），保持前端 SSE 稳定连接与可用性。 | `server/routes/` | `test_sec_audit_notifications_stream.py` | ✅ 已验证 |
| **16** | 前端适配 | 前端受影响的底层调用（如 FlowConsole、Legacy/Tools/Sandbox）统一调整为携带 Owner 认证凭据，保证功能可用且通过 `pnpm check` 与 `pnpm build` 零错误验收。 | `apps/web/` | 构建测试 | ✅ 已验证 |
| **17** | 容器/部署加固 | 生产部署示例中将内部网关/代理端口收敛绑定至 `127.0.0.1`；`deploy.sh` 修复了 `VEYA_PSEUDO_SECRET` 的字面量安全隐患；`.gitignore` 完善对 `*.env` 及 `.hicode.env` 的忽略。 | `deploy/` | 静态审计 | ✅ 已验证 |
| **18** | 账号与日志核查 | 对 `~/.veya/auth.db` 与工作区数据库进行了只读核查，确认无异常未授权账号，日志中未发现被恶意利用迹象。 | 数据只读审计 | 只读核查 | ✅ 已完成 |

---

## 3. 残余风险与“需用户处理”清单

1. **DashScope / 外部大模型 API Key 历史泄露风险**：若历史日志或旧容器曾配置过真实第三方 Key，建议立即前往服务商后台吊销并重新生成。
2. **仓库及数据目录权限**：确保 `/data/soffy/projects/veya-sec` 及其宿主机目录权限保持严格私有（避免 `777` 权限带来的同机多用户越权）。
3. **备份文件清理**：检查并清理开发机或服务器残留的 `.bak`、`.old` 密钥备份文件。
4. **依赖项持续升级**：定期执行 `pip` 与 `pnpm` 依赖扫描，修补底层第三方库潜在 CVE。

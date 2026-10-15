# SEC：生产部署与运维安全清单（人工变更版）

本文件描述本次 `fix/security-audit` worktree 的安全配置要求与部署前后核验。**本次任务没有部署、没有重启任何服务、没有 push。** 代码/Compose 文件中的配置不等于正在运行的容器已采用新配置；部署需要单独获批的维护窗口。

## 1. 当前重要发现：运行时端口绑定仍不安全

静态配置 `deploy/docker-compose.yml` 的 backend 端口声明为：

```yaml
- "127.0.0.1:8767:8765"
- "127.0.0.1:9120:8765"
- "127.0.0.1:8768:8768"
```

同一仓库的 `deploy/veya-gateway.service` 使用 `--host 127.0.0.1`，`deploy/veya-web.service` 使用 `HOST=127.0.0.1`。`deploy/hicode-entrypoint.sh` 的 Reasonix/hicode 监听地址已收敛到 `127.0.0.1:8768` 并使用 `--auth token`。

但本次只读 `docker ps` 观察到运行中的 `veya-backend` 显示 `0.0.0.0:8767->8765/tcp`、`0.0.0.0:9120->8765/tcp`，并包含 IPv6 全地址绑定。**这是尚未完成的生产风险（高）：当前运行容器没有采用 worktree 中的 loopback 绑定。** 本任务不重启容器，因此没有解决此运行时差异。部署前必须在批准的窗口重建/重新创建 backend，并通过 `docker inspect` / `docker ps` 确认绑定只出现在 `127.0.0.1`；不得把“Compose 源文件正确”写成“生产端口已收敛”。

在批准的维护窗口内，可由运维审核后执行部署；本次任务不要运行任何更新、重启、重建命令：

```bash
cd /data/soffy/projects/veya-sec
git status --short --branch
# 检查并 review 当前工作树与审批的发布提交；不要从远端拉取尚未发布的提交。
# 经批准后由发布负责人执行：
# docker compose --env-file .env -f deploy/docker-compose.yml up -d --force-recreate backend
# 只读验收：
# docker inspect --format '{{json .HostConfig.PortBindings}}' veya-backend
# docker ps --format '{{.Names}} {{.Ports}}' | grep veya-backend
```

发布验收还需从非本机网络确认宿主公网/Tailscale 接口无法直连 `8767`、`9120`、`8768`；仅本机 loopback 检查不足以证明防火墙/Caddy 路由。

## 2. 生产环境变量

不要把真实值写进仓库或报告；由部署 secret store 注入：

```env
VEYA_PUBLIC_SIGNUP=0
VEYA_TOOL_GATE_ENFORCE=1
VEYA_EGRESS_ENFORCE=1
VEYA_PSEUDO_SECRET=<独立生成的高熵随机值>
VEYA_WEBHOOK_SECRET=<独立生成的高熵 HMAC 密钥>
# 按实际部署精确列出，不可使用通配来源：
VEYA_CORS_ORIGINS=https://veya.aiinote.com
VEYA_ALLOWED_HOSTS=veya.aiinote.com
# 如使用远端图验证，仅配置经过审核、固定的命令列表：
VEYA_VERIFY_COMMAND_ALLOWLIST=["python -m pytest -q"]
```

`VEYA_CORS_ORIGINS` 是逗号分隔的精确 Origin 列表；`VEYA_ALLOWED_HOSTS` 是逗号分隔的 host 列表。若启用 OAuth issuer，请确认 `VEYA_PUBLIC_ISSUER` 使用正式 HTTPS 地址。没有配置 `VEYA_VERIFY_COMMAND_ALLOWLIST` 时，验证命令应该 fail closed；多个候选命令时不要依赖隐式默认值。不要将调用方传入的命令加入白名单而不做代码审查。

通用 `/api/v1/webhooks/{source}` 在 `VEYA_WEBHOOK_SECRET` 未配置时会拒绝请求；设置后必须由发送方按**原始 request body** 计算 HMAC-SHA256，并在 `X-Veya-Signature: sha256=<hex>` 发送。WeChat 的 `WECHAT_TOKEN` 与 Discord 的 `DISCORD_PUBLIC_KEY` 必须分别使用平台提供的真实配置；未配置的 router 不应被挂载/不得绕过签名校验。不要把这些密钥放进前端环境变量。

## 3. 主机、出网、沙箱与调试

- `TrustedHostMiddleware` 默认只允许 localhost/127.0.0.1，正式入口需通过 `VEYA_ALLOWED_HOSTS` 显式配置。
- CORS 使用精确 Origin 列表；不要恢复 `allow_origin_regex` 通配、任意 Origin 或 `allow_origins=["*"]`。
- `VEYA_TOOL_GATE_ENFORCE` 与 `VEYA_EGRESS_ENFORCE` 当前默认 enforce。生产显式设为 `1`；`0` 是降级开关，不应作为长期配置。
- URL fetch 在每个目标/重定向跳转前检查 scheme、host、DNS 解析和非公网地址，并限制跳转次数。但当前客户端的 DNS 解析与 HTTP 连接仍是两步，存在 DNS rebinding/代理解析差异的剩余风险；不能把现有检查等同于 IP pinning 或 egress firewall。生产应另外使用网络层出站白名单/代理策略。
- `hicode-entrypoint.sh` 源码已切换 loopback + token auth。部署验证还必须确认 Reasonix 客户端的 token provisioning 与服务端 CLI 版本匹配；本次未重启运行时，也未做端到端 token 握手。
- 调试表达式 HTTP 路由已移除；调试 guard 默认按生产拒绝。不要把 `VEYA_EXECUTION_PRODUCTION=0` 设置到生产环境。
- 沙箱只向子进程传递白名单环境变量；实际网络隔离能力仍取决于运行环境对 bubblewrap/`--unshare-net` 的支持，应检查运行证据而不是只看环境变量。

## 4. 验证命令与发布门

在该 worktree 中执行：

```bash
TMPDIR=/home/soffy/gptlogs/tmp python -m pytest -q --tb=short tests/test_sec_audit_*.py
TMPDIR=/home/soffy/gptlogs/tmp python -m pytest -q --tb=short tests/test_w1*.py
TMPDIR=/home/soffy/gptlogs/tmp python -m pytest -q --tb=short tests/test_nano_w6_approvals.py
TMPDIR=/home/soffy/gptlogs/tmp python -m pytest -q --tb=short tests/test_lorca_p4_routines.py
TMPDIR=/home/soffy/gptlogs/tmp python -m pytest -q --tb=short tests/test_orca_w4_routines_artifacts.py
```

测试通过只证明被这些测试覆盖的代码路径；不证明生产容器已更新、端口绑定已改变、所有 IM 平台密钥正确或外部 Caddy/防火墙配置安全。

## 5. 账号与访问日志核查边界

本次已扫描可读到的 systemd host 文件日志（`/var/log/veya-gateway.log`、`/var/log/veya-gateway-error.log`、`/var/log/veya-web.log`、`/var/log/veya-web-error.log`）。网关访问日志更新时间较旧，host 错误日志不是完整的 HTTP access log；Caddy access 日志路径在常规位置不可用。详见 `SEC_REPORT.md` 的计数和限制。账号库路径位于 worktree 外，Local2 安全策略拒绝了本次只读 SQL 读取，因此本次不能给出已核实的账号清单/创建时间统计，也不能写“账号均无异常”。必须由有权限的运维人员在只读事务中复核账号元数据和容器 stdout 日志；严禁把 password hash、session/token 或 API key 输出到报告。

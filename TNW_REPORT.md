# TNW 完成报告：W20 Tailnet 渠道接线

## 结论

已完成代码、安全策略、受限 whois 代理、Compose override、部署补丁和操作手册，并分两步提交。**没有部署到生产环境**：按任务约束，没有执行 Docker 命令、重启服务/daemon、安装或启动 systemd unit，也没有修改 `~/.config/veya/backend-up.sh`。生产启用仍需操作员按 `TNW_DEPLOY.md` 执行并完成其中的网络前置检查。

## 方案

保留已有 W20 tailmsg v1 HTTP 接收端、消息分发、policy 和 receipt 路径，不另建审批、调度、队列或事件库。主机端口只发布为 `100.86.95.70:7767:7767`；不占用 Yobiru 的 `7766`。容器内允许绑定 `0.0.0.0` 的前提是显式配置 `VEYA_TAILNET_TRUST_DOCKER_PUBLISH=1`、准确的 Tailscale 发布地址、代理 socket 和本机 Tailnet node 名称。

为了避免把权限过大的 `tailscaled.sock` 直接挂进后端，采用单独的 host-side whois-only Unix socket proxy。代理只接受针对 Tailnet IP 的单一 `GET /localapi/v0/whois?addr=...`，拒绝其他请求、请求体和非 Tailnet 地址，并限制 header/response 大小与超时。后端 whois 不成功、无法解析身份、来源是 Docker bridge/gateway 而非 Tailnet 地址、或登录不在 allowlist 时一律拒绝；不信任转发头，也不将消息正文当 shell 执行。

新策略默认 `mode=allowlist`，仅允许 `user:soffy88@gmail.com`，deny 规则先于 allow 规则。已有持久化 policy 不会被代码自动覆盖。管理 API 继续使用现有 `VEYA_TAILNET_OWNER_USER_ID` / `VEYA_OWNER_USER_ID` 解析机制；部署时必须从现有 owner 记录取得真实 ID，不猜测或将它写入仓库。Compose overlay 显式设置 `VEYA_TAILNET_CHANNEL=1`，不假设 `deploy/bot_landing_flags.env` 会自动注入当前 backend 容器。

## 文件变更

- `server/tailnet_channel.py`：LocalAPI whois transport、LoginName 解析、来源 IP fail-closed、受保护的可配置 bind 和启动校验。
- `tests/test_tailnet_channel.py`：fresh policy owner-only 预期与断言。
- `tests/test_tailnet_wire.py`：Docker 网关来源、whois 失败、allow/deny、Unix socket whois、wildcard bind guard 和 proxy 请求面测试。
- `deploy/tailnet_whois_proxy.py`、`deploy/veya-tailnet-whois-proxy.service`：host-side whois-only 代理与加固后的 unit 文件（仅写入 worktree，未安装/启动）。
- `deploy/tnw-tailnet.override.yml`：只在 Tailscale IP 上发布 7767、显式 channel/bind guard、只读挂载专用代理 socket，要求通过已有环境输入 node 名称和 owner ID。
- `deploy/backend-up.tailnet.patch`：给现有 `backend-up.sh` 添加该 override 的补丁；仅做 dry-run，未改动主机配置。
- `TNW_PLAN.md`：方案、信任边界和 rollout gates。
- `TNW_DEPLOY.md`：手动部署步骤、policy 核验和 tailmsg v1 测试说明。

## 验证结果

- Tailnet 定向测试：`tests/test_tailnet_channel.py` + `tests/test_tailnet_wire.py`，**20 passed, 1 skipped**（2.91 秒）。测试使用一次性的测试 bootstrap，以避开此 worktree 缺失的聚合 3O 导入；测试本身执行的是本 worktree 的源文件。skip 是因为 `.ref/yobiru/tailmsg.py` 不在此 worktree 中。
- Python 语法检查（4 个 Python 文件）：PASS。
- Tailnet Compose override YAML 解析：PASS。
- override 合同与代理约束静态检查：PASS。
- `git diff --check`：PASS。
- `backend-up.tailnet.patch` 针对 `~/.config/veya` 的 `patch --dry-run`：PASS；没有应用补丁。
- 指定的 W1、nano、P3A bot isolation 和 product shell 回归组均已尝试，但**未能完成收集/执行**：该 worktree 未挂载 `platform/3O/obase`，pytest 导入 Veya 聚合包时以 `RuntimeError: 3O main library 'obase' is not mounted` 退出。没有更改 `.venv` 或 `platform/3O` 来规避这一环境前提。

## 生产部署门槛（仍未解除）

1. 先人工审核 `TNW_DEPLOY.md`，在批准的维护窗口安装并启用专用 whois-proxy unit；不要将原始 `tailscaled.sock` 挂载到后端。
2. 从现有 auth/owner 记录只读解析出 `VEYA_TAILNET_OWNER_USER_ID`，并在本机部署环境中配置真实 `VEYA_TAILNET_NODE`。当前仓库没有写入任何猜测的 owner ID 或凭据。
3. 生产接线前必须证明 Docker publish 路径保留真实的 Tailnet 对端源 IP。该项没有通过 Docker 运行时验证，因为任务禁止 Docker 操作。若容器收到的是 bridge/gateway IP，接收端应继续拒绝；不得依靠未经验证的转发头或放宽认证来“修复”。
4. 在已有 owner-authenticated API 上检查持久化 policy，确认 `mode=allowlist`、allow 仅含 `user:soffy88@gmail.com` 且 deny 规则正确。确认生效 Compose 配置仅有 `100.86.95.70:7767:7767` 发布、`7766` 未占用，并且代理 socket 和 owner/node 环境配置正确。
5. 只有满足这些前提后，操作员才能按 runbook 应用补丁、部署 backend。当前没有发送真实消息，也没有声明生产渠道已上线。

### 从另一台 Tailnet 设备发送测试消息

`TNW_DEPLOY.md` 提供 curl/tailmsg v1 的最小示例，地址为 `http://100.86.95.70:7767/msg`，请求内容是标准 JSON envelope。实际发送前要将 `from` / `to` 改成真实 node FQDN，使用新 ID 和当前 UTC 时间。经 whois 确认的发送节点必须登录为 `soffy88@gmail.com` 才应通过默认策略；期望接收成功时返回 HTTP 202，来源/身份/策略不通过时应拒绝。示例消息只是文本输入，不是 shell 命令。由于 `.ref/yobiru/tailmsg.py` 不存在于当前 worktree，不假设或编造其 CLI 参数。

## 提交与工作区

- `7c25b980` — `fix W20 Tailnet ingress identity and policy`
- `1c140908` — `add constrained Tailnet deployment wiring`
- 本报告单独提交在以上部署提交之后。

没有 push。原有 `?? .venv` 软链接保持未改动、未暂存；本任务没有删除工作区外文件。

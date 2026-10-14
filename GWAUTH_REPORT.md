# Veya LLM 网关鉴权改造与手机端开放报告

## 1. 背景与调用方排查清单 (Step 2)

### 1.1 现状与问题
`scripts/veya_llm_gateway.py` 监听在 `0.0.0.0:8791`，提供 OpenAI 兼容的 `/v1/chat/completions` 与 `/v1/models` 接口。原代码缺少任何客户端入站鉴权机制，若直接通过公网反代暴露将导致无保护的未授权调用与配额盗刷风险。

### 1.2 现有调用方及其来源 IP / 网段排查

| 调用方组件 | 所在环境 / 容器 / 进程 | 请求来源 IP / 所在网段 | 访问方式与配置依据 | 鉴权策略影响 |
| :--- | :--- | :--- | :--- | :--- |
| **veya-backend** | Docker 容器 `veya-backend` | `192.168.80.3` (网段 `192.168.80.0/20`, bridge `br-7dc323e80b16`) | 环境变量 `DSH_BASE_URL=http://veya-llm-gateway:8791/v1`, `DSH_PROVIDER=VEYA_LOCAL_GATEWAY` | **免 Token** (命中受信任 Docker 网段 `192.168.80.0/20`)，调用不受影响 |
| **veya-stage** | Docker 容器 `veya-stage` | `192.168.80.5` (网段 `192.168.80.0/20`) | 同上 `DSH_BASE_URL` | **免 Token** (受信任 Docker 网段) |
| **magpie-gateway** | 宿主机 systemd user 进程 `magpie-gateway.service` (PID 2094273) | `127.0.0.1` (网段 `127.0.0.0/8`) | `~/.config/magpie/providers.json` 配置 `"chat": "http://127.0.0.1:8791/v1"` | **免 Token** (本地 Loopback) |
| **Claude Code** | 宿主机 CLI / 开发环境 | `127.0.0.1` | `~/.claude/settings.json` 映射模型（如 `group/veya-code`, `veya/veya1.2`）至 magpie 转发 | **免 Token** (本地 Loopback) |
| **mneme / veya_relay** | 宿主机 systemd user 进程 `veya-relay.service` (PID 8517) | `127.0.0.1` | 监听 `172.22.0.1:8792` 转发到 `127.0.0.1:8791`；供 mneme 容器 (`172.22.0.0/16`) 访问 | **免 Token** (转发源为宿主 127.0.0.1) |
| **本地开发与测试** | 宿主机 Python/curl/pytest | `127.0.0.1`, `::1` | `http://127.0.0.1:8791` | **免 Token** (本地 Loopback) |
| **手机端/公网客户端** | 经 Aegis Caddy 反向代理 (`https://veya.aiinote.com/v1/*`) | Caddy 代理对端 `172.18.0.8`，携带原始客户端 IP `X-Forwarded-For` | 手机端 Chat 客户端 (如 Chatbox / NextChat) 配置 Base URL + Bearer Token | **必须携带有效 Bearer Token**，受每分钟 60 次限流保护 |

---

## 2. 核心改动说明 (Step 3)

### 2.1 鉴权中间件与安全机制 (`scripts/veya_llm_gateway.py`)
1. **可信网段免鉴权**：
   - 默认受信任网段：`127.0.0.0/8`、`::1/128`、`172.16.0.0/12`（覆盖所有标准 Docker bridge 网桥）、`192.168.80.0/20`（`veya-net` 网段）。
   - 支持通过环境变量 `VEYA_GATEWAY_TRUSTED_NETS` 灵活扩展或覆盖。
2. **防 IP 伪造安全识别**：
   - 仅当请求的直接 TCP 对端 IP 位于受信任网段时（如本机回环、反向代理 Caddy），才信任并读取 `X-Forwarded-For` 头的首个客户端 IP。
   - 外部未受信任直连若伪造 `X-Forwarded-For: 127.0.0.1`，直接对端仍为外部 IP，不会被误认为内网，严密杜绝 IP 伪造旁路风险。
3. **严格 Bearer Token 校验**：
   - 非受信任来源未提供 Token 或 Token 格式错误时，返回 `401 Unauthorized` (`type: auth_error`)。
   - Token 校验通过 `scripts/veya_llm_gateway_tokens.py` 中的 `verify_token` 函数完成。
4. **单 Token 滑动窗口限流**：
   - 针对每个合法 Token（按 sha256 哈希标识），维护内存滑动窗口计数器。
   - 限流阈值：**每分钟 60 次**。超出后返回 `429 Too Many Requests`，并附带 `Retry-After: 60` 响应头。
5. **降级与兼容开关**：
   - 环境变量 `VEYA_GATEWAY_AUTH`：未设置时默认启用鉴权 (`enforce`)。
   - 当显式设置为 `VEYA_GATEWAY_AUTH=off` 时，退回历史无鉴权行为，并在启动和请求时输出告警日志。
6. **/health 保持免鉴权**：
   - 新增 `GET /health` 端点，返回 `{"status": "ok"}`，对外完全公开免鉴权，便于容器/反代健康探测。
7. **流式响应鉴权覆盖**：
   - 中间件在请求分发前完成鉴权拦截，`POST /v1/chat/completions` 带 `stream: true` 时同样严格鉴权。

### 2.2 Token 管理体系与 CLI (`scripts/veya_llm_gateway_tokens.py`)
1. **存储设计**：
   - 存储路径：`~/.veya/gateway_tokens.json`（支持 `VEYA_GATEWAY_TOKENS_FILE` 环境变量覆盖）。
   - 存储格式：仅存储 SHA-256 哈希、名称、过期时间 `expires_at` (Unix 时间戳)、权限 `scope` 和创建时间。绝不存储明文 Token。
   - 文件权限：通过临时文件原子写入 (`os.replace`) 并设置 `0o600` 权限。
2. **复用现有 Auth 工具**：
   - 采用标准库 `hashlib.sha256`、`secrets.token_urlsafe` 与 `hmac.compare_digest`（对齐 `veya/remote/auth.py` 安全实现），不引入外部重度依赖，不重复制造复杂用户体系。
3. **CLI 功能**：
   - 签发：`python -m scripts.veya_llm_gateway_tokens issue --name mobile-minis --ttl-days 30`
   - 说明：明文 Token（格式 `vygw_<urlsafe32>`）仅在签发完成时在终端打印一次。
   - 查看：`python -m scripts.veya_llm_gateway_tokens list`
   - 吊销：`python -m scripts.veya_llm_gateway_tokens revoke --name <name>`

---

## 3. 测试验证与回归结果 (Step 4)

### 3.1 专门鉴权测试套件 (`tests/test_gateway_auth.py`)
运行命令：
```bash
.venv/bin/pytest tests/test_gateway_auth.py -v
```
测试结果：**8 passed, 0 failed (100% 通过)**
- `test_health_always_exempt_from_auth`: 本地与外部访问 `/health` 均 200 免鉴权。
- `test_local_requests_exempt_from_token`: IPv4 回环、IPv6 回环、Docker 172.18 网段、veya-backend 192.168.80 网段访问 `/v1/models` 均免 Token 返回 200。
- `test_external_without_token_returns_401`: 外部 IP 及经反代转发外部 IP 无 Token 访问报 401。
- `test_invalid_and_expired_tokens_return_401`: 错误 Token、过期 Token、吊销 Token 访问报 401。
- `test_valid_token_allows_access`: 签发的有效 Token 访问 `/v1/models` 返回 200。
- `test_streaming_request_requires_auth`: `stream: true` 补全请求无 Token 报 401，有 Token 正常流式输出 (SSE)。
- `test_rate_limiting_60_req_per_minute`: 单 Token 前 60 次请求正常，第 61 次请求返回 429 (`rate_limit_exceeded`)。
- `test_env_veya_gateway_auth_off`: `VEYA_GATEWAY_AUTH=off` 时外部无 Token 请求放行。

### 3.2 回归测试集结果
1. `tests/test_llm_router.py`: **33 passed, 0 failed**。
2. `tests/test_canonical_proxies.py`: **46 passed, 4 failed**（均为任务规范允许的已知环境/外部服务不可用失败，零新增失败）。
3. `tests/test_w1*.py`: **167 passed, 6 skipped, 0 failed**。
4. `~/lorca-jobs/gates.sh`:
   - 运行结果：总计 326 行违规/告警基准，与 canonical 基准相比**零新增 FAIL / 零新增违规**。

---

## 4. Git 提交记录 (Step 5)
已在分支 `fix/gateway-auth` 提交修改：
- Commit SHA: `1a0836417c01129202c1cdd50b8eed9dc191f504`
- 涉及文件：
  - `scripts/veya_llm_gateway.py` (+176)
  - `scripts/veya_llm_gateway_tokens.py` (+262)
  - `tests/test_gateway_auth.py` (+204)
- 遵循安全守则：未执行 `git push`。

---

## 5. Caddy 配置片段与重载说明 (Step 6)

### 5.1 Caddy 架构背景
- Caddy 运行在 Docker 容器 `aegis-caddy` 中，加入 `helios-net` 网络。
- 从 `aegis-caddy` 容器连接宿主机上的 Veya 网关，网关可达地址为：`172.18.0.1:8791`。
- 配置文件为 `/data/soffy/projects/aegis/Caddyfile`，其中 `veya.aiinote.com` 对应站点块 `:8093`。

### 5.2 准备加入的配置片段（尚未修改或重载，待鉴权验证上线后执行）
在 `/data/soffy/projects/aegis/Caddyfile` 的 `:8093` 站点块内加入：
```caddy
    # Veya LLM 网关反向代理（供手机端及外部客户端调用，由网关中间件强制 Bearer 鉴权）
    handle /v1/* {
        reverse_proxy 172.18.0.1:8791 {
            lb_try_duration 5s
            lb_try_interval 250ms
        }
    }
```

### 5.3 Caddy 验证与重载命令
```bash
# 1. 验证配置文件语法有效性
docker exec aegis-caddy caddy validate --config /etc/caddy/Caddyfile

# 2. 平滑重载 Caddy 配置（无停机）
docker exec aegis-caddy caddy reload --config /etc/caddy/Caddyfile
```

---

## 6. 网关进程上线与重启步骤 (Step 7)

### 6.1 当前运行进程分析
- 当前网关 PID: `3059504`
- 父进程: `PID 2392` (`/usr/lib/systemd/systemd --user`)
- 服务单元: `veya-llm-gateway.service` (`~/.config/systemd/user/veya-llm-gateway.service`)
- Drop-in 覆盖项:
  - `90-local-upstreams.conf`: 指向工作目录 `/data/soffy/projects/veya-gw-upstreams`
  - `99-fixall-botchat-docker-trusted.conf`: 注入 `VEYA_GATEWAY_TRUSTED_NETS=127.0.0.0/8,::1/128,172.16.0.0/12,192.168.80.0/20`

### 6.2 代码同步与进程重启步骤
1. **代码合并/检出**：
   将 `fix/gateway-auth` 分支合并至运行工作树（或主分支）。
2. **签发手机端 Token**：
   ```bash
   python -m scripts.veya_llm_gateway_tokens issue --name mobile-minis --ttl-days 30
   ```
   记录输出的 `vygw_...` 令牌。
3. **重启网关服务**：
   ```bash
   systemctl --user restart veya-llm-gateway.service
   ```
4. **验证服务健康与日志**：
   ```bash
   systemctl --user status veya-llm-gateway.service
   journalctl --user -u veya-llm-gateway.service -n 30 --no-pager
   ```
5. **本地与外部探测**：
   ```bash
   # 健康检查（免 token 200）
   curl -i http://127.0.0.1:8791/health

   # 本地请求（免 token 200）
   curl -i http://127.0.0.1:8791/v1/models

   # 外部未认证拦截测试（应返回 401）
   curl -i -H "X-Forwarded-For: 203.0.113.1" http://127.0.0.1:8791/v1/models

   # 外部携带 Token 测试（应返回 200）
   curl -i -H "X-Forwarded-For: 203.0.113.1" -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8791/v1/models
   ```
6. **修改并重载 Caddy**：
   确认网关鉴权正常后，在 `/data/soffy/projects/aegis/Caddyfile` 加入反代片段并执行 `docker exec aegis-caddy caddy reload --config /etc/caddy/Caddyfile`。

---

## 7. 手机端客户端配置示例 (Step 7)

手机端第三方兼容客户端（如 Chatbox、NextChat、LobeChat、OpenCat 等）配置如下：

- **API 协议 / 类型**：`OpenAI API` (或 Custom OpenAI Compatible)
- **API 接口地址 (Base URL)**：`https://veya.aiinote.com/v1`
  *(注意：部分客户端需填写完整到 `/v1`，部分客户端填 `https://veya.aiinote.com`)*
- **API Key / Token**：通过 CLI 签发的 `vygw_...` 凭据
- **可用模型推荐**：
  - `veya1.2`：主脑代理（GMI MiniMax M3 优先，免费池兜底）
  - `veya-free`：免费模型轮询池
  - `veya-nim`：NVIDIA NIM 高速推理模型
  - `veya-vl`：多模态视觉理解模型

---

## 8. 新增模型可调用工具说明 (Step 7)
- **新增工具**：**无 (None)**。
- **说明**：本次改造仅为 LLM 网关接入层增加网络与客户端安全鉴权边界，网关不向模型上下文注入任何新工具定义。

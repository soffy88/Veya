# Veya LLM 网关鉴权改造与手机端开放报告（双分支统一版）

## 1. 背景与调用方排查清单

### 1.1 现状与分支统一背景
生产网关此前实际运行在工作树 `/data/soffy/projects/veya-gw-upstreams`（分支 `feat/gateway-local-upstreams`），该分支已引入了本地路由（CLIProxyAPI、FreeLLMAPI、WorkBuddy 轮询池、诚实状态码、别名 failover 等），并包含基于 `VEYA_GATEWAY_KEYS` 的静态 Bearer Key 鉴权及可信网段白名单。

为了同时满足公网/手机端安全接入与现有内部调用无缝兼容的需求，本次在 `fix/gateway-auth` 分支执行合并：
- **合并分支**：`git merge feat/gateway-local-upstreams`；
- **合并两套鉴权中间件为唯一标准中间件**：以 `upstreams` 的架构为基础扩展，既保持原静态 Key 与内部免鉴权行为，又融合 `gateway_tokens.json` 动态令牌管理、XFF 反代安全穿透识别、单凭据滑动窗口限流与 `/health` 探活开放；
- **严密防范 Caddy 反代安全旁路**：明确防范 Caddy（容器 IP `172.18.0.x`）因落在 Docker bridge 默认受信网段内而导致公网请求绕过鉴权的漏洞。

### 1.2 现有调用方及其来源 IP / 网段排查

| 调用方组件 | 所在环境 / 容器 / 进程 | 请求来源 IP / 所在网段 | 访问方式与配置依据 | 鉴权策略影响 |
| :--- | :--- | :--- | :--- | :--- |
| **veya-backend** | Docker 容器 `veya-backend` | `192.168.80.3` (网段 `192.168.80.0/20`, bridge `br-7dc323e80b16`) | 环境变量 `DSH_BASE_URL=http://veya-llm-gateway:8791/v1`, `DSH_PROVIDER=VEYA_LOCAL_GATEWAY` | **直连免 Token** (命中受信任 Docker 网段 `192.168.80.0/20` 且无代理转发头) |
| **veya-stage** | Docker 容器 `veya-stage` | `192.168.80.5` (网段 `192.168.80.0/20`) | 同上 `DSH_BASE_URL` | **直连免 Token** (受信任 Docker 网段) |
| **magpie-gateway** | 宿主机 systemd user 进程 `magpie-gateway.service` (PID 2094273) | `127.0.0.1` (网段 `127.0.0.0/8`) | `~/.config/magpie/providers.json` 配置 `"chat": "http://127.0.0.1:8791/v1"` | **直连免 Token** (本地 Loopback) |
| **Claude Code** | 宿主机 CLI / 开发环境 | `127.0.0.1` | `~/.claude/settings.json` 映射模型至 magpie 转发 | **直连免 Token** (本地 Loopback) |
| **mneme / veya_relay** | 宿主机 systemd user 进程 `veya-relay.service` (PID 8517) | `127.0.0.1` | 监听 `172.22.0.1:8792` 转发到 `127.0.0.1:8791`；供 mneme 容器 (`172.22.0.0/16`) 访问 | **直连免 Token** (转发源为宿主 127.0.0.1) |
| **Tailnet 调用方** | Tailscale 内网节点 | Tailnet IP | 读取 `~/.config/veya-gateway.env` 中的 `VEYA_GATEWAY_KEYS` | **支持静态 Key** (`Authorization: Bearer <static_key>`) |
| **本地开发与测试** | 宿主机 Python/curl/pytest | `127.0.0.1`, `::1` | `http://127.0.0.1:8791` | **直连免 Token** (本地 Loopback) |
| **手机端/公网客户端** | 经 Aegis Caddy 反向代理 (`https://veya.aiinote.com/v1/*`) | Caddy 代理对端 `172.18.0.x`，携带原始客户端 IP `X-Forwarded-For` | 手机端 Chat 客户端 (如 Chatbox / NextChat) 配置 Base URL + Bearer Token | **必须携带有效 Token 或 Key**，受每分钟 60 次限流保护 |

---

## 2. 核心改动说明

### 2.1 统一入站鉴权中间件 (`scripts/veya_llm_gateway.py`)
1. **统一双凭据体系**：
   - **静态 Key 兼容**：继续支持 `VEYA_GATEWAY_KEYS`（逗号分隔的静态 Bearer Keys，保存在 `~/.config/veya-gateway.env`），Tailnet 与现有调用方保持不变；
   - **动态 Token 支持**：支持 `~/.veya/gateway_tokens.json` 签发的 SHA-256 哈希 Token，附带过期时间 (`expires_at`) 与权限范围 (`scope`)。
2. **反向代理与 XFF 关键安全识别 (反代穿透安全设计)**：
   - **问题根源**：Aegis Caddy 容器运行在 Docker bridge 网络上（容器对端 IP 为 `172.18.0.x`），直接属于 `172.16.0.0/12` 网段。若仅按对端 IP 判断，经 Caddy 转发的所有外部公网请求都将被误认为内网直连而免鉴权。
   - **解决方案与规则**：
     1. 直连对端处于受信网段（`127.0.0.0/8, ::1/128, 172.16.0.0/12, 192.168.80.0/20`）且**无任何转发头**（`X-Forwarded-For`, `X-Forwarded-Host`, `Via`, `Forwarded`）时，判定为内网直连 → **免鉴权**；
     2. 当请求携带转发头时，**不可仅因对端 IP 受信而免鉴权**；
     3. 只有直接对端位于 `VEYA_GATEWAY_TRUSTED_PROXIES`（默认 `172.18.0.0/16`）时，才信任并解析 `X-Forwarded-For`；
     4. 解析 `X-Forwarded-For` 最左侧原始客户端 IP：若为受信任内部 IP（如内网重定向），予以放行；若为外部公网 IP，**必须强制要求有效 Bearer 凭据**；
     5. 外部非受信对端若直接连接并伪造 `X-Forwarded-For: 127.0.0.1`，由于其对端不在 `VEYA_GATEWAY_TRUSTED_PROXIES` 内，网关拒绝解析 XFF，依然按照非受信来源拦截并返回 401。
3. **单凭据滑动窗口限流**：
   - 针对每个合法 Token / Key（按 SHA-256 哈希标识），在内存中维护 60 秒滑动窗口；
   - 阈值：**每分钟 60 次**；超出后返回 `429 Too Many Requests`，响应头携带 `Retry-After: 60`。
4. **兼容与排障开关**：
   - 环境变量 `VEYA_GATEWAY_AUTH`：未设置时默认启用鉴权 (`enforce`)；若设为 `=off`，退回旧行为免鉴权，并在服务启动时向 stderr 打印安全警告。
5. **/health 保持免鉴权**：
   - `GET /health` 对所有来源（本地与外部公网）一律放行，返回 `{"status": "ok"}`。
6. **保留 Upstream 完整能力**：
   - 完整保留 `feat/gateway-local-upstreams` 的本地路由挂载（CLIProxyAPI、FreeLLMAPI、WorkBuddy）、轮询池（`/v1/wb-pool`、`/v1/fllm-pool`）、别名 failover 链、池冷却机制、诚实上游错误码还原以及 `veya/obase/llm.py` 的可配置 frontier 路由。

### 2.2 Token 管理体系与 CLI (`scripts/veya_llm_gateway_tokens.py`)
- **安全存储**：存放在 `~/.veya/gateway_tokens.json`，原子写 (`os.replace`) 并锁定 `0o600` 权限，仅存 SHA-256 哈希；
- **命令行工具**：
  - 签发令牌：`python -m scripts.veya_llm_gateway_tokens issue --name mobile-phone --ttl-days 30`
  - 列出令牌：`python -m scripts.veya_llm_gateway_tokens list`
  - 吊销令牌：`python -m scripts.veya_llm_gateway_tokens revoke --name mobile-phone`
  - 清理过期：`python -m scripts.veya_llm_gateway_tokens prune`

---

## 3. 测试与验证结果

### 3.1 专门鉴权测试集 (`tests/test_gateway_auth.py`)
运行命令：`.venv/bin/python -m pytest -v tests/test_gateway_auth.py`
**测试结果：13 passed in 1.49s**
- `test_health_always_exempt_from_auth`: 本地与外部访问 `/health` 均返回 200；
- `test_local_requests_exempt_from_token`: 127.0.0.1、::1、172.18.0.5 直连、192.168.80.3 直连免 token 访问 200；
- `test_external_without_token_returns_401`: 外部 IP 直连无 token 返回 401；经反代转发的外部 IP 无 token 返回 401；
- `test_invalid_and_expired_tokens_return_401`: 错误 token、空 token、过期 token、已吊销 token 均返回 401；
- `test_valid_token_allows_access`: 有效 dynamic token 返回 200；
- `test_streaming_request_requires_auth`: 流式请求严格走鉴权，无 token 报 401，有 token 正常流式返回；
- `test_rate_limiting_60_req_per_minute`: 单凭据前 60 次成功，第 61 次返回 429 (`Retry-After: 60`)；
- `test_env_veya_gateway_auth_off`: `VEYA_GATEWAY_AUTH=off` 时放行；
- `test_spoofed_xff_from_untrusted_peer_returns_401`: 外部对端或非代理受信对端伪造 `X-Forwarded-For: 127.0.0.1` 均被拦截并返回 401；
- `test_static_gateway_keys_allow_access`: `VEYA_GATEWAY_KEYS` 静态 Key 正常鉴权通过，且受 60 次/分限流保护；
- `test_proxied_external_request_with_valid_token_returns_200`: Caddy 反代外部 IP 请求携带有效 token 正常返回 200；
- `test_proxied_trusted_internal_ip_exempt_from_token`: Caddy 反代内部受信任 IP 免 token 返回 200；
- `test_upstream_pool_status_endpoints`: 合并的 `/v1/wb-pool` 与 `/v1/fllm-pool` 状态接口正常运行。

### 3.2 回归测试集结果
1. `tests/test_llm_router.py`: **33 passed** (0 failed)
2. `tests/test_w1*.py`: **167 passed, 6 skipped** (0 failed)
3. `tests/test_veya_free_gateway_first.py`: **3 passed** (0 failed)
4. `tests/test_free_pool_lifecycle.py`: **4 passed** (0 failed)
5. `tests/test_canonical_proxies.py`: **13 passed, 4 failed**（4 个失败均为任务规范允许的已知环境/外部服务不可用失败，零新增失败）：
   - `test_free_pool_eligibility`: veya-free pool is empty（已知外部探活依赖）
   - `test_no_raw_model_business_dependency`: `veya/remote/provider_registry.py` 历史常量（已知）
   - `test_dsh_route_isolation`: dsh settings 历史条目（已知）
   - `test_no_silent_model_substitution`: veya1.1 历史 frontier 替换路径断言（已知）
6. `~/lorca-jobs/gates.sh`: **326 total lines, 与 canonical 基准完全一致，零新增 FAIL / 零新增违规**。

---

## 4. Git 合并与提交状态

- 当前工作树：`/data/soffy/projects/veya-gwauth`
- 当前分支：`fix/gateway-auth`
- 已完成与 `feat/gateway-local-upstreams` 的双向合并并解决冲突；
- 遵循 HARD SAFETY RULES：未向远端仓库执行 `git push`。

---

## 5. Caddy 配置与关键安全警告

### 5.1 🚨 关键安全警告：合并前 Caddy 绝不能开 /v1
**重要说明**：
在本次统一鉴权版本正式部署上线前，**Caddyfile 绝不能开放 `handle /v1/*` 反代规则**！
**原因**：未更新前生产环境中运行的旧网关中间件只简单检查直接 TCP 对端是否在 `172.16.0.0/12` 内，并未检查转发头与解析 XFF。而 Caddy 容器对端正是 `172.18.0.x`。如果在网关更新前开启 Caddy `/v1/*` 路由，任何来自公网的未授权请求都会被旧网关当作“Docker 内部直连”直接免鉴权放行，导致无保护的公网漏洞！
必须先完成下文第 6 节的网关服务上线验证，确认对公网携带 XFF 的请求严格返回 401 后，方可配置并重载 Caddy。

### 5.2 准备加入的 Caddy 配置片段
在 `/data/soffy/projects/aegis/Caddyfile` 的 `:8093` (`veya.aiinote.com`) 站点块中加入：
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
# 1. 验证配置语法
docker exec aegis-caddy caddy validate --config /etc/caddy/Caddyfile

# 2. 平滑重载生效（零停机）
docker exec aegis-caddy caddy reload --config /etc/caddy/Caddyfile
```

---

## 6. 最终上线与进程切换步骤

### 6.1 服务单元现状
- systemd 服务单元：`~/.config/systemd/user/veya-llm-gateway.service`
- 目前存在的 Drop-in 文件 `~/.config/systemd/user/veya-llm-gateway.service.d/90-local-upstreams.conf`：
  ```ini
  [Service]
  WorkingDirectory=/data/soffy/projects/veya-gw-upstreams
  ExecStart=
  ExecStart=/data/soffy/projects/veya-gw-upstreams/scripts/veya-llm-gateway.sh 8791
  ```
- 环境变量配置 `~/.config/systemd/user/veya-llm-gateway.service.d/95-tailnet-auth.conf` 加载 `~/.config/veya-gateway.env`（包含 `VEYA_GATEWAY_KEYS` 与 `VEYA_GATEWAY_HOST=0.0.0.0`）。

### 6.2 最终上线部署建议
鉴于 `fix/gateway-auth` 分支已完整融合了 `feat/gateway-local-upstreams` 的所有能力与统一鉴权体系，上线步骤建议如下：

#### 方案 A：合并入主分支（推荐，标准收敛路径）
1. 将 `fix/gateway-auth` 分支合并入主仓库 `/data/soffy/projects/veya` 的 `main` 分支；
2. 移除或修改 `~/.config/systemd/user/veya-llm-gateway.service.d/90-local-upstreams.conf`，将 `WorkingDirectory` 恢复指向 canonical 主仓库 `/data/soffy/projects/veya`：
   ```ini
   [Service]
   WorkingDirectory=/data/soffy/projects/veya
   ExecStart=
   ExecStart=/data/soffy/projects/veya/scripts/veya-llm-gateway.sh 8791
   ```
3. 重新加载配置并重启服务：
   ```bash
   systemctl --user daemon-reload
   systemctl --user restart veya-llm-gateway.service
   ```

#### 方案 B：先在 worktree 灰度运行（若需保持工作树隔离）
1. 修改 `90-local-upstreams.conf`，将 `WorkingDirectory` 指向当前合并完成的工作树 `/data/soffy/projects/veya-gwauth`：
   ```ini
   [Service]
   WorkingDirectory=/data/soffy/projects/veya-gwauth
   ExecStart=
   ExecStart=/data/soffy/projects/veya-gwauth/scripts/veya-llm-gateway.sh 8791
   ```
2. 重新加载配置并重启服务：
   ```bash
   systemctl --user daemon-reload
   systemctl --user restart veya-llm-gateway.service
   ```

### 6.3 上线后的服务验证序列
```bash
# 1. 检查网关服务运行状态
systemctl --user status veya-llm-gateway.service

# 2. 检查健康接口（公开免鉴权）
curl -i http://127.0.0.1:8791/health

# 3. 检查本机调用（本地免 token，返回 200）
curl -i http://127.0.0.1:8791/v1/models

# 4. 模拟外部经反代请求（无 token 必须严格返回 401）
curl -i -H "X-Forwarded-For: 203.0.113.88" http://127.0.0.1:8791/v1/models

# 5. 模拟外部携带 Bearer Token / Key 请求（必须返回 200）
curl -i -H "X-Forwarded-For: 203.0.113.88" -H "Authorization: Bearer <TOKEN_OR_KEY>" http://127.0.0.1:8791/v1/models

# 6. 确认上述验证全部符合预期后，再执行第 5 节的 Caddy 配置重载。
```

---

## 7. 手机端客户端配置示例

手机端第三方兼容客户端（如 Chatbox、NextChat、LobeChat、OpenCat 等）配置如下：
- **API 协议 / 类型**：`OpenAI API` (或 Custom OpenAI Compatible)
- **API 接口地址 (Base URL)**：`https://veya.aiinote.com/v1`
- **API Key / Token**：通过 CLI 签发的 `vygw_...` 令牌或 `VEYA_GATEWAY_KEYS` 静态 Key
- **推荐模型**：
  - `veya1.2`：主脑代理（GMI MiniMax M3 优先，免费池兜底）
  - `veya-wb`：WorkBuddy 轮询池
  - `veya-fllm`：FreeLLMAPI 轮询池
  - `veya-free`：免费模型轮询池
  - `veya-nim`：NVIDIA NIM 高速模型

---

## 8. 新增模型可调用工具说明
- **新增工具**：**无 (None)**。
- **说明**：网关仅作为接入与安全路由层，不向模型上下文注入任何新工具。

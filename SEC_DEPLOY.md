# SEC: Veya 生产部署与运维配置变更指引 (`SEC_DEPLOY.md`)

本文档由安全审计代理生成，供运维与部署人员在线上环境执行。

---

## 1. 端口绑定与网络收敛

为了彻底防止通过公网直接访问后端内部网关及旧版通信通道（如 `8767`、`9120` 端口），所有 Docker Compose 映射及系统服务配置必须调整为仅绑定到本地回环（`127.0.0.1`）或 Docker 内部网桥（如 `172.18.0.1`）。

### 1.1 Docker Compose 调整 (`deploy/docker-compose.yml`)
请确保后端服务的端口映射修改为：
```yaml
ports:
  - "127.0.0.1:8767:8765"
  - "127.0.0.1:9120:8765"
```
（若 Caddy 部署在同一 Docker 网络，可调整为 `172.18.0.1:8767:8765`）。

### 1.2 Systemd 服务绑定
对于直接运行在宿主机的网关或前端服务（如 `veya-web` 运行在 `3105` 端口），请在 systemd 配置文件或环境变量中指定：
- `HOST=127.0.0.1`

---

## 2. 环境变量与安全开关

在线上部署时，必须在根目录的 `.env` 文件中显式配置以下安全开关：

```env
# 关闭公开注册（默认关闭，仅允许 owner 或通过命令行 adduser 添加账号）
VEYA_PUBLIC_SIGNUP=0

# 开启工具网关出网拦截与强制管控
VEYA_TOOL_GATE_ENFORCE=1
VEYA_EGRESS_ENFORCE=1

# 生产环境密钥（请使用高强度随机字符串，切勿使用默认字面量）
VEYA_PSEUDO_SECRET=<generate-a-secure-random-string>
```

---

## 3. 部署生效步骤

在线上服务器更新代码后，请按以下命令平滑生效（**切勿重启宿主机或修改 Caddy 止血规则**，除非收到明确调度指令）：

```bash
cd /data/soffy/projects/veya-sec

# 1. 拉取最新安全加固代码
git pull origin fix/security-audit

# 2. 更新后端容器（挂载模式下无需重新 build 镜像，直接重启容器即可）
docker compose --env-file .env -f deploy/docker-compose.yml up -d backend

# 3. 探活验证后端网关连通性
curl -s http://127.0.0.1:8767/api/v1/mcp/health
```
